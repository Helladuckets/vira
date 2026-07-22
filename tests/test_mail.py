"""Mail account management — the Setup mail card's IMAP add path.

Password never touches the JSON; the {email, host} row is deduped by
address and a graph account for the same address is left intact."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import mail


class ImapAddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accts = Path(self.tmp.name) / "mail-accounts.json"
        self.store = {}
        self._patches = [
            mock.patch.object(mail, "ACCOUNTS", self.accts),
            mock.patch.object(
                mail.secrets, "set",
                side_effect=lambda s, a, v: self.store.__setitem__((s, a), v)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_add_writes_row_and_stores_password(self):
        r = mail.add_imap_account("Me@Example.com", "imap.example.com", "pw1")
        self.assertTrue(r["added"])
        self.assertEqual(r["email"], "me@example.com")  # normalized
        rows = json.loads(self.accts.read_text())
        self.assertEqual(
            rows, [{"email": "me@example.com", "host": "imap.example.com"}])
        # password rode the secrets ladder, never the file
        self.assertEqual(
            self.store[(mail.keychain_service(), "me@example.com")], "pw1")
        self.assertNotIn("pw1", self.accts.read_text())

    def test_readd_updates_host_in_place(self):
        mail.add_imap_account("me@example.com", "old.example.com", "pw1")
        r = mail.add_imap_account("me@example.com", "new.example.com", "pw2")
        self.assertFalse(r["added"])
        rows = json.loads(self.accts.read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "new.example.com")

    def test_graph_account_for_same_address_is_left_intact(self):
        self.accts.write_text(
            json.dumps([{"email": "me@example.com", "type": "graph"}]))
        mail.add_imap_account("me@example.com", "imap.example.com", "pw")
        rows = json.loads(self.accts.read_text())
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(a.get("type") == "graph" for a in rows))
        self.assertTrue(any(a.get("host") == "imap.example.com" for a in rows))

    def test_load_accounts_tolerates_wrapped_shape(self):
        self.accts.write_text(
            json.dumps({"accounts": [{"email": "a@example.com",
                                      "host": "h.example.com"}]}))
        self.assertEqual(len(mail.load_accounts()), 1)

    def test_validation_rejects_bad_input(self):
        for bad in [("noat", "h.example.com", "p"),
                    ("a@example.com", "", "p"),
                    ("a@example.com", "h.example.com", "")]:
            with self.assertRaises(ValueError):
                mail.add_imap_account(*bad)


if __name__ == "__main__":
    unittest.main()
