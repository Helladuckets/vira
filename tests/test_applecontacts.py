"""Apple Contacts write-back: the plan (pure), the script, the gates,
the sqlite matcher, and the push orchestration with osascript mocked.
Fixtures are synthetic (@example.com, 555-01xx) per the PII guard."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import applecontacts as ac


def row(kind, value, rank="", use=""):
    return {"key": f"{kind}:{value}", "kind": kind, "value": value,
            "display": value, "rank": rank, "use": use, "added": False}


class PlanTest(unittest.TestCase):
    def test_rank_order_and_archived_label(self):
        rows = [row("email", "a@example.com", rank="primary", use="personal"),
                row("email", "b@example.com", rank="secondary"),
                row("email", "c@example.com"),
                row("email", "old@example.com", rank="archived", use="work")]
        plan = ac.plan_handles(rows, [], "email")
        self.assertEqual([v for v, _ in plan],
                         ["a@example.com", "b@example.com", "c@example.com",
                          "old@example.com"])
        self.assertEqual(plan[0][1], "home")       # personal -> home
        self.assertEqual(plan[-1][1], "old")       # archived -> "old"

    def test_use_maps_and_existing_label_preserved(self):
        rows = [row("email", "w@example.com", use="work"),
                row("email", "n@example.com")]
        existing = [{"value": "n@example.com", "label": "home"}]
        plan = dict(ac.plan_handles(rows, existing, "email"))
        self.assertEqual(plan["w@example.com"], "work")
        self.assertEqual(plan["n@example.com"], "home")   # kept, not invented

    def test_apple_only_address_survives(self):
        rows = [row("email", "a@example.com", rank="primary")]
        existing = [{"value": "a@example.com", "label": ""},
                    {"value": "keep@example.com", "label": "work"}]
        plan = ac.plan_handles(rows, existing, "email")
        self.assertEqual(plan[-1], ("keep@example.com", "work"))

    def test_apple_spelling_wins_for_phones(self):
        rows = [row("phone", "5550142222", rank="primary", use="personal")]
        existing = [{"value": "+1 (555) 014-2222", "label": "mobile"}]
        plan = ac.plan_handles(rows, existing, "phone")
        self.assertEqual(plan[0][0], "+1 (555) 014-2222")
        self.assertEqual(plan[0][1], "home")

    def test_dedupe_and_same_check(self):
        rows = [row("email", "a@example.com", rank="primary"),
                row("email", "A@Example.com")]
        existing = [{"value": "a@example.com", "label": ""}]
        plan = ac.plan_handles(rows, existing, "email")
        self.assertEqual(len(plan), 1)
        self.assertTrue(ac._same_handles(plan, existing, "email"))
        self.assertFalse(ac._same_handles(
            plan, [{"value": "a@example.com", "label": "work"}], "email"))


class ScriptTest(unittest.TestCase):
    def test_company_and_birthday(self):
        s = ac.build_script("UID:ABPerson", company='Ac "Me" Inc',
                            birthday=(1991, 8, 3))
        self.assertIn('set organization of P to "Ac \\"Me\\" Inc"', s)
        self.assertIn("set month of bd to August", s)
        self.assertIn("set day of bd to 3", s)
        self.assertNotIn("delete every email", s)

    def test_email_rewrite_lines(self):
        s = ac.build_script("UID:ABPerson",
                            emails=[("a@example.com", "home"),
                                    ("b@example.com", "")])
        self.assertIn("delete every email of P", s)
        self.assertIn('{label:"home", value:"a@example.com"}', s)
        self.assertIn('with properties {value:"b@example.com"}', s)
        self.assertLess(s.index("delete every email"),
                        s.index("a@example.com"))

    def test_birthday_parse(self):
        self.assertEqual(ac._birthday_parts("8/3/1991"), (1991, 8, 3))
        self.assertEqual(ac._birthday_parts("1991-08-03"), (1991, 8, 3))
        self.assertIsNone(ac._birthday_parts("next tuesday"))
        self.assertIsNone(ac._birthday_parts(""))


class GateTest(unittest.TestCase):
    def test_passive_and_sandbox_refuse(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            self.assertFalse(ac.enabled())
        with mock.patch.dict(os.environ, {"VIRA_SANDBOX": "1"}):
            self.assertFalse(ac.enabled())

    def test_off_mac_refuses(self):
        with mock.patch.object(ac.settings, "IS_MAC", False):
            self.assertFalse(ac.enabled())

    def test_config_off_refuses(self):
        with mock.patch.object(ac.settings, "get",
                               side_effect=lambda k: False):
            with mock.patch.object(ac.settings, "IS_MAC", True):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("VIRA_PASSIVE", None)
                    os.environ.pop("VIRA_SANDBOX", None)
                    self.assertFalse(ac.enabled())


def _fixture_store(dirpath):
    db = Path(dirpath) / "AddressBook-v22.abcddb"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, "
              "ZUNIQUEID TEXT, ZORGANIZATION TEXT)")
    c.execute("CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, "
              "ZOWNER INTEGER, ZADDRESS TEXT, ZLABEL TEXT)")
    c.execute("CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, "
              "ZOWNER INTEGER, ZFULLNUMBER TEXT, ZLABEL TEXT)")
    c.execute("INSERT INTO ZABCDRECORD VALUES (1, 'AAA:ABPerson', 'OldCo')")
    c.execute("INSERT INTO ZABCDEMAILADDRESS VALUES "
              "(1, 1, 'match@example.com', '_$!<Home>!$_')")
    c.execute("INSERT INTO ZABCDPHONENUMBER VALUES "
              "(1, 1, '+1 (555) 014-9999', '_$!<Mobile>!$_')")
    c.execute("INSERT INTO ZABCDRECORD VALUES (2, 'BBB:ABPerson', '')")
    c.execute("INSERT INTO ZABCDEMAILADDRESS VALUES "
              "(2, 2, 'other@example.com', NULL)")
    c.commit()
    c.close()
    return db


class FindCardsTest(unittest.TestCase):
    def test_match_and_label_unwrap(self):
        with tempfile.TemporaryDirectory() as td:
            db = _fixture_store(td)
            with mock.patch.object(ac, "_stores", return_value=[db]):
                cards = ac.find_cards(["match@example.com"], [])
                self.assertEqual(len(cards), 1)
                self.assertEqual(cards[0]["uid"], "AAA:ABPerson")
                self.assertEqual(cards[0]["org"], "OldCo")
                self.assertEqual(cards[0]["emails"][0]["label"], "home")
                phone_hit = ac.find_cards([], ["5550149999"])
                self.assertEqual(phone_hit[0]["uid"], "AAA:ABPerson")
                self.assertEqual(ac.find_cards(["nope@example.com"], []), [])


class PushTest(unittest.TestCase):
    def _card(self):
        return {"pid": "p_x", "custom": [], "updated": None,
                "fields": [{"key": "company", "value": "NewCo"},
                           {"key": "birthday", "value": "8/3/1991"},
                           {"key": "display_name", "value": "Pat Example"}],
                "handles": [row("email", "match@example.com", rank="primary",
                                use="personal"),
                            row("email", "gone@example.com", rank="archived")]}

    def test_push_updates_matched_card(self):
        scripts = []
        apple = {"uid": "AAA:ABPerson", "store": "s", "org": "OldCo",
                 "emails": [{"value": "match@example.com", "label": "home"},
                            {"value": "extra@example.com", "label": "work"}],
                 "phones": []}
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[apple]), \
             mock.patch.object(ac, "backup_vcard", return_value=None), \
             mock.patch.object(ac, "_osascript",
                               side_effect=lambda s: scripts.append(s)), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}, "master": {}}), \
             mock.patch("server.contactcard.compose",
                        return_value=self._card()):
            out = ac.push_person("p_x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cards"][0]["changed"],
                         ["company", "birthday", "emails"])
        s = scripts[0]
        self.assertIn('set organization of P to "NewCo"', s)
        # the Apple-only address survived the rewrite
        self.assertIn("extra@example.com", s)
        # archived sank to the CRM block's end wearing "old", before extras
        self.assertLess(s.index("match@example.com"),
                        s.index("gone@example.com"))
        self.assertIn('{label:"old", value:"gone@example.com"}', s)

    def test_no_match_is_honest(self):
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[]), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}}), \
             mock.patch("server.contactcard.compose",
                        return_value=self._card()):
            out = ac.push_person("p_x")
        self.assertFalse(out["ok"])
        self.assertIn("no Apple contact", out["detail"])

    def test_current_card_not_rewritten(self):
        apple = {"uid": "AAA:ABPerson", "store": "s", "org": "NewCo",
                 "emails": [{"value": "match@example.com", "label": "home"},
                            {"value": "gone@example.com", "label": "old"}],
                 "phones": []}
        card = self._card()
        card["fields"] = [{"key": "company", "value": "NewCo"},
                          {"key": "birthday", "value": ""}]
        ran = []
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[apple]), \
             mock.patch.object(ac, "_osascript",
                               side_effect=lambda s: ran.append(s)), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}}), \
             mock.patch("server.contactcard.compose", return_value=card):
            out = ac.push_person("p_x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["cards"][0]["changed"], [])
        self.assertEqual(ran, [])
        self.assertIn("already current", out["detail"])

    def test_osascript_failure_never_raises(self):
        apple = {"uid": "AAA:ABPerson", "store": "s", "org": "Old",
                 "emails": [{"value": "match@example.com", "label": ""}],
                 "phones": []}
        with mock.patch.object(ac, "enabled", return_value=True), \
             mock.patch.object(ac, "find_cards", return_value=[apple]), \
             mock.patch.object(ac, "backup_vcard", return_value=None), \
             mock.patch.object(ac, "_osascript",
                               side_effect=RuntimeError("-600")), \
             mock.patch("server.data.get_person",
                        return_value={"person": {}}), \
             mock.patch("server.contactcard.compose",
                        return_value=self._card()):
            out = ac.push_person("p_x")
        self.assertFalse(out["ok"])
        self.assertIn("-600", out["detail"])


if __name__ == "__main__":
    unittest.main()
