"""POST /api/demo/reset — the sandbox badge's "Reset to a new user".

The button has to hand back what a stranger's first boot actually is: the
CURRENT code, and nothing left over from the last walk. Before this it only
forgot four ui-state keys, so a sandbox whose owner had pasted an API key or
imported contacts came back already set up — the welcome took its
already-connected path and the four provider tiles never rendered.

Three rules these pin:
  * the wipe is QUEUED for the relaunch loop, never done in-process (data/
    holds open sqlite handles belonging to this very server);
  * an ordinary update refusal is reported and the reset continues, while a
    DEPENDENCY failure stops it (new code on old deps must not be restarted
    onto — the rule update.apply() already holds);
  * with no loop supervising, the reset degrades to the shallow behaviour and
    SAYS SO, rather than reporting a reset it did not perform.

Run: .venv/bin/python -m unittest discover tests
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from server import main, update


class DemoResetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.maint = Path(self.tmp.name) / ".maintenance"
        self.store = {}                       # the sandbox's namespaced keys

        def _get(_svc, acct=None):
            return self.store.get(acct, "")

        def _delete(_svc, acct=None):
            self.store.pop(acct, None)

        for target, attr, new in (
            (main.settings, "sandboxed", lambda: True),
            (main.settings, "sandbox_loop", lambda: str(self.maint)),
            (main.uistate, "forget", lambda keys: list(keys)),
            (main.secrets, "get", _get),
            (main.secrets, "delete", _delete),
            (main.update, "pull", lambda: {"updated": False,
                                           "note": "already up to date"}),
        ):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)
        # Never let a test actually exit the interpreter.
        t = mock.patch.object(main.threading, "Timer")
        self.timer = t.start()
        self.addCleanup(t.stop)

    def reset(self, update_=True):
        return main.api_demo_reset(main.DemoResetReq(update=update_))


class SandboxOnly(DemoResetBase):
    def test_refused_off_a_sandbox(self):
        # Live and a branch test instance both carry a real arrangement and a
        # real connected account; an endpoint that wipes onboarding must not
        # be reachable there at all.
        with mock.patch.object(main.settings, "sandboxed", lambda: False):
            with self.assertRaises(HTTPException) as ctx:
                self.reset()
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertFalse(self.maint.exists())


class TheWipeIsQueued(DemoResetBase):
    def test_wipe_is_written_for_the_loop_and_the_server_exits(self):
        out = self.reset()
        self.assertTrue(out["restarting"])
        self.assertIn("wipe", self.maint.read_text(encoding="utf-8"))
        self.timer.assert_called_once()

    def test_without_a_loop_it_degrades_and_says_so(self):
        with mock.patch.object(main.settings, "sandbox_loop", lambda: ""):
            out = self.reset()
        self.assertFalse(out["restarting"])
        self.assertIn("sandbox.sh replay", out["note"])
        self.timer.assert_not_called()      # nothing would bring it back


class LeftoversAreCleared(DemoResetBase):
    def test_a_pasted_key_does_not_survive_the_reset(self):
        # The key lives in the secrets ladder — on a Mac, the machine
        # Keychain — so a data/ wipe alone leaves the app connected while its
        # config says nothing. That half-reset is what the owner reported.
        self.store["xai"] = "sk-real-key"
        out = self.reset()
        self.assertIn("xai", out["keys_cleared"])
        self.assertEqual(self.store, {})

    def test_only_this_sandboxs_namespace_is_touched(self):
        # keychain_service prefixes every service name from
        # VIRA_KEYCHAIN_PREFIX, so live secrets are unreachable by construction.
        seen = []
        with mock.patch.object(main.secrets, "delete",
                               lambda svc, acct=None: seen.append(svc)), \
             mock.patch.object(main.settings, "keychain_service",
                               lambda n: "sandbox-" + n):
            self.reset()
        self.assertTrue(seen)
        self.assertTrue(all(s.startswith("sandbox-") for s in seen))

    def test_the_welcome_flag_is_forgotten(self):
        out = self.reset()
        self.assertIn("vira-firstrun-done", out["forgot"])


class TheUpdateHalf(DemoResetBase):
    def test_it_pulls_by_default(self):
        with mock.patch.object(main.update, "pull",
                               return_value={"updated": True,
                                             "sha": "abc1234"}) as pull:
            out = self.reset()
        pull.assert_called_once()
        self.assertEqual(out["update"]["sha"], "abc1234")

    def test_an_ordinary_refusal_does_not_block_the_reset(self):
        # No network, no remote, a dirty tree: worth reporting, never worth
        # refusing to hand back a clean sandbox over.
        with mock.patch.object(main.update, "pull",
                               side_effect=ValueError("not a git clone")):
            out = self.reset()
        self.assertTrue(out["restarting"])
        self.assertIn("not a git clone", out["update"]["note"])
        self.assertTrue(self.maint.exists())

    def test_a_dependency_failure_stops_everything(self):
        # The checkout already moved; restarting onto it with old deps is the
        # one failure update.apply() refuses, and this must refuse it too.
        with mock.patch.object(main.update, "pull",
                               side_effect=update.DepsError("pip failed")):
            with self.assertRaises(HTTPException) as ctx:
                self.reset()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertFalse(self.maint.exists())
        self.timer.assert_not_called()

    def test_update_can_be_skipped(self):
        with mock.patch.object(main.update, "pull") as pull:
            out = self.reset(update_=False)
        pull.assert_not_called()
        self.assertIsNone(out["update"])
        self.assertTrue(out["restarting"])


if __name__ == "__main__":
    unittest.main()
