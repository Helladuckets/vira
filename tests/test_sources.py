"""The source registry (server/sources.py): shape, platform filtering, and
the probe contract — present / configured / count derived from the world,
independently, never raising.

Hermetic by construction: every probe target (AddressBook glob, chat.db,
Calendar store, mail-accounts.json, the CRM root) is patched to a tmp
fixture, so these pass identically on a loaded Mac and a bare CI runner.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import mail, settings, sources

KINDS = {sources.CONTACTS, sources.MESSAGES, sources.CALENDAR, sources.MAIL}


def _tiny_db(path, schema, rows_sql=()):
    con = sqlite3.connect(path)
    con.executescript(schema)
    for sql in rows_sql:
        con.execute(sql)
    con.commit()
    con.close()


class RegistryShapeTests(unittest.TestCase):
    def test_every_row_is_fully_declared(self):
        for sid, spec in sources.SOURCES.items():
            self.assertTrue(spec["label"])
            self.assertIn(spec["kind"], KINDS)
            self.assertTrue(spec["platforms"])
            self.assertTrue(set(spec["platforms"]) <= set(sources.ALL_PLATFORMS))
            self.assertIsInstance(spec["needs_disk"], bool)
            self.assertTrue(spec["card"])
            self.assertTrue(callable(spec["probe"]))

    def test_the_expected_sources_are_wired(self):
        self.assertEqual(
            list(sources.SOURCES),
            ["apple-contacts", "google-csv", "imessage", "apple-calendar",
             "imap-mail", "m365-mail"])

    def test_ids_double_as_import_tags(self):
        # import_contacts stamps refs.import_source with these exact strings;
        # the contacts probes count by them.
        self.assertIn("apple-contacts", sources.SOURCES)
        self.assertIn("google-csv", sources.SOURCES)

    def test_probe_record_shape(self):
        rec = sources.probe("google-csv", {"people": []})
        for key in ("id", "label", "kind", "platforms", "supported",
                    "needs_disk", "card", "present", "configured", "count",
                    "detail", "action"):
            self.assertIn(key, rec)

    def test_unknown_source_is_none(self):
        self.assertIsNone(sources.probe("carrier-pigeon"))

    def test_a_crashing_probe_never_raises(self):
        broken = dict(sources.SOURCES["google-csv"],
                      probe=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.dict(sources.SOURCES, {"google-csv": broken}):
            rec = sources.probe("google-csv", {"people": []})
        self.assertIn("probe error", rec["detail"])
        self.assertFalse(rec["configured"])


class PlatformFilterTests(unittest.TestCase):
    """The Setup fork is a filter over rows, driven by settings.IS_MAC /
    IS_WIN read at probe time — patchable exactly like test_platform.py."""

    def _plat(self, is_mac, is_win):
        return (mock.patch.object(sources.settings, "IS_MAC", is_mac),
                mock.patch.object(sources.settings, "IS_WIN", is_win))

    def test_platform_token_tracks_the_constants(self):
        for is_mac, is_win, want in ((True, False, "mac"),
                                     (False, True, "win"),
                                     (False, False, "linux")):
            a, b = self._plat(is_mac, is_win)
            with a, b:
                self.assertEqual(sources._platform(), want)

    def test_apple_rows_unsupported_off_mac_with_the_reason_named(self):
        a, b = self._plat(False, True)
        with a, b:
            rec = sources.probe("imessage", {"people": []})
        self.assertFalse(rec["supported"])
        self.assertIn("macOS-only", rec["detail"])
        self.assertFalse(rec["present"])

    def test_cross_platform_rows_survive_the_filter_everywhere(self):
        for is_mac, is_win in ((True, False), (False, True), (False, False)):
            a, b = self._plat(is_mac, is_win)
            with a, b, \
                 mock.patch.object(sources, "_people", return_value=[]), \
                 mock.patch.object(sources, "_mail_accounts", return_value=[]):
                ids = {r["id"] for r in sources.available()}
            self.assertIn("google-csv", ids)
            self.assertIn("imap-mail", ids)
            self.assertIn("m365-mail", ids)
            self.assertEqual("apple-contacts" in ids, is_mac)

    def test_of_kind_groups_rows(self):
        rows = [{"kind": "mail", "id": "a"}, {"kind": "contacts", "id": "b"}]
        self.assertEqual([r["id"] for r in sources.of_kind("mail", rows)],
                         ["a"])


class ContactProbeTests(unittest.TestCase):
    PEOPLE = [
        {"id": "p_1", "refs": {"import_source": "apple-contacts"}},
        {"id": "p_2", "refs": {"import_source": "apple-contacts"}},
        {"id": "p_3", "refs": {"import_source": "google-csv"}},
        {"id": "p_4", "refs": {}},          # a triage add — no import tag
        {"id": "p_5"},                      # no refs at all
    ]

    def test_counts_split_by_import_tag(self):
        ctx = {"people": self.PEOPLE}
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "AddressBook-v22.abcddb"
            store.touch()
            with mock.patch.object(sources, "AB_SOURCES", Path(tmp)):
                apple = sources._probe_apple_contacts(ctx)
        google = sources._probe_google_csv(ctx)
        self.assertEqual(apple["count"], 2)
        self.assertTrue(apple["configured"])
        self.assertEqual(google["count"], 1)
        self.assertTrue(google["configured"])

    def test_apple_present_tracks_the_stores_not_the_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sources, "AB_SOURCES", Path(tmp)):
                rec = sources._probe_apple_contacts({"people": []})
        self.assertFalse(rec["present"])
        self.assertFalse(rec["configured"])
        self.assertIn("No AddressBook stores", rec["detail"])

    def test_google_csv_always_present_never_configured_until_imported(self):
        rec = sources._probe_google_csv({"people": []})
        self.assertTrue(rec["present"])     # an upload path always exists
        self.assertFalse(rec["configured"])

    def test_people_reader_tolerates_a_missing_crm(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sources, "_crm_root",
                                   return_value=Path(tmp) / "nope"):
                self.assertEqual(sources._people(), [])


class StoreProbeTests(unittest.TestCase):
    """present and configured are separate facts: a chat.db on disk that
    Full Disk Access hasn't unlocked is present but not configured."""

    def test_missing_store(self):
        with mock.patch.object(sources, "CHAT_DB", Path("/nonexistent/chat.db")):
            rec = sources._probe_imessage({})
        self.assertFalse(rec["present"])
        self.assertEqual(rec["count"], 0)

    def test_present_but_unreadable_is_not_configured(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as fh:
            fh.write(b"not a database")
            fh.flush()
            with mock.patch.object(sources, "CHAT_DB", Path(fh.name)):
                rec = sources._probe_imessage({})
        self.assertTrue(rec["present"])
        self.assertFalse(rec["configured"])
        self.assertIn("Full Disk Access", rec["detail"])

    def test_readable_store_counts_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "chat.db"
            _tiny_db(db, "CREATE TABLE message (ROWID INTEGER PRIMARY KEY);",
                     ["INSERT INTO message VALUES (1)",
                      "INSERT INTO message VALUES (2)"])
            with mock.patch.object(sources, "CHAT_DB", db):
                rec = sources._probe_imessage({})
        self.assertTrue(rec["configured"])
        self.assertEqual(rec["count"], 2)

    def test_calendar_probe_same_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "Calendar.sqlitedb"
            _tiny_db(db, "CREATE TABLE CalendarItem (ROWID INTEGER PRIMARY KEY);",
                     ["INSERT INTO CalendarItem VALUES (7)"])
            with mock.patch.object(sources, "CAL_DB", db):
                rec = sources._probe_apple_calendar({})
        self.assertTrue(rec["configured"])
        self.assertEqual(rec["count"], 1)


class MailProbeTests(unittest.TestCase):
    def _with_accounts(self, payload):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        tmp.write(json.dumps(payload))
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return mock.patch.object(mail, "ACCOUNTS", Path(tmp.name))

    def test_accounts_split_by_type(self):
        with self._with_accounts([
                {"email": "owner@example.com", "host": "imap.example.com"},
                {"email": "work@example.com", "type": "graph"}]):
            imap = sources._probe_imap({})
            m365 = sources._probe_m365({})
        self.assertEqual((imap["count"], imap["configured"]), (1, True))
        self.assertEqual((m365["count"], m365["configured"]), (1, True))

    def test_no_accounts_file_is_dormant_not_an_error(self):
        with mock.patch.object(mail, "ACCOUNTS", Path("/nonexistent/x.json")):
            imap = sources._probe_imap({})
        self.assertTrue(imap["present"])
        self.assertFalse(imap["configured"])
        self.assertEqual(imap["count"], 0)


class DiscoverTests(unittest.TestCase):
    def test_discover_returns_every_row_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            patches = [
                mock.patch.object(sources, "AB_SOURCES", Path(tmp) / "ab"),
                mock.patch.object(sources, "CHAT_DB", Path(tmp) / "chat.db"),
                mock.patch.object(sources, "CAL_DB", Path(tmp) / "cal.db"),
                mock.patch.object(sources, "_crm_root",
                                  return_value=Path(tmp) / "crm"),
                mock.patch.object(mail, "ACCOUNTS", Path(tmp) / "mail.json"),
            ]
            for p in patches:
                p.start()
                self.addCleanup(p.stop)
            rows = sources.discover()
        self.assertEqual([r["id"] for r in rows], list(sources.SOURCES))
        for r in rows:
            self.assertIsInstance(r["detail"], str)

    def test_platform_label_is_a_name_not_a_token(self):
        self.assertIn(sources.platform_label(), ("macOS", "Windows", "Linux"))


if __name__ == "__main__":
    unittest.main()
