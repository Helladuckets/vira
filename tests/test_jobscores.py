"""The per-role score store.

Everything roots at ONE tmp universe dir. `test_an_empty_fixture_reads_
nothing` is the isolation guard: jobscores reads the universe dir, the
boards snapshot and the owner's canon, and a case added later that
resolves one of those from settings instead of the fixture fails there
rather than silently reading the real self-record.
"""
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import jobscores


def entry(uid="a-1", **kw):
    base = {"uid": uid, "fit": 70, "tier": "2", "why_fit": "grounded reason"}
    base.update(kw)
    return base


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.udir = Path(self._tmp.name)
        (self.udir / "candidate-universe" / "role").mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        # canon_paths reaches self_record(); pin it inside the fixture so no
        # case can read the owner's real MASTER_HISTORY.
        self.record = self.udir / "record"
        (self.record / "canon").mkdir(parents=True)
        p = mock.patch("server.applications.self_record",
                       return_value=self.record)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch("server.applications.universe_dir",
                        return_value=self.udir)
        p2.start()
        self.addCleanup(p2.stop)

    def batch(self, name, rows):
        (self.udir / name).write_text(json.dumps(rows), encoding="utf-8")

    def role_file(self, uid):
        (self.udir / "candidate-universe" / "role" / f"{uid}.json").write_text(
            json.dumps({"uid": uid}), encoding="utf-8")

    def canon(self, when=None):
        f = self.record / "canon" / "MASTER_HISTORY.md"
        f.write_text("history", encoding="utf-8")
        if when is not None:
            os.utime(f, (when, when))
        return f


class Isolation(Base):
    def test_an_empty_fixture_reads_nothing(self):
        self.assertEqual(jobscores.load(self.udir), {})
        self.assertEqual(jobscores.per_role(self.udir), {})
        self.assertEqual(jobscores.legacy(self.udir), {})
        self.assertEqual(jobscores.status(self.udir)["per_role"], 0)


class Precedence(Base):
    def test_per_role_beats_a_legacy_entry(self):
        self.batch("v2-raw-scores.json", [entry(why_fit="the old read")])
        jobscores.write(entry(why_fit="the new read"), udir=self.udir)
        self.assertEqual(jobscores.load(self.udir)["a-1"]["why_fit"],
                         "the new read")

    def test_the_date_named_file_trap_cannot_bite(self):
        """The regression this whole store exists for.

        Legacy merging is sorted-filename order, later wins — and digits
        sort before letters, so a file named for today ('2026-08-12-...')
        LOSES to 'v2-...' for the same uid. A rescore written that way
        would land on disk and be silently overridden. Under the new
        reader the per-role store decides and filenames decide nothing.
        """
        self.batch("v2-raw-scores.json", [entry(why_fit="july")])
        self.batch("2026-08-12-raw-scores.json", [entry(why_fit="today")])
        # the legacy merge really does prefer the July file
        self.assertEqual(jobscores.legacy(self.udir)["a-1"]["why_fit"],
                         "july")
        jobscores.write(entry(why_fit="today"), udir=self.udir)
        self.assertEqual(jobscores.load(self.udir)["a-1"]["why_fit"],
                         "today")

    def test_legacy_still_answers_for_a_uid_with_no_per_role_file(self):
        self.batch("v2-raw-scores.json", [entry("a-1"), entry("a-2")])
        jobscores.write(entry("a-1", why_fit="fresh"), udir=self.udir)
        loaded = jobscores.load(self.udir)
        self.assertEqual(loaded["a-1"]["why_fit"], "fresh")
        self.assertEqual(loaded["a-2"]["why_fit"], "grounded reason")

    def test_fulluid_is_double_indexed_like_the_legacy_reader(self):
        jobscores.write(entry("a-1", _fulluid="a-1-full"), udir=self.udir)
        loaded = jobscores.load(self.udir)
        self.assertIn("a-1", loaded)
        self.assertIn("a-1-full", loaded)
        self.assertIs(loaded["a-1"], loaded["a-1-full"])


