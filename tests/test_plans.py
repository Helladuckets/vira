"""Saved plans — the vault home + registry for Plan-mode output.

A Plan-mode job produces markdown; historically Vira only published it to
the owner's private hosted lab, a no-op on every other install. These tests
cover the universal path: the plan is saved as a vault note (a Vira vault is
created when none is connected), registered with a stable id + name, opens
in-app, and stamps its launching idea with a reopenable reference.

Run: .venv/bin/python -m unittest discover tests
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import plans, session


class TitleSlugTests(unittest.TestCase):
    def test_title_from_heading(self):
        self.assertEqual(plans._extract_title("# My Plan\n\nbody"), "My Plan")

    def test_title_fallback_to_first_line(self):
        self.assertEqual(plans._extract_title("no heading\nmore"), "no heading")

    def test_title_empty_input(self):
        self.assertEqual(plans._extract_title(""), "Untitled plan")

    def test_slugify(self):
        self.assertEqual(plans._slugify("My Great Plan!"), "my-great-plan")
        self.assertEqual(plans._slugify("   "), "plan")


class _VaultCase(unittest.TestCase):
    """A tmp registry + a connected tmp vault, so no real store is touched
    and ensure_vault never needs qocha."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        self.reg = root / "plans.json"
        for p in (
            mock.patch.object(plans, "REG_PATH", self.reg),
            mock.patch.object(
                plans.settings, "get",
                side_effect=lambda k: (str(self.vault) if k == "vault_root"
                                       else "")),
        ):
            p.start()
            self.addCleanup(p.stop)


class SaveTests(_VaultCase):
    def test_save_writes_file_and_registry(self):
        e = plans.save_plan("# Cool Plan\n\nDo the thing.",
                            idea_id="idea_x", job_id="job_y")
        self.assertEqual(e["title"], "Cool Plan")
        self.assertTrue(e["id"].startswith("pl_"))
        self.assertEqual(Path(e["path"]).parent, self.vault / "plans")
        self.assertIn("Do the thing.", Path(e["path"]).read_text())
        self.assertEqual(e["idea_id"], "idea_x")
        reg = json.loads(self.reg.read_text())
        self.assertEqual(reg["plans"][0]["id"], e["id"])

    def test_empty_plan_raises(self):
        with self.assertRaises(ValueError):
            plans.save_plan("   ")

    def test_same_title_does_not_clobber(self):
        a = plans.save_plan("# Same Title\n\nA")
        b = plans.save_plan("# Same Title\n\nB")
        self.assertNotEqual(a["path"], b["path"])
        self.assertTrue(Path(a["path"]).is_file())
        self.assertTrue(Path(b["path"]).is_file())

    def test_list_get_delete_roundtrip(self):
        e = plans.save_plan("# T\n\nbody text")
        lst = plans.list_plans()
        self.assertEqual(lst[0]["id"], e["id"])
        self.assertFalse(lst[0]["missing"])
        got = plans.get_plan(e["id"])
        self.assertIn("body text", got["markdown"])
        with self.assertRaises(KeyError):
            plans.get_plan("pl_nope")
        plans.delete_plan(e["id"])
        self.assertEqual(plans.list_plans(), [])
        self.assertFalse(Path(e["path"]).exists())
        with self.assertRaises(KeyError):
            plans.delete_plan(e["id"])

    def test_missing_file_is_flagged(self):
        e = plans.save_plan("# T\n\nbody")
        Path(e["path"]).unlink()
        self.assertTrue(plans.list_plans()[0]["missing"])
        self.assertTrue(plans.get_plan(e["id"])["missing"])


class EnsureVaultUnsetTests(unittest.TestCase):
    def test_creates_vira_vault_when_none_connected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        default = Path(tmp.name) / "vira-vault"

        def fake_setup(path, init=False):
            Path(path).mkdir(parents=True, exist_ok=True)

        with mock.patch.object(plans.settings, "get", return_value=""), \
             mock.patch.object(plans, "DEFAULT_VAULT", default), \
             mock.patch("server.onboard.vault_setup",
                        side_effect=fake_setup) as vs:
            root = plans.ensure_vault()
        self.assertEqual(root, default)
        self.assertTrue(default.is_dir())
        vs.assert_called_once()

    def test_falls_back_to_bare_dir_when_qocha_missing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        default = Path(tmp.name) / "vira-vault"
        with mock.patch.object(plans.settings, "get", return_value=""), \
             mock.patch.object(plans, "DEFAULT_VAULT", default), \
             mock.patch("server.onboard.vault_setup",
                        side_effect=RuntimeError("no qocha CLI")), \
             mock.patch("server.onboard.config_set") as cs:
            root = plans.ensure_vault()
        self.assertEqual(root, default)
        self.assertTrue(default.is_dir())
        cs.assert_called_once()


