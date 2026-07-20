"""Native-tool tests: preamble content, tool registry shape, the calendar
range/merge/dedup logic, CRM and mail rendering, and the session gate's
auto-allow of the read-only vira tools. No SDK client is connected and no
real data store is touched — module boundaries are mocked.

Run: .venv/bin/python -m unittest discover tests
"""
import datetime as dt
import unittest
from unittest import mock

from server import session, viratools


def _ev(title, offset_days=0, hour=9, cal="Home", **over):
    start = (dt.datetime.now().replace(hour=hour, minute=0, second=0,
                                       microsecond=0)
             + dt.timedelta(days=offset_days))
    e = {"title": title, "calendar": cal, "family": False, "birthday": False,
         "all_day": False, "start": start.isoformat(),
         "end": (start + dt.timedelta(hours=1)).isoformat(),
         "start_hm": start.strftime("%-I:%M %p"),
         "end_hm": (start + dt.timedelta(hours=1)).strftime("%-I:%M %p"),
         "conflict": False}
    e.update(over)
    return e


class TestRegistryShape(unittest.TestCase):
    def test_tool_names_derive_from_specs(self):
        self.assertEqual(viratools.TOOL_NAMES,
                         [f"mcp__vira__{n}" for n, *_ in viratools.TOOL_SPECS])
        self.assertIn("mcp__vira__calendar", viratools.TOOL_NAMES)

    def test_sdk_server_builds_and_caches(self):
        if not viratools.SDK_AVAILABLE:
            self.skipTest("claude-agent-sdk not installed")
        srv = viratools.sdk_server()
        self.assertIsNotNone(srv)
        self.assertIs(srv, viratools.sdk_server())

    def test_runner_auto_allows_vira_tools(self):
        # the auto-allow guarantee moved into the detached runner
        import json
        import tempfile
        from pathlib import Path

        from server import runner as runner_mod
        with tempfile.TemporaryDirectory() as tmp:
            jdir = Path(tmp) / "tjob"
            jdir.mkdir(parents=True, exist_ok=True)
            (jdir / "job.json").write_text(json.dumps(
                {"id": "t" * 12, "prompt": "p", "cwd": "/tmp",
                 "mode": "interactive", "auto_allow": ["Read"],
                 "permission_timeout": 600}))
            r = runner_mod.Runner(jdir)
            try:
                self.assertTrue(set(viratools.TOOL_NAMES) <= r.auto_allow)
            finally:
                r.out.close()


class TestPreamble(unittest.TestCase):
    def test_native_mentions_tools_legacy_does_not(self):
        native, legacy = viratools.preamble(), viratools.preamble(False)
        self.assertIn("mcp__vira__", native)
        self.assertNotIn("mcp__vira__", legacy)

    def test_both_carry_api_and_restart_guard(self):
        for p in (viratools.preamble(), viratools.preamble(False)):
            self.assertIn("localhost:8377", p)
            self.assertIn("Never restart", p)
            self.assertIn("nyc.durham.vira", p)


class TestCalendar(unittest.TestCase):
    def test_renders_local_events_and_clamps_days(self):
        cal_db = mock.Mock()
        cal_db.exists.return_value = True
        with mock.patch.object(viratools.brief, "CAL_DB", cal_db), \
             mock.patch.object(viratools.brief, "_occurrences",
                               return_value=[_ev("Doctor — Dr. Katz")]), \
             mock.patch.object(viratools.brief, "_graph_accounts",
                               return_value=[]):
            out = viratools._calendar_text(99)
        self.assertIn("next 31 day(s)", out)
        self.assertIn("Doctor — Dr. Katz", out)
        self.assertIn("(Home)", out)

    def test_merges_graph_and_dedups_mirrored(self):
        local = _ev("Standup", cal="Work mirror")
        mirrored = {"title": "Standup", "start": local["start"],
                    "end": local["end"], "all_day": False}
        fresh = {"title": "Board call",
                 "start": local["start"].replace("T09", "T14"),
                 "end": local["end"].replace("T10", "T15"), "all_day": False}
        cal_db = mock.Mock()
        cal_db.exists.return_value = True
        with mock.patch.object(viratools.brief, "CAL_DB", cal_db), \
             mock.patch.object(viratools.brief, "_occurrences",
                               return_value=[local]), \
             mock.patch.object(viratools.brief, "_graph_accounts",
                               return_value=["owner@work.com"]), \
             mock.patch.object(viratools.msgraph, "calendar_events",
                               return_value=[mirrored, fresh]):
            out = viratools._calendar_text(2)
        self.assertEqual(out.count("Standup"), 1)     # mirrored deduped
        self.assertIn("Board call", out)
        self.assertIn("[work]", out)

    def test_degrades_when_stores_unavailable(self):
        cal_db = mock.Mock()
        cal_db.exists.return_value = False
        with mock.patch.object(viratools.brief, "CAL_DB", cal_db), \
             mock.patch.object(viratools.brief, "_graph_accounts",
                               side_effect=[["m@w.com"]]), \
             mock.patch.object(viratools.msgraph, "calendar_events",
                               side_effect=RuntimeError("token expired")):
            out = viratools._calendar_text(7)
        self.assertIn("No events found", out)
        self.assertIn("local calendar store unavailable", out)
        self.assertIn("token expired", out)


class TestCrm(unittest.TestCase):
    def test_renders_dossier(self):
        person = {"id": "p_1", "name": "Steve Grossman", "tier": 1,
                  "relationship_class": "friend", "imsg_last": "2026-07-10",
                  "imsg_n": 42, "email_n": 3, "class_hint": None}
        full = {"master": {"company": "Acme", "title": "CEO"},
                "profile": {"hooks": [{"text": "ask about the snowmobile"}],
                            "open_loops": ["dinner plan"]}}
        with mock.patch.object(viratools.crm, "search_people",
                               return_value=[person]), \
             mock.patch.object(viratools.crm, "get_person",
                               return_value=full):
            out = viratools._crm_text("steve")
        self.assertIn("Steve Grossman", out)
        self.assertIn("Acme", out)
        self.assertIn("snowmobile", out)
        self.assertIn("dinner plan", out)

    def test_no_match(self):
        with mock.patch.object(viratools.crm, "search_people",
                               return_value=[]):
            self.assertIn("No CRM match", viratools._crm_text("nobody"))


class TestMailAndMedia(unittest.TestCase):
    def test_mail_requires_accounts(self):
        with mock.patch.object(viratools, "_accounts", return_value=[]):
            self.assertIn("No mail accounts", viratools._mail_text("x", 5))

    def test_mail_one_account_failure_does_not_kill_others(self):
        accounts = [{"email": "a@work.com", "type": "graph"},
                    {"email": "b@example.com", "host": "imap.gmail.com"}]
        with mock.patch.object(viratools, "_accounts",
                               return_value=accounts), \
             mock.patch.object(viratools, "_mail_graph",
                               return_value=["  2026-07-01 · x · hit — ok"]), \
             mock.patch.object(viratools, "_mail_imap",
                               side_effect=RuntimeError("login failed")):
            out = viratools._mail_text("invoice", 5)
        self.assertIn("hit — ok", out)
        self.assertIn("unavailable (login failed)", out)

    def test_media_requires_query(self):
        self.assertIn("error", viratools._media_text("", None, 5))


if __name__ == "__main__":
    unittest.main()
