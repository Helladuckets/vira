"""Business-sender detection (triage) and remote-event conflict exclusion
(brief). Both are pure/deterministic paths: business_signals classifies from
handle shape + verdict wording + message content (the chat.db probe is passed
in as texts here), and _flag_conflicts must never pair a remote event against
an in-person one.

Run: .venv/bin/python -m unittest discover tests
"""
import unittest
from unittest import mock

from server import brief, triage


class BusinessSignals(unittest.TestCase):
    def test_toll_free_number_flags(self):
        sig, _ = triage.business_signals("+18005550100")
        self.assertIn("toll-free number", sig)

    def test_toll_free_with_sms_forward_suffix(self):
        sig, _ = triage.business_signals("+18335550142(smsft)")
        self.assertIn("toll-free number", sig)

    def test_short_code_flags(self):
        sig, _ = triage.business_signals("29900")
        self.assertIn("short-code sender", sig)

    def test_regular_mobile_number_is_clean(self):
        sig, guess = triage.business_signals("+12125550123")
        self.assertEqual(sig, [])
        self.assertEqual(guess, "")

    def test_email_handle_is_clean(self):
        sig, _ = triage.business_signals("friend@example.com")
        self.assertEqual(sig, [])

    def test_verdict_wording_flags(self):
        v = {"relationship": "Example Bank loan-application notifications",
             "evidence": "Automated (closing docs)."}
        sig, _ = triage.business_signals("+12125550123", v)
        self.assertIn("enrichment: automated/notifications", sig)

    def test_personal_verdict_does_not_flag(self):
        v = {"relationship": "friend from the neighborhood",
             "evidence": "'so nice seeing you Saturday'"}
        sig, _ = triage.business_signals("+12125550123", v)
        self.assertEqual(sig, [])

    def test_message_content_flags_and_names_company(self):
        texts = ["Hi there, this is an automated message from U.S. Bank. "
                 "A new document package has been added to your loan "
                 "application."]
        sig, guess = triage.business_signals("+12125550123", None, texts)
        self.assertIn("message content: automated", sig)
        self.assertEqual(guess, "U.S. Bank")

    def test_alerts_prefix_names_company(self):
        texts = ["Example Bank Alerts: 983634 is your code. Do not share "
                 "this code with someone contacting you."]
        sig, guess = triage.business_signals("29900", None, texts)
        self.assertIn("message content: automated", sig)
        self.assertEqual(guess, "Example Bank")

    def test_personal_texts_do_not_flag(self):
        texts = ["Hope your week is going well, so nice seeing you Saturday!",
                 "Would it be okay if we moved our call to tomorrow?"]
        sig, guess = triage.business_signals("+12125550123", None, texts)
        self.assertEqual(sig, [])
        self.assertEqual(guess, "")

    def test_candidates_annotates_and_sinks_businesses(self):
        people = {"people": [
            {"id": "p_test0000biz1", "name": "(unidentified)",
             "handles": {"imessage": [], "emails": [],
                         "phones10": ["8005550100"]},
             "master_tier": "D-skip", "activity": {"imsg_n": 2}},
            {"id": "p_test0000per1", "name": "(unidentified)",
             "handles": {"imessage": ["+12125550123"], "emails": [],
                         "phones10": ["2125550123"]},
             "master_tier": "C-review", "activity": {"imsg_n": 9}},
        ]}
        with mock.patch.object(triage.crm, "_load",
                               return_value={"people": people["people"]}), \
             mock.patch.object(triage, "_verdicts", return_value=[]), \
             mock.patch.object(triage, "_dismissed", return_value=set()), \
             mock.patch.object(triage, "_recent_inbound", return_value=[]):
            out = triage.candidates()
        self.assertEqual([c["business"] for c in out], [False, True])
        self.assertEqual(out[-1]["person_id"], "p_test0000biz1")
        self.assertIn("toll-free number", out[-1]["business_signals"])


def _ev(title, start, end, remote=False, all_day=False, birthday=False):
    return {"title": title, "all_day": all_day, "birthday": birthday,
            "start": start, "end": end, "conflict": False, "remote": remote}


