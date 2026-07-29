"""Group profiles: leg merging, the related-group diff, member dossiers,
the interconnection filter, the cached AI brief, and guid-addressed
sending — all over a synthetic chat.db (no Full Disk Access, no personal
data; numbers stay in the NANP reserved-fiction 555-01xx block).

Run: .venv/bin/python -m unittest tests.test_groupchat
"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from server import data as crm
from server import groupchat, imessage, jsonstore
from server import send as sender
from server.imessage import apple_ns

DAY = 86_400 * 1_000_000_000

ANN, RAJ, LEE = "ann@example.test", "raj@example.test", "lee@example.test"
STRANGER = "+12125550177"


def _crm_cache():
    people = {
        "p_ann": {"id": "p_ann", "name": "Ann Reyes"},
        "p_raj": {"id": "p_raj", "name": "Raj Patel"},
        "p_lee": {"id": "p_lee", "name": "Lee Chen"},
    }
    return {
        "loaded_at": 1.0,
        "by_id": people,
        "by_handle": {ANN: "p_ann", RAJ: "p_raj", LEE: "p_lee"},
        "profiles": {
            "p_ann": {
                "relationship_class": "close friend",
                "relationship_summary": "College roommate, plans the trips.",
                "hooks": [{"hook": "new job at Vertex"}],
                "open_loops": [
                    {"what": "owe her the cabin deposit", "owed_by": "me"},
                    {"what": "old one", "owed_by": "me", "status": "closed"},
                ],
            },
            "p_raj": {"relationship_class": "friend"},
        },
        "master": {
            "p_raj": {"id": "p_raj", "title": "Engineer", "company": "Distillery"},
        },
        "chats_by_person": {},
    }


def _chat_db(path, base_ns):
    """Two legs of 'Ski trip' (iMessage newer, SMS older), a superset group,
    a one-shared-member group (filtered), and a same-set renamed group."""
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE chat(ROWID INTEGER PRIMARY KEY, style INT,
                        display_name TEXT, guid TEXT, service_name TEXT);
      CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
      CREATE TABLE chat_handle_join(chat_id INT, handle_id INT);
      CREATE TABLE message(ROWID INTEGER PRIMARY KEY, date INT,
                           is_from_me INT, handle_id INT, text TEXT,
                           attributedBody BLOB,
                           associated_message_type INT DEFAULT 0);
      CREATE TABLE chat_message_join(chat_id INT, message_id INT);
    """)
    chats = [
        (1, 45, "", "iMessage;-;ann", "iMessage"),          # 1:1 Ann
        (2, 43, "Ski trip", "iMessage;+;chat100", "iMessage"),
        (3, 43, "Ski trip", "SMS;+;chat101", "SMS"),        # older leg
        (4, 43, "", "iMessage;+;chat102", "iMessage"),      # + Lee (superset)
        (5, 43, "Book club", "iMessage;+;chat103", "iMessage"),  # 1 shared
        (6, 43, "Ski trip 2024", "iMessage;+;chat104", "iMessage"),  # same set
    ]
    con.executemany("INSERT INTO chat VALUES(?,?,?,?,?)", chats)
    con.executemany("INSERT INTO handle VALUES(?,?)",
                    [(1, ANN), (2, RAJ), (3, LEE), (4, STRANGER)])
    con.executemany("INSERT INTO chat_handle_join VALUES(?,?)", [
        (1, 1),
        (2, 1), (2, 2),
        (3, 1), (3, 2),
        (4, 1), (4, 2), (4, 3),
        (5, 1), (5, 3),
        (6, 1), (6, 2),
    ])
    rows = [
        (1, base_ns, 0, 1, "old sms leg message", None, 0),
        (2, base_ns + DAY, 0, 1, "who's driving up?", None, 0),
        (3, base_ns + 2 * DAY, 1, None, "I can take three", None, 0),
        (4, base_ns + 3 * DAY, 0, 2, "lifts open at 8", None, 0),
        (5, base_ns + DAY, 0, 3, "chapter four this week", None, 0),
        (6, base_ns + DAY, 0, 2, "superset chatter", None, 0),
    ]
    con.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?)", rows)
    con.executemany("INSERT INTO chat_message_join VALUES(?,?)",
                    [(3, 1), (2, 2), (2, 3), (2, 4), (5, 5), (4, 6)])
    con.commit()
    con.close()