class Validation(Base):
    def refuses(self, bad, *, known=None):
        with self.assertRaises(jobscores.ScoreError) as cm:
            jobscores.validate(bad, known=known)
        return str(cm.exception)

    def test_a_blank_uid_is_refused(self):
        self.assertIn("uid is required", self.refuses(entry(uid="")))

    def test_a_uid_that_is_not_filename_safe_is_refused(self):
        self.assertIn("not a valid role uid",
                      self.refuses(entry(uid="../../etc/passwd")))

    def test_an_unknown_uid_is_refused_when_the_caller_knows_the_set(self):
        msg = self.refuses(entry("a-9"), known={"a-1"})
        self.assertIn("matches no role", msg)

    def test_fit_out_of_range_is_refused(self):
        self.assertIn("between 0 and 100", self.refuses(entry(fit=140)))

    def test_a_missing_required_field_is_refused(self):
        bad = entry()
        del bad["why_fit"]
        self.assertIn("why_fit is required", self.refuses(bad))

    def test_an_oversize_why_fit_is_refused_on_write(self):
        msg = self.refuses(entry(why_fit="x" * (jobscores.MAX_WHY + 1)))
        self.assertIn("keep it under", msg)

    def test_an_off_vocabulary_tier_is_refused(self):
        self.assertIn("must be one of", self.refuses(entry(tier="gold")))

    def test_the_T_prefixed_tier_spelling_is_accepted(self):
        """A model writing T1 is using this module's own UI spelling; a
        retry round trip over the prefix would buy nothing."""
        self.assertEqual(jobscores.validate(entry(tier="T1"))["tier"], "1")

    def test_an_off_vocabulary_verdict_is_refused(self):
        self.assertIn("verdict must be one of",
                      self.refuses(entry(verdict="maybe")))

    def test_the_model_may_not_stamp_its_own_provenance(self):
        clean = jobscores.validate(
            entry(scored_at="1999-01-01T00:00:00+00:00", canon="x",
                  prev={"fit": 1}))
        for field in ("scored_at", "canon", "prev"):
            self.assertNotIn(field, clean)

    def test_unknown_fields_are_preserved(self):
        clean = jobscores.validate(entry(served="2026-01-01", _title="Eng"))
        self.assertEqual(clean["served"], "2026-01-01")
        self.assertEqual(clean["_title"], "Eng")


