"""The Reader's queue: soft pointers, completion that removes, graceful sections.

The three behaviours worth pinning, because each is a decision rather than an
implementation detail:
  1. register() is idempotent on the locator AND never resurrects a read item -
     both the producers and the backfill sweep call it repeatedly.
  2. Completion REMOVES from the queue. The entry survives so the sweep cannot
     re-add it and so the mark can be undone.
  3. Section progress degrades to whole-document silently when a document has
     no anchors, because most documents will not have them.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from server import reading, readinglist


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = root / "reading-list.json"
        self.readdir = root / "reading"
        self.static = root / "static"
        (self.static / "explainer").mkdir(parents=True)
        for p in (mock.patch.object(readinglist, "STORE", self.store),
                  mock.patch.object(readinglist, "ROOT", root),
                  mock.patch.object(readinglist, "EXPLAINER_DIR",
                                    self.static / "explainer"),
                  mock.patch.object(reading, "STORE_DIR", self.readdir)):
            p.start()
            self.addCleanup(p.stop)

    def dossier(self, name, title, sections=()):
        d = self.static / "explainer" / name
        d.mkdir(parents=True, exist_ok=True)
        body = "".join(f'<h2 id="{sid}">{lbl}</h2><p>x</p>'
                       for sid, lbl in sections)
        (d / "index.html").write_text(
            f"<html><head><title>{title}</title></head><body>{body}</body></html>",
            encoding="utf-8")
        return f"/explainer/{name}/"


class RegisterTests(Base):
    def test_register_returns_an_entry_with_a_legal_slug(self):
        it = readinglist.register("Where we stand", "dossier", "/explainer/audit/")
        self.assertTrue(it["id"].startswith("rl_"))
        self.assertEqual(it["kind"], "dossier")
        self.assertIsNone(it["completed"])
        # the slug is used as a reading.py list name, which validates hard
        self.assertRegex(it["slug"], reading.NAME_RE)

    def test_register_is_idempotent_on_the_locator(self):
        a = readinglist.register("Audit", "dossier", "/explainer/audit/")
        b = readinglist.register("Audit", "dossier", "/explainer/audit/")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(readinglist.queue()), 1)

    def test_reregistering_refreshes_the_title(self):
        readinglist.register("Old name", "dossier", "/explainer/audit/")
        readinglist.register("New name", "dossier", "/explainer/audit/")
        self.assertEqual(readinglist.queue()[0]["title"], "New name")

    def test_reregistering_never_un_reads(self):
        # the sweep re-walks the same directories; a read document must stay read
        it = readinglist.register("Audit", "dossier", "/explainer/audit/")
        readinglist.complete(it["id"])
        readinglist.register("Audit", "dossier", "/explainer/audit/")
        self.assertEqual(readinglist.queue(), [])
        self.assertEqual(len(readinglist.completed()), 1)

    def test_same_locator_different_kind_is_a_different_entry(self):
        readinglist.register("A", "dossier", "x", "url")
        readinglist.register("A", "plan", "x", "plan")
        self.assertEqual(len(readinglist.queue()), 2)

    def test_bad_kind_and_bad_locator_kind_are_refused(self):
        with self.assertRaises(ValueError):
            readinglist.register("A", "nonsense", "/x/")
        with self.assertRaises(ValueError):
            readinglist.register("A", "dossier", "/x/", "carrier-pigeon")

    def test_empty_locator_is_refused(self):
        with self.assertRaises(ValueError):
            readinglist.register("A", "dossier", "   ")

    def test_title_falls_back_to_the_locator(self):
        it = readinglist.register("", "dossier", "/explainer/audit/")
        self.assertEqual(it["title"], "/explainer/audit/")


class CompletionTests(Base):
    def test_completing_removes_it_from_the_queue(self):
        it = readinglist.register("Audit", "dossier", "/explainer/audit/")
        self.assertEqual(len(readinglist.queue()), 1)
        readinglist.complete(it["id"])
        self.assertEqual(readinglist.queue(), [])

    def test_the_entry_survives_completion(self):
        it = readinglist.register("Audit", "dossier", "/explainer/audit/")
        readinglist.complete(it["id"])
        kept = readinglist.get(it["id"])
        self.assertIsNotNone(kept)
        self.assertTrue(kept["completed"])
        self.assertEqual(readinglist.completed()[0]["id"], it["id"])

    def test_completion_is_undoable(self):
        it = readinglist.register("Audit", "dossier", "/explainer/audit/")
        readinglist.complete(it["id"])
        readinglist.complete(it["id"], done=False)
        self.assertEqual(len(readinglist.queue()), 1)
        self.assertEqual(readinglist.completed(), [])

    def test_completing_an_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            readinglist.complete("rl_nope")

    def test_forget_drops_the_pointer_and_leaves_the_file(self):
        loc = self.dossier("audit", "Audit")
        it = readinglist.register("Audit", "dossier", loc)
        readinglist.forget(it["id"])
        self.assertEqual(readinglist.queue(), [])
        self.assertTrue((self.static / "explainer" / "audit" / "index.html").is_file())

    def test_counts_split_queued_from_completed(self):
        a = readinglist.register("A", "dossier", "/a/")
        readinglist.register("B", "dossier", "/b/")
        readinglist.complete(a["id"])
        self.assertEqual(readinglist.counts(),
                         {"queued": 1, "completed": 1, "total": 2})


class SectionTests(Base):
    def test_html_h2_anchors_become_sections(self):
        loc = self.dossier("audit", "Audit",
                           [("now", "Broken right now"), ("sec", "Security"),
                            ("data", "Data integrity")])
        it = readinglist.register("Audit", "dossier", loc)
        secs = readinglist.sections(it)
        self.assertEqual([s["id"] for s in secs], ["now", "sec", "data"])
        self.assertEqual(secs[0]["title"], "Broken right now")

    def test_markup_inside_a_heading_is_stripped(self):
        d = self.static / "explainer" / "m"
        d.mkdir(parents=True)
        (d / "index.html").write_text(
            "<title>M</title><h2 id='a'><span class='k'>01</span>Numbers</h2>"
            "<h2 id='b'>Two</h2>", encoding="utf-8")
        it = readinglist.register("M", "dossier", "/explainer/m/")
        self.assertEqual(readinglist.sections(it)[0]["title"], "01 Numbers")

    def test_a_document_without_anchors_reports_no_sections(self):
        loc = self.dossier("plain", "Plain")          # no h2s at all
        it = readinglist.register("Plain", "dossier", loc)
        self.assertEqual(readinglist.sections(it), [])
        self.assertEqual(readinglist.progress(it), {"done": 0, "total": 0})

    def test_a_single_heading_is_not_progress(self):
        loc = self.dossier("one", "One", [("only", "Only section")])
        it = readinglist.register("One", "dossier", loc)
        self.assertEqual(readinglist.sections(it), [])

    def test_a_missing_file_degrades_rather_than_raising(self):
        it = readinglist.register("Gone", "dossier", "/explainer/gone/")
        self.assertEqual(readinglist.sections(it), [])
        self.assertTrue(readinglist.queue()[0]["missing"])

    def test_markdown_headings_become_sections(self):
        vault = Path(self.tmp.name) / "vault"
        (vault / "Sessions").mkdir(parents=True)
        (vault / "Sessions" / "r.md").write_text(
            "# Title\n\n## Shipped\ntext\n\n## Drift\ntext\n", encoding="utf-8")
        with mock.patch.object(readinglist.settings, "get",
                               side_effect=lambda k: str(vault) if k == "vault_root" else ""):
            it = readinglist.register("r", "retro", "Sessions/r.md", "vault")
            self.assertEqual([s["id"] for s in readinglist.sections(it)],
                             ["shipped", "drift"])

    def test_progress_counts_marks_against_the_sections(self):
        loc = self.dossier("audit", "Audit",
                           [("a", "A"), ("b", "B"), ("c", "C")])
        it = readinglist.register("Audit", "dossier", loc)
        reading.set_done(it["slug"], "a")
        reading.set_done(it["slug"], "c")
        reading.set_done(it["slug"], "not-a-section")   # ignored
        self.assertEqual(readinglist.progress(it), {"done": 2, "total": 3})


class VaultContainmentTests(Base):
    def test_a_locator_escaping_the_vault_resolves_to_nothing(self):
        vault = Path(self.tmp.name) / "vault"
        (vault / "Sessions").mkdir(parents=True)
        secret = Path(self.tmp.name) / "secret.md"
        secret.write_text("## a\n## b\n", encoding="utf-8")
        with mock.patch.object(readinglist.settings, "get",
                               side_effect=lambda k: str(vault) if k == "vault_root" else ""):
            it = readinglist.register("esc", "retro", "../secret.md", "vault")
            self.assertIsNone(readinglist.source_path(it))
            self.assertEqual(readinglist.sections(it), [])
            self.assertTrue(readinglist.queue()[0]["missing"])

    def test_no_vault_configured_is_dormant_not_broken(self):
        with mock.patch.object(readinglist.settings, "get", return_value=""):
            it = readinglist.register("r", "retro", "Sessions/r.md", "vault")
            self.assertIsNone(readinglist.source_path(it))
            self.assertEqual(readinglist._vault_documents(), [])


class BackfillTests(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(readinglist, "_rooms", return_value=[]),
                  mock.patch.object(readinglist, "_plans", return_value=[]),
                  mock.patch.object(readinglist, "_vault_documents", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def test_backfill_finds_dossiers_and_reads_their_titles(self):
        self.dossier("audit", "Where we stand - end-of-week audit")
        self.dossier("other", "Another dossier")
        out = readinglist.backfill()
        self.assertEqual(out["added"], 2)
        titles = sorted(i["title"] for i in readinglist.queue())
        self.assertEqual(titles,
                         ["Another dossier", "Where we stand - end-of-week audit"])

    def test_backfill_is_idempotent(self):
        self.dossier("audit", "Audit")
        readinglist.backfill()
        second = readinglist.backfill()
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(readinglist.queue()), 1)

    def test_backfill_does_not_resurrect_a_read_dossier(self):
        self.dossier("audit", "Audit")
        readinglist.backfill()
        readinglist.complete(readinglist.queue()[0]["id"])
        readinglist.backfill()
        self.assertEqual(readinglist.queue(), [])

    def test_a_directory_without_an_index_is_skipped(self):
        (self.static / "explainer" / "assets").mkdir()
        self.assertEqual(readinglist.backfill()["added"], 0)

    def test_top_level_pages_are_not_taken(self):
        # the four hand-authored atlas pages are the system explainer, not reading
        (self.static / "explainer" / "modules.html").write_text(
            "<title>Modules</title>", encoding="utf-8")
        self.assertEqual(readinglist.backfill()["added"], 0)


class FreshnessTests(Base):
    """A first sweep over months of history must not hand back a 186-item
    "focused attention" list. Anything older than the window is filed as read."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(readinglist, "_rooms", return_value=[]),
                  mock.patch.object(readinglist, "_plans", return_value=[]),
                  mock.patch.object(readinglist, "_vault_documents", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def _aged(self, name, days):
        loc = self.dossier(name, name.title())
        old = datetime.now(timezone.utc) - timedelta(days=days)
        f = self.static / "explainer" / name / "index.html"
        import os
        os.utime(f, (old.timestamp(), old.timestamp()))
        return loc

    def test_an_old_document_is_filed_as_read_not_queued(self):
        self._aged("ancient", 90)
        out = readinglist.backfill()
        self.assertEqual(out["added"], 1)
        self.assertEqual(out["queued_new"], 0)
        self.assertEqual(readinglist.queue(), [])
        self.assertEqual(len(readinglist.completed()), 1)

    def test_a_fresh_document_queues(self):
        self._aged("today", 0)
        out = readinglist.backfill()
        self.assertEqual(out["queued_new"], 1)
        self.assertEqual(len(readinglist.queue()), 1)

    def test_the_sweep_never_refiles_something_put_back(self):
        self._aged("ancient", 90)
        readinglist.backfill()
        it = readinglist.completed()[0]
        readinglist.complete(it["id"], done=False)      # owner puts it back
        readinglist.backfill()                          # sweep runs again
        self.assertEqual(len(readinglist.queue()), 1)

    def test_a_filename_date_prefix_beats_the_mtime(self):
        # retros are rsynced, so mtime is when the copy landed, not when it
        # was written — the name carries the truth
        f = Path(self.tmp.name) / "2026-05-17 1009 some-project.md"
        f.write_text("## a\n## b\n", encoding="utf-8")
        self.assertTrue(readinglist._file_created(f, f.stem)
                        .startswith("2026-05-17"))

    def test_unknown_age_counts_as_fresh(self):
        self.assertFalse(readinglist._is_stale(None))
        self.assertFalse(readinglist._is_stale("not a date"))


class PerSessionRetroTests(Base):
    """The provenance system writes a retro PER SESSION, so one busy night can
    produce fifty. Those are the raw material; the aggregates are the read."""

    def _vault(self, *names):
        vault = Path(self.tmp.name) / "vault"
        (vault / "Sessions").mkdir(parents=True, exist_ok=True)
        for n in names:
            (vault / "Sessions" / f"{n}.md").write_text(
                "## a\n## b\n", encoding="utf-8")
        return mock.patch.object(
            readinglist.settings, "get",
            side_effect=lambda k: str(vault) if k == "vault_root" else "")

    def test_a_timestamped_retro_is_raw_material(self):
        for name in ("2026-07-25 0205 vira", "2026-07-25 0205 vira (3)",
                     "2026-07-16 1124 vira — journal-pid-verification"):
            self.assertTrue(readinglist._PER_SESSION_RE.match(name), name)

    def test_the_aggregates_are_the_read(self):
        for name in ("2026-07-25 day vira", "2026-07-25 cross-project",
                     "2026-07-25 morning-dossier"):
            self.assertIsNone(readinglist._PER_SESSION_RE.match(name), name)

    def test_a_busy_night_does_not_flood_the_queue(self):
        names = [f"2026-07-25 020{n} vira" for n in range(5)]
        names += ["2026-07-25 day vira", "2026-07-25 cross-project"]
        with self._vault(*names), \
             mock.patch.object(readinglist, "_rooms", return_value=[]), \
             mock.patch.object(readinglist, "_plans", return_value=[]), \
             mock.patch.object(readinglist, "_is_stale", return_value=False):
            out = readinglist.backfill()
        self.assertEqual(out["added"], 7)
        self.assertEqual(out["queued_new"], 2)      # only the two aggregates
        self.assertEqual(sorted(i["title"] for i in readinglist.queue()),
                         ["2026-07-25 cross-project", "2026-07-25 day vira"])

    def test_a_per_session_retro_stays_findable_and_restorable(self):
        with self._vault("2026-07-25 0205 vira"), \
             mock.patch.object(readinglist, "_rooms", return_value=[]), \
             mock.patch.object(readinglist, "_plans", return_value=[]), \
             mock.patch.object(readinglist, "_is_stale", return_value=False):
            readinglist.backfill()
        done = readinglist.completed()
        self.assertEqual(len(done), 1)
        readinglist.complete(done[0]["id"], done=False)
        self.assertEqual(len(readinglist.queue()), 1)


class StoreTests(Base):
    def test_a_corrupt_store_does_not_take_the_queue_down(self):
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text("{{{ not json", encoding="utf-8")
        self.assertEqual(readinglist.queue(), [])
        it = readinglist.register("A", "dossier", "/a/")
        self.assertEqual(len(readinglist.queue()), 1)
        written = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(written["items"][0]["id"], it["id"])

    def test_queue_is_newest_first(self):
        readinglist.register("old", "dossier", "/a/", created="2026-01-01T00:00:00")
        readinglist.register("new", "dossier", "/b/", created="2026-07-27T00:00:00")
        self.assertEqual([i["title"] for i in readinglist.queue()], ["new", "old"])


class SlugTests(Base):
    def test_slugs_are_always_reading_name_legal(self):
        for text in ("Where we stand — audit!", "2026-07-27 retro", "///",
                     "a" * 200, "Ünicode Ttitle", ""):
            self.assertRegex(readinglist.slugify(text), reading.NAME_RE)


if __name__ == "__main__":
    unittest.main()