class FinalizeTests(_VaultCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(session, "_publish_plan", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_finalize_saves_and_returns(self):
        res = session._finalize_plan("# Plan A\n\nbody",
                                     idea_id="idea_1", job_id="job_1")
        self.assertIsNotNone(res["plan_id"])
        self.assertEqual(res["title"], "Plan A")
        self.assertIsNone(res["url"])
        self.assertEqual(len(plans.list_plans()), 1)

    def test_finalize_records_lab_url_when_hook_publishes(self):
        with mock.patch.object(session, "_publish_plan",
                               return_value="https://x/plans/y.html"):
            res = session._finalize_plan("# Plan B\n\nbody")
        self.assertEqual(res["url"], "https://x/plans/y.html")
        self.assertEqual(plans.get_plan(res["plan_id"])["lab_url"],
                         "https://x/plans/y.html")

    def test_finalize_never_raises_on_empty_plan(self):
        res = session._finalize_plan("   ")
        self.assertIsNone(res["plan_id"])


class PassiveGuardTests(_VaultCase):
    def setUp(self):
        super().setUp()
        # even with the hook "present", passive must publish/save nothing
        p = mock.patch.object(session, "_publish_plan",
                              return_value="https://x/plans/y.html")
        p.start()
        self.addCleanup(p.stop)

    def test_finalize_is_noop_on_a_passive_instance(self):
        with mock.patch.dict("os.environ", {"VIRA_PASSIVE": "1"}):
            res = session._finalize_plan("# P\n\nbody",
                                         idea_id="i", job_id="j")
        self.assertIsNone(res["plan_id"])
        self.assertIsNone(res["url"])
        self.assertEqual(plans.list_plans(), [])     # real vault untouched


class PlanRefTests(unittest.TestCase):
    def test_plain_title(self):
        self.assertEqual(
            session._plan_ref({"plan_id": "pl_9", "title": "My Plan"}),
            "[plan pl_9: My Plan]")

    def test_bracket_in_title_cannot_truncate_the_link(self):
        ref = session._plan_ref({"plan_id": "pl_1", "title": "Fix [bug] now"})
        # no interior ] before the closing one -> the client regex captures
        # the whole title, not a truncated head
        self.assertEqual(ref, "[plan pl_1: Fix [bug) now]")
        self.assertEqual(ref.count("]"), 1)


class MarkIdeaTests(unittest.TestCase):
    def _capture(self, job, ok=True, interrupted=False):
        calls = {}

        def fake_update(idea_id, **kw):
            calls["idea_id"] = idea_id
            calls.update(kw)

        with mock.patch.object(session.ideas, "update",
                               side_effect=fake_update):
            session._mark_idea(job, ok, interrupted=interrupted)
        return calls

    def test_plan_success_stamps_reopenable_ref_and_stays_open(self):
        calls = self._capture({
            "id": "job_abcdef12", "idea_id": "idea_9", "publish_plan": True,
            "plan": {"plan_id": "pl_123", "title": "My Plan", "url": None},
            "output": ""})
        self.assertEqual(calls["idea_id"], "idea_9")
        self.assertIn("[plan pl_123: My Plan]", calls["note"])
        self.assertNotIn("status", calls)      # a plan does not close the idea

    def test_plan_without_saved_entry_falls_back_to_terminal(self):
        calls = self._capture({
            "id": "job_x", "idea_id": "idea_9", "publish_plan": True,
            "plan": {"plan_id": None, "title": None, "url": None},
            "output": ""})
        self.assertIn("see terminal", calls["note"])

    def test_implement_success_marks_done(self):
        calls = self._capture({
            "id": "job_x", "idea_id": "idea_9", "publish_plan": False,
            "output": ""})
        self.assertEqual(calls["status"], "done")

    def test_failure_keeps_open_with_failure_note(self):
        calls = self._capture({
            "id": "job_x", "idea_id": "idea_9", "publish_plan": True,
            "output": ""}, ok=False)
        self.assertNotIn("status", calls)
        self.assertIn("failed", calls["note"])


if __name__ == "__main__":
    unittest.main()