class Writing(Base):
    def test_the_server_stamps_when_and_against_what(self):
        self.canon()
        rec = jobscores.write(entry(), udir=self.udir)
        self.assertTrue(rec["scored_at"])
        self.assertEqual(rec["canon"], jobscores.canon_at(self.udir))

    def test_a_rescore_keeps_the_previous_score_one_deep(self):
        jobscores.write(entry(fit=50, why_fit="first"), udir=self.udir)
        rec = jobscores.write(entry(fit=90, why_fit="second"), udir=self.udir)
        self.assertEqual(rec["fit"], 90)
        self.assertEqual(rec["prev"]["fit"], 50)
        self.assertNotIn("prev", rec["prev"])

    def test_the_screen_score_round_trips(self):
        rec = jobscores.write(entry(screen=42), udir=self.udir)
        self.assertEqual(jobscores.load(self.udir)["a-1"]["screen"], 42)
        self.assertEqual(rec["screen"], 42)

    def test_a_passive_instance_refuses_to_write(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                jobscores.write(entry(), udir=self.udir)
        self.assertEqual(jobscores.per_role(self.udir), {})

    def test_a_corrupt_score_file_is_skipped_not_fatal(self):
        d = jobscores.scores_dir(self.udir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "a-2.json").write_text("{not json", encoding="utf-8")
        jobscores.write(entry("a-1"), udir=self.udir)
        self.assertEqual(list(jobscores.per_role(self.udir)), ["a-1"])

    def test_cp1252_bytes_do_not_raise_into_the_caller(self):
        """The documented widen-the-except rule: this module pins
        encoding='utf-8' on every read, so an install carrying bytes from
        before that pin raises UnicodeDecodeError — which is neither an
        OSError nor a JSONDecodeError."""
        d = jobscores.scores_dir(self.udir)
        d.mkdir(parents=True, exist_ok=True)
        (d / "a-3.json").write_bytes(
            '{"uid": "a-3", "why_fit": "café"}'.encode("cp1252"))
        self.assertEqual(jobscores.per_role(self.udir), {})


class Staleness(Base):
    def test_a_score_written_before_the_canon_moved_is_stale(self):
        self.canon(when=time.time() - 86400)
        rec = jobscores.write(entry(), udir=self.udir)
        self.assertFalse(jobscores.is_stale(rec, udir=self.udir))
        self.canon(when=time.time() + 60)          # the canon moves
        self.assertTrue(jobscores.is_stale(rec, udir=self.udir))

    def test_an_unstamped_score_reads_stale(self):
        self.canon()
        self.assertTrue(jobscores.is_stale({"uid": "a-1"}, udir=self.udir))

    def test_nothing_is_stale_when_no_canon_exists(self):
        self.assertEqual(jobscores.canon_at(self.udir), "")
        self.assertFalse(jobscores.is_stale({"uid": "a-1"}, udir=self.udir))

    def test_the_adjudication_counts_as_canon_too(self):
        (self.udir / "owner-adjudication.json").write_text("{}",
                                                           encoding="utf-8")
        self.assertTrue(jobscores.canon_at(self.udir))

    def test_status_splits_current_from_stale_and_undated(self):
        """Undated is its OWN bucket: an entry with no stamp predates
        stamping, which is a different fact from one written against a
        canon that has since moved."""
        self.canon(when=time.time() - 86400)
        jobscores.write(entry("a-1"), udir=self.udir)
        d = jobscores.scores_dir(self.udir)
        (d / "a-2.json").write_text(json.dumps({"uid": "a-2", "fit": 1}),
                                    encoding="utf-8")
        st = jobscores.status(self.udir)
        self.assertEqual(st["per_role"], 2)
        self.assertEqual(st["scores_current"], 1)
        self.assertEqual(st["scores_stale"], 0)
        self.assertEqual(st["scores_unstamped"], 1)

    def test_status_counts_legacy_entries_too(self):
        """Before the migration every score is a legacy entry; a freshness
        line that ignored those would report zero on an install holding a
        thousand of them."""
        self.batch("v2-raw-scores.json", [entry("a-1"), entry("a-2")])
        st = jobscores.status(self.udir)
        self.assertEqual(st["scored_total"], 2)
        self.assertEqual(st["legacy_only"], 2)
        self.assertEqual(st["per_role"], 0)


class Migration(Base):
    def test_every_legacy_entry_lands_exactly_once(self):
        self.batch("v2-raw-scores.json", [entry("a-1"), entry("a-2")])
        self.batch("swarm-1-raw-scores.json", [entry("a-3")])
        report = jobscores.migrate(self.udir)
        self.assertEqual(report["written"], 3)
        self.assertEqual(report["entries"], 3)
        self.assertEqual(len(jobscores.per_role(self.udir)), 3)

    def test_a_double_indexed_entry_writes_one_file(self):
        self.batch("v2-raw-scores.json",
                   [entry("a-1", _fulluid="a-1-full")])
        report = jobscores.migrate(self.udir)
        self.assertEqual(report["written"], 1)
        files = list(jobscores.scores_dir(self.udir).glob("*.json"))
        self.assertEqual([p.name for p in files], ["a-1.json"])

    def test_an_oversize_why_fit_survives_verbatim(self):
        """Caps apply to what Vira writes, never to what it migrates —
        truncating the owner's own analysis to satisfy a rule invented
        afterwards would destroy data."""
        long = "x" * (jobscores.MAX_WHY + 5000)
        self.batch("v2-raw-scores.json", [entry(why_fit=long)])
        jobscores.migrate(self.udir)
        self.assertEqual(jobscores.load(self.udir)["a-1"]["why_fit"], long)

    def test_provenance_and_the_source_mtime_are_recorded(self):
        self.batch("v2-raw-scores.json", [entry()])
        when = time.time() - 30 * 86400
        os.utime(self.udir / "v2-raw-scores.json", (when, when))
        jobscores.migrate(self.udir)
        rec = jobscores.load(self.udir)["a-1"]
        self.assertEqual(rec["source_file"], "v2-raw-scores.json")
        self.assertEqual(rec["scored_at"], jobscores._stamp(when))

    def test_migration_never_overwrites_an_existing_per_role_file(self):
        jobscores.write(entry(why_fit="hand written"), udir=self.udir)
        self.batch("v2-raw-scores.json", [entry(why_fit="the old batch")])
        report = jobscores.migrate(self.udir)
        self.assertEqual(report["existing"], 1)
        self.assertEqual(report["written"], 0)
        self.assertEqual(jobscores.load(self.udir)["a-1"]["why_fit"],
                         "hand written")

    def test_the_legacy_files_are_left_on_disk(self):
        self.batch("v2-raw-scores.json", [entry()])
        before = (self.udir / "v2-raw-scores.json").read_bytes()
        jobscores.migrate(self.udir)
        self.assertEqual((self.udir / "v2-raw-scores.json").read_bytes(),
                         before)

    def test_an_unusable_uid_is_reported_not_crashed(self):
        self.batch("v2-raw-scores.json",
                   [entry("../evil"), entry("a-2")])
        report = jobscores.migrate(self.udir)
        self.assertEqual(report["written"], 1)
        self.assertEqual(report["skipped"], ["../evil"])

    def test_a_passive_instance_refuses_to_migrate(self):
        self.batch("v2-raw-scores.json", [entry()])
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(PermissionError):
                jobscores.migrate(self.udir)


class CacheInvalidation(Base):
    def test_a_score_write_moves_the_universe_cache_key(self):
        """Without this a rescore lands correctly and the catalog keeps
        serving the old why_fit until an unrelated file happens to change
        (the jobboards.state_mtime seam)."""
        from server import applications
        before = applications._universe_key(self.udir)
        jobscores.write(entry(), udir=self.udir)
        self.assertNotEqual(applications._universe_key(self.udir), before)

    def test_dir_mtime_is_zero_when_nothing_has_been_scored(self):
        self.assertEqual(jobscores.dir_mtime(self.udir), 0.0)


class KnownUids(Base):
    def test_role_files_and_the_snapshot_both_count(self):
        self.role_file("a-1")
        with mock.patch("server.jobboards.snapshot",
                        return_value={"roles": {"as-x-2": {}}}):
            self.assertEqual(jobscores.known_uids(self.udir),
                             {"a-1", "as-x-2"})

    def test_a_broken_snapshot_never_blocks_a_write(self):
        self.role_file("a-1")
        with mock.patch("server.jobboards.snapshot",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(jobscores.known_uids(self.udir), {"a-1"})


class ToolLayer(Base):
    """The MCP write tool: one bad entry loses itself, never the batch."""

    def setUp(self):
        super().setUp()
        p = mock.patch("server.jobscores.known_uids", return_value=set())
        p.start()
        self.addCleanup(p.stop)

    def call(self, payload):
        from server import viratools
        return viratools._record_role_scores_text(json.dumps(payload))

    def test_a_batch_writes_and_reports(self):
        out = self.call([entry("a-1"), entry("a-2")])
        self.assertIn("recorded 2", out)
        self.assertEqual(len(jobscores.per_role(self.udir)), 2)

    def test_one_bad_entry_does_not_lose_the_good_ones(self):
        out = self.call([entry("a-1"), entry("a-2", fit=500)])
        self.assertIn("recorded 1", out)
        self.assertIn("1 refused", out)
        self.assertIn("a-2", out)
        self.assertEqual(list(jobscores.per_role(self.udir)), ["a-1"])

    def test_malformed_json_is_named(self):
        from server import viratools
        self.assertIn("not valid JSON",
                      viratools._record_role_scores_text("{["))

    def test_a_passive_instance_refuses_the_whole_call(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            out = self.call([entry()])
        self.assertIn("passive instance", out)
        self.assertEqual(jobscores.per_role(self.udir), {})


if __name__ == "__main__":
    unittest.main()
