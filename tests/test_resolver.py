"""The triage contact resolver: deterministic referrer extraction, referral
group intersection, grounded-or-held name verification, and the two routes
(/api/triage/resolve is read-only; /api/crm/add writes the referral fact).

The model call (suggest.complete) is always mocked — these tests cover the
deterministic scaffolding around it. Fixtures are fully synthetic and use the
555-01xx reserved-fiction phone block so the PII guard stays green.

Run: .venv/bin/python -m unittest discover tests
"""
import unittest
from unittest import mock

from server import resolver


def _ev(thread=None, group_msgs=None, cards=None, referrer="",
        referrer_pids=None, ambiguous=False, candidates=None, sources=None):
    return {"thread": thread or [], "group_msgs": group_msgs or [],
            "cards": cards or [], "referrer": referrer,
            "referrer_pids": referrer_pids or set(), "ambiguous": ambiguous,
            "candidates": candidates or [], "sources": sources or [],
            "verdict": {}}


class ReferrerExtraction(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(resolver, "_owner_first", return_value="owner")
        p.start()
        self.addCleanup(p.stop)

    def test_introd_by(self):
        self.assertEqual(
            resolver.referrer_from_text("intro'd by Eric (Mar 2026), offered "
                                        "mortgage-broker referral"), "Eric")

    def test_referred_by_full_name(self):
        self.assertEqual(
            resolver.referrer_from_text("professional — referred by Sarah Chen"),
            "Sarah Chen")

    def test_connect_us_phrasing(self):
        self.assertEqual(
            resolver.referrer_from_text("Eric was so nice to connect us"), "Eric")

    def test_via_intro(self):
        self.assertEqual(
            resolver.referrer_from_text("met them via Dana's intro last spring"),
            "Dana")

    def test_no_referral_phrase_returns_empty(self):
        # ordinary evidence with capitalized non-names must not false-positive
        self.assertEqual(
            resolver.referrer_from_text("saw them Saturday, family Brooklyn "
                                        "property under contract"), "")

    def test_day_after_by_is_not_a_referrer(self):
        self.assertEqual(resolver.referrer_from_text("send the docs by Friday"), "")

    def test_owner_name_is_not_a_referrer(self):
        # "connected by Owner" — the owner is the recipient, not the referrer
        self.assertEqual(resolver.referrer_from_text("connected by Owner"), "")

    def test_name_matches_first_name(self):
        self.assertTrue(resolver._name_matches("Eric", "Eric Vale"))
        self.assertTrue(resolver._name_matches("Sarah Chen", "Sarah Chen"))

    def test_name_matches_rejects_substring(self):
        # whole-word only: "Eric" must not match inside "Frederic"
        self.assertFalse(resolver._name_matches("Eric", "Frederic Jones"))


class ReferrerResolution(unittest.TestCase):
    def test_single_name_match(self):
        people = [{"id": "p_res00000001", "name": "Eric Vale"}]
        with mock.patch.object(resolver.crm, "search_people",
                               return_value=people):
            pids, cands, ambig = resolver._resolve_referrer("Eric")
        self.assertEqual(pids, {"p_res00000001"})
        self.assertFalse(ambig)
        self.assertEqual(cands, [])

    def test_two_matches_are_ambiguous(self):
        people = [{"id": "p_res00000001", "name": "Eric Vale"},
                  {"id": "p_res00000002", "name": "Eric Rowe"}]
        with mock.patch.object(resolver.crm, "search_people",
                               return_value=people):
            pids, cands, ambig = resolver._resolve_referrer("Eric")
        self.assertEqual(pids, {"p_res00000001", "p_res00000002"})
        self.assertTrue(ambig)
        self.assertEqual(set(cands), {"Eric Vale", "Eric Rowe"})

    def test_handle_only_match_is_dropped_when_a_name_matches(self):
        # search_people also matches handles/emails; a non-name hit is ignored
        people = [{"id": "p_res00000001", "name": "Eric Vale"},
                  {"id": "p_res00000009", "name": "Someone Else"}]
        with mock.patch.object(resolver.crm, "search_people",
                               return_value=people):
            pids, _c, ambig = resolver._resolve_referrer("Eric")
        self.assertEqual(pids, {"p_res00000001"})
        self.assertFalse(ambig)

    def test_referral_groups_filters_by_participant(self):
        groups = [
            {"chat_ids": [1], "participants": [
                {"person_id": "p_res00000001"}, {"person_id": "p_unknown0001"}]},
            {"chat_ids": [2], "participants": [
                {"person_id": "p_other00001"}, {"person_id": "p_unknown0001"}]},
        ]
        with mock.patch.object(resolver.imessage, "groups_for_person",
                               return_value=groups):
            out = resolver._referral_groups("p_unknown0001", {"p_res00000001"})
        self.assertEqual([g["chat_ids"] for g in out], [[1]])

    def test_referral_groups_empty_without_referrer(self):
        self.assertEqual(resolver._referral_groups("p_x", set()), [])
        self.assertEqual(resolver._referral_groups(None, {"p_res00000001"}), [])


class Grounding(unittest.TestCase):
    def test_grounded_by_shared_card(self):
        ev = _ev(cards=["BEGIN:VCARD\nFN:John Merritt\nTEL:+12125550142\nEND:VCARD"])
        self.assertTrue(resolver._grounded("John Merritt", ev, ""))

    def test_grounded_by_typed_memory(self):
        self.assertTrue(resolver._grounded("John Merritt", _ev(),
                                           "his name is John Merritt"))

    def test_partial_name_is_held(self):
        # thread only ever says "John"; the surname is unsupported -> held
        ev = _ev(thread=[("them", "hey it's John, good to connect")])
        self.assertFalse(resolver._grounded("John Merritt", ev, ""))

    def test_empty_name_not_grounded(self):
        self.assertFalse(resolver._grounded("", _ev(cards=["FN:John Merritt"]), ""))

    def test_finalize_grounded_keeps_confidence(self):
        data = {"full_name": "John Merritt", "confidence": "high",
                "class_hint": "business", "evidence": "from Eric's card",
                "referral_fact": "Introduced to the owner by Eric Vale"}
        ev = _ev(cards=["FN:John Merritt"], referrer="Eric Vale")
        out = resolver._finalize(data, ev, "")
        self.assertEqual(out["name"], "John Merritt")
        self.assertFalse(out["held"])
        self.assertEqual(out["confidence"], "high")
        self.assertEqual(out["class_hint"], "business")
        self.assertEqual(out["fact"], "Introduced to the owner by Eric Vale")
        self.assertEqual(out["referrer"], "Eric Vale")

    def test_finalize_ungrounded_is_held_and_low(self):
        data = {"full_name": "John Merritt", "confidence": "high"}
        out = resolver._finalize(data, _ev(), "")
        self.assertTrue(out["held"])
        self.assertEqual(out["confidence"], "low")

    def test_finalize_first_name_only(self):
        data = {"first_name": "John", "full_name": ""}
        ev = _ev(thread=[("them", "it's John")])
        out = resolver._finalize(data, ev, "")
        self.assertEqual(out["name"], "")
        self.assertEqual(out["first_name"], "John")
        self.assertFalse(out["held"])

    def test_finalize_rejects_bad_class_hint(self):
        out = resolver._finalize({"full_name": "", "class_hint": "boss"}, _ev(), "")
        self.assertIsNone(out["class_hint"])


class ResolveEndToEnd(unittest.TestCase):
    def test_resolve_returns_grounded_proposal(self):
        ev = _ev(cards=["FN:John Merritt"], referrer="Eric Vale",
                 sources=["cards", "referral"])
        raw = ('{"full_name": "John Merritt", "first_name": "", '
               '"class_hint": "business", "confidence": "high", '
               '"evidence": "printed on the contact card Eric shared", '
               '"referral_fact": "Introduced to the owner by Eric Vale (Mar 2026)"}')
        with mock.patch.object(resolver, "gather_evidence", return_value=ev), \
             mock.patch.object(resolver.suggest, "complete", return_value=raw):
            out = resolver.resolve("+12125550142", "p_unknown0001", "")
        self.assertEqual(out["name"], "John Merritt")
        self.assertFalse(out["held"])
        self.assertEqual(out["fact"],
                         "Introduced to the owner by Eric Vale (Mar 2026)")
        self.assertEqual(out["sources"], ["cards", "referral"])

    def test_resolve_holds_unsupported_guess(self):
        raw = '{"full_name": "Made Upname", "confidence": "high"}'
        with mock.patch.object(resolver, "gather_evidence", return_value=_ev()), \
             mock.patch.object(resolver.suggest, "complete", return_value=raw):
            out = resolver.resolve("+12125550142", None, "")
        self.assertTrue(out["held"])
        self.assertEqual(out["confidence"], "low")


class Routes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.main = main
        cls.client = TestClient(main.app)

    def test_resolve_route_returns_proposal(self):
        proposal = {"name": "John Merritt", "held": False, "sources": ["cards"]}
        with mock.patch.object(self.main.resolver, "resolve",
                               return_value=proposal) as m:
            r = self.client.post("/api/triage/resolve",
                                 json={"handle": "+12125550142",
                                       "person_id": "p_unknown0001",
                                       "memory": "Eric introduced us"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "John Merritt")
        m.assert_called_once_with("+12125550142", "p_unknown0001",
                                  "Eric introduced us")

    def test_resolve_route_502_on_failure(self):
        with mock.patch.object(self.main.resolver, "resolve",
                               side_effect=RuntimeError("model down")):
            r = self.client.post("/api/triage/resolve",
                                 json={"handle": "+12125550142"})
        self.assertEqual(r.status_code, 502)

    def test_add_writes_referral_fact(self):
        person = {"id": "p_res00000010", "name": "John Merritt"}
        with mock.patch.object(self.main.triage, "add_person",
                               return_value=person), \
             mock.patch.object(self.main.crm, "add_fact") as fact:
            r = self.client.post("/api/crm/add", json={
                "name": "John Merritt", "handles": ["+12125550142"],
                "person_id": "p_unknown0001",
                "fact": "Introduced to the owner by Eric Vale"})
        self.assertEqual(r.status_code, 200)
        fact.assert_called_once_with("p_res00000010",
                                     "Introduced to the owner by Eric Vale")

    def test_add_without_fact_writes_no_fact(self):
        person = {"id": "p_res00000011", "name": "Casey Rowe"}
        with mock.patch.object(self.main.triage, "add_person",
                               return_value=person), \
             mock.patch.object(self.main.crm, "add_fact") as fact:
            r = self.client.post("/api/crm/add", json={
                "name": "Casey Rowe", "handles": ["+12125550143"]})
        self.assertEqual(r.status_code, 200)
        fact.assert_not_called()

    def test_add_fact_failure_never_fails_the_add(self):
        person = {"id": "p_res00000012", "name": "John Merritt"}
        with mock.patch.object(self.main.triage, "add_person",
                               return_value=person), \
             mock.patch.object(self.main.crm, "add_fact",
                               side_effect=RuntimeError("profile locked")):
            r = self.client.post("/api/crm/add", json={
                "name": "John Merritt", "handles": ["+12125550142"],
                "fact": "Introduced by Eric Vale"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["added"])


class TriageReferralHint(unittest.TestCase):
    """A person candidate whose evidence names a referrer carries a
    referral_hint (so the client auto-resolves it); a business never does."""

    def _candidates(self, verdict):
        from server import triage
        people = [{"id": "p_res00000020", "name": "(unidentified)",
                   "handles": {"imessage": ["+12125550144"], "emails": [],
                               "phones10": ["2125550144"]},
                   "master_tier": "C-review", "activity": {"imsg_n": 5}}]
        with mock.patch.object(triage.crm, "_load",
                               return_value={"people": people}), \
             mock.patch.object(triage, "_verdicts", return_value=[verdict]), \
             mock.patch.object(triage, "_dismissed", return_value=set()), \
             mock.patch.object(triage, "_recent_inbound", return_value=[]), \
             mock.patch.object(triage.crm, "resolve_handle", return_value=None), \
             mock.patch("server.companion.unknown_senders", return_value=[]):
            return triage.candidates()

    def test_referral_hint_present(self):
        out = self._candidates({
            "handle": "+12125550144", "confirmed_name": "",
            "relationship": "real-estate financing contact",
            "evidence": "intro'd by Eric (Mar 2026), offered a referral",
            "contact_worthy": "yes", "action": "needs_name"})
        c = next(c for c in out if c["person_id"] == "p_res00000020")
        self.assertEqual(c["referral_hint"], "Eric")

    def test_no_referral_hint_without_phrase(self):
        out = self._candidates({
            "handle": "+12125550144", "confirmed_name": "",
            "relationship": "neighbor", "evidence": "chats about the block",
            "contact_worthy": "yes", "action": "needs_name"})
        c = next(c for c in out if c["person_id"] == "p_res00000020")
        self.assertEqual(c["referral_hint"], "")


if __name__ == "__main__":
    unittest.main()