FAKE_GRAPH = {
    "status": "ok",
    "nodes": [{"id": "p_ann"}, {"id": "p_raj"}],
    "edges": [
        {"a": "p_ann", "b": "p_raj", "weight": 2.0,
         "signals": [{"type": "group_cochat"}, {"type": "photo_cooccur"}]},
        {"a": "p_ann", "b": "p_zzz", "weight": 9.0, "signals": []},
    ],
}

GOOD_BRIEF = json.dumps({
    "read": "The trip is on for Saturday and logistics are settling.",
    "highlights": ["driving plan", "lift timing"],
    "suggestions": [{"label": "Confirm the car", "text": "I can take three."}],
    "loops": [{"what": "cabin deposit", "who": "you"}],
    "watch": "Nobody has booked the cabin yet.",
})


class GroupChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = apple_ns(datetime(2026, 3, 10, 12, 0))
        chat = Path(self.tmp.name) / "chat.db"
        _chat_db(chat, self.base)
        self.briefs = Path(self.tmp.name) / "group-briefs.json"
        cache = _crm_cache()
        patches = [
            mock.patch.object(
                imessage, "_connect",
                lambda: sqlite3.connect(f"file:{chat}?mode=ro", uri=True)),
            mock.patch.object(crm, "_load", lambda: cache),
            mock.patch.object(
                crm, "resolve_handle",
                lambda h: cache["by_handle"].get(h)),
            mock.patch.object(groupchat, "BRIEFS", self.briefs),
            mock.patch.object(groupchat.settings, "fixture_mode",
                              lambda: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class ResolveTests(GroupChatBase):
    def test_legs_with_same_members_and_name_merge(self):
        g = groupchat.resolve_group(2)
        self.assertEqual(sorted(g["chat_ids"]), [2, 3])
        self.assertEqual(g["name"], "Ski trip")
        self.assertEqual([p["name"] for p in g["participants"]],
                         ["Ann Reyes", "Raj Patel"])

    def test_the_send_leg_is_the_newest_conversation(self):
        g = groupchat.resolve_group(3)   # entering via the stale SMS leg
        self.assertEqual(g["send"]["chat_id"], 2)
        self.assertEqual(g["send"]["guid"], "iMessage;+;chat100")

    def test_a_direct_chat_is_not_a_group(self):
        self.assertIsNone(groupchat.resolve_group(1))
        self.assertIsNone(groupchat.resolve_group(999))

    def test_a_renamed_same_member_group_stays_separate(self):
        g = groupchat.resolve_group(2)
        self.assertNotIn(6, g["chat_ids"])


class ProfileTests(GroupChatBase):
    def setUp(self):
        super().setUp()
        for p in [
            mock.patch("server.media.counts_for_chats",
                       lambda ids: {2: {"photos": 3, "links": 1, "docs": 0}}),
            mock.patch("server.photos.photo_path", lambda pid: None),
            mock.patch("server.atlas.compose", lambda: dict(FAKE_GRAPH)),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def test_profile_shape_and_label(self):
        d = groupchat.profile(chat=2)
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["label"], "Ski trip")
        self.assertEqual(d["media"], {"photos": 3, "links": 1, "docs": 0})

    def test_member_dossiers_carry_crm_depth(self):
        d = groupchat.profile(chat=2)
        ann = next(p for p in d["group"]["participants"]
                   if p["person_id"] == "p_ann")
        self.assertEqual(ann["relationship"], "close friend")
        self.assertEqual(ann["hooks"], ["new job at Vertex"])
        # closed loops stay out of the dossier
        self.assertEqual(len(ann["open_loops"]), 1)
        raj = next(p for p in d["group"]["participants"]
                   if p["person_id"] == "p_raj")
        self.assertEqual(raj["company"], "Distillery")

    def test_activity_includes_the_owner_and_sums_to_100(self):
        d = groupchat.profile(chat=2)
        names = {a["name"]: a for a in d["activity"]}
        self.assertIn("you", names)
        self.assertEqual(sum(a["n"] for a in d["activity"]), 4)

    def test_interconnections_filter_to_members_only(self):
        d = groupchat.profile(chat=2)
        edges = d["connections"]["edges"]
        self.assertEqual(len(edges), 1)
        self.assertEqual({edges[0]["a"], edges[0]["b"]}, {"p_ann", "p_raj"})
        self.assertEqual(d["connections"]["off_graph"], [])

    def test_off_graph_members_are_named_not_dropped(self):
        d = groupchat.profile(chat=4)   # Lee is not on the fake graph
        self.assertIn("p_lee", d["connections"]["off_graph"])

    def test_missing_graph_degrades_honestly(self):
        with mock.patch("server.atlas.compose",
                        lambda: {"status": "empty"}):
            d = groupchat.profile(chat=2)
        self.assertFalse(d["connections"]["available"])

    def test_related_groups_diff(self):
        d = groupchat.profile(chat=2)
        rel = {g["relation"]: g for g in d["related"]}
        self.assertIn("superset", rel)
        self.assertEqual(rel["superset"]["added"], ["Lee"])
        self.assertEqual(rel["superset"]["missing"], [])
        self.assertIn("same", rel)              # Ski trip 2024
        self.assertEqual(rel["same"]["name"], "Ski trip 2024")
        # Book club shares only Ann — below the 2-member floor
        self.assertNotIn("Book club", [g["name"] for g in d["related"]])

    def test_fixture_mode_is_dormant(self):
        with mock.patch.object(groupchat.settings, "fixture_mode",
                               lambda: True):
            self.assertEqual(groupchat.profile(chat=2)["status"], "empty")


class BriefTests(GroupChatBase):
    def setUp(self):
        super().setUp()
        for p in [
            mock.patch("server.media.counts_for_chats", lambda ids: {}),
            mock.patch("server.photos.photo_path", lambda pid: None),
            mock.patch("server.atlas.compose", lambda: dict(FAKE_GRAPH)),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def test_brief_composes_validates_and_caches(self):
        calls = []

        def fake_complete(prompt):
            calls.append(prompt)
            return GOOD_BRIEF
        with mock.patch("server.suggest.complete", fake_complete):
            r1 = groupchat.brief([2, 3])
            r2 = groupchat.brief([2, 3])
        self.assertEqual(r1["status"], "ok")
        self.assertFalse(r1["cached"])
        self.assertTrue(r2["cached"])
        self.assertEqual(len(calls), 1)
        # the prompt is grounded: members, thread, and the diffs ride in
        self.assertIn("Ann Reyes", calls[0])
        self.assertIn("who's driving up?", calls[0])
        self.assertIn("Distillery", calls[0])
        self.assertEqual(r1["brief"]["suggestions"][0]["label"],
                         "Confirm the car")

    def test_force_regenerates(self):
        calls = []

        def fake_complete(prompt):
            calls.append(prompt)
            return GOOD_BRIEF
        with mock.patch("server.suggest.complete", fake_complete):
            groupchat.brief([2, 3])
            groupchat.brief([2, 3], force=True)
        self.assertEqual(len(calls), 2)

    def test_a_new_message_invalidates_the_cache(self):
        with mock.patch("server.suggest.complete",
                        lambda p: GOOD_BRIEF):
            groupchat.brief([2, 3])
        store = jsonstore.read(self.briefs, {})
        key = groupchat._group_key([2, 3])
        store["briefs"][key]["latest"] -= 1     # pretend a newer row landed
        jsonstore.write_atomic(self.briefs, store)
        calls = []
        with mock.patch("server.suggest.complete",
                        lambda p: calls.append(p) or GOOD_BRIEF):
            r = groupchat.brief([2, 3])
        self.assertEqual(len(calls), 1)
        self.assertFalse(r["cached"])

    def test_clean_brief_drops_junk_and_requires_a_read(self):
        cleaned = groupchat._clean_brief({
            "read": "Fine.",
            "highlights": ["ok", "", 7],
            "suggestions": [{"label": "x", "text": ""}, {"text": "send this"},
                            "junk"],
            "loops": [{"what": "thing", "who": "nonsense"}, {}],
            "watch": None,
        })
        self.assertEqual(cleaned["highlights"], ["ok"])
        self.assertEqual(len(cleaned["suggestions"]), 1)
        self.assertEqual(cleaned["loops"][0]["who"], "group")
        with self.assertRaises(ValueError):
            groupchat._clean_brief({"read": ""})
        with self.assertRaises(ValueError):
            groupchat._clean_brief([1, 2])


class SendTests(GroupChatBase):
    def test_send_targets_the_active_leg_guid(self):
        seen = {}

        def fake_send(guid, text, chat_ids=None):
            seen.update(guid=guid, text=text, chat_ids=chat_ids)
            return {"guid": guid, "channel": "imessage", "note": None}
        with mock.patch.object(sender, "send_to_group", fake_send):
            r = groupchat.send([3, 2], "on my way")
        self.assertEqual(seen["guid"], "iMessage;+;chat100")
        self.assertEqual(sorted(seen["chat_ids"]), [2, 3])
        self.assertEqual(r["label"], "Ski trip")

    def test_send_refuses_an_unknown_group(self):
        with self.assertRaises(ValueError):
            groupchat.send([999], "hello?")

    def test_passive_instance_blocks_group_sends(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(RuntimeError):
                sender.send_to_group("iMessage;+;chat100", "hi")

    def test_empty_and_guidless_sends_are_bad_input(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIRA_PASSIVE", None)
            with mock.patch.object(sender.settings, "IS_MAC", True):
                with self.assertRaises(ValueError):
                    sender.send_to_group("", "hi")
                with self.assertRaises(ValueError):
                    sender.send_to_group("iMessage;+;chat100", "   ")


class RouteTests(unittest.TestCase):
    """TestClient without the context manager runs no lifespan — no
    watchers, no pollers (the test_companion pattern)."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)

    def test_profile_route_answers(self):
        with mock.patch("server.groupchat.profile",
                        lambda chat_ids=None, chat=None:
                        {"status": "ok", "chat": chat, "ids": chat_ids}):
            r = self.client.get("/api/group/profile?chat=7")
            self.assertEqual(r.json()["chat"], 7)
            r = self.client.get("/api/group/profile?ids=2,3")
            self.assertEqual(r.json()["ids"], [2, 3])

    def test_profile_route_rejects_bad_ids(self):
        self.assertEqual(
            self.client.get("/api/group/profile?ids=abc").status_code, 400)

    def test_brief_route_requires_ids(self):
        r = self.client.post("/api/group/brief", json={"ids": []})
        self.assertEqual(r.status_code, 400)

    def test_send_route_maps_runtime_errors_to_400(self):
        with mock.patch("server.groupchat.send",
                        side_effect=RuntimeError("passive test instance")):
            r = self.client.post("/api/group/send",
                                 json={"ids": [2], "text": "hi"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("passive", r.json()["detail"])

    def test_send_route_passes_through_result(self):
        with mock.patch("server.groupchat.send",
                        lambda ids, text: {"guid": "g", "channel": "imessage",
                                           "note": None, "label": "Ski trip"}):
            r = self.client.post("/api/group/send",
                                 json={"ids": [2, 3], "text": "hi"})
        self.assertEqual(r.json()["label"], "Ski trip")


if __name__ == "__main__":
    unittest.main()