class RemoteConflicts(unittest.TestCase):
    def test_remote_does_not_conflict_with_in_person(self):
        evs = brief._flag_conflicts([
            _ev("Weekly video call", "2026-07-16T16:00", "2026-07-16T17:00",
                remote=True),
            _ev("Sitter at the house", "2026-07-16T16:00", "2026-07-16T18:00"),
        ])
        self.assertFalse(any(e["conflict"] for e in evs))

    def test_two_in_person_events_still_conflict(self):
        evs = brief._flag_conflicts([
            _ev("Dentist", "2026-07-16T16:00", "2026-07-16T17:00"),
            _ev("School pickup", "2026-07-16T16:30", "2026-07-16T17:30"),
        ])
        self.assertTrue(all(e["conflict"] for e in evs))

    def test_two_remote_events_still_conflict(self):
        evs = brief._flag_conflicts([
            _ev("Video call A", "2026-07-16T16:00", "2026-07-16T17:00",
                remote=True),
            _ev("Video call B", "2026-07-16T16:30", "2026-07-16T17:30",
                remote=True),
        ])
        self.assertTrue(all(e["conflict"] for e in evs))

    def test_missing_remote_key_treated_as_in_person(self):
        a = _ev("Old-shape event", "2026-07-16T16:00", "2026-07-16T17:00")
        del a["remote"]
        evs = brief._flag_conflicts([
            a, _ev("Errand", "2026-07-16T16:30", "2026-07-16T17:30"),
        ])
        self.assertTrue(all(e["conflict"] for e in evs))

    def test_title_config_marks_remote(self):
        with mock.patch.object(brief.settings, "get",
                               return_value=["AI Geek Out Weekly"]):
            rts = brief._remote_titles()
        self.assertTrue(brief._is_remote("AI Geek Out Weekly", rts))
        self.assertTrue(brief._is_remote("ai geek out weekly (moved)", rts))
        self.assertFalse(brief._is_remote("Sitter at the house", rts))

    def test_blank_config_entries_ignored(self):
        with mock.patch.object(brief.settings, "get",
                               return_value=["", "  ", None]):
            self.assertEqual(brief._remote_titles(), [])


class CompanyExclusion(unittest.TestCase):
    """class_hint "company" (a non-person entity like a bank's automated
    number) must never surface as owed-a-reply or going-quiet. class_hint
    "business" is a PERSON in a business relationship and stays in."""

    def test_is_company(self):
        self.assertTrue(brief._is_company({"class_hint": "company"}))
        self.assertFalse(brief._is_company({"class_hint": "business"}))
        self.assertFalse(brief._is_company({}))
        self.assertFalse(brief._is_company(None))

    def test_going_quiet_skips_companies(self):
        people = [
            {"id": "p_test0000comp", "name": "Example Bank",
             "class_hint": "company", "profile_tier": "active",
             "handles": {"imessage": ["+18005550100"]}},
            {"id": "p_test0000pers", "name": "Casey Example",
             "class_hint": "business", "profile_tier": "active",
             "handles": {"imessage": ["+12125550123"]}},
        ]
        stale = "2026-01-01T09:00:00"
        with mock.patch.object(brief.crm, "_load",
                               return_value={"people": people}), \
             mock.patch.object(brief.crm, "_last_contact",
                               return_value=stale), \
             mock.patch.object(brief, "_live_imsg_last", return_value={}):
            out = brief._going_quiet()
        self.assertEqual([q["person_id"] for q in out], ["p_test0000pers"])

    def test_recent_mail_skips_companies(self):
        by_id = {"p_test0000comp": {"class_hint": "company"},
                 "p_test0000pers": {"class_hint": "business"}}
        import datetime as dt
        fresh = dt.datetime.now().astimezone().isoformat()
        items = [
            {"channel": "email", "person_id": "p_test0000comp",
             "person_name": "Example Bank", "when": fresh},
            {"channel": "email", "person_id": "p_test0000pers",
             "person_name": "Casey Example", "when": fresh},
        ]
        with mock.patch.object(brief.crm, "_load",
                               return_value={"by_id": by_id}):
            out = brief._recent_mail(items)
        self.assertEqual([m["person_id"] for m in out], ["p_test0000pers"])


if __name__ == "__main__":
    unittest.main()
