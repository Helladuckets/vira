"""Read-and-reply on feed email cards (server/mailread.py).

No network anywhere: IMAP/SMTP/Graph are all patched. Fixture addresses
stay on @example.com per the PII guard.
"""
import email.message
import os
import unittest
from unittest import mock

from server import mailread

IMAP_ACCT = {"email": "owner@example.com", "host": "imap.example.com"}
GRAPH_ACCT = {"email": "work@example.com", "type": "graph"}


def _accounts_patch():
    return mock.patch.object(
        mailread, "_accounts", return_value=[IMAP_ACCT, GRAPH_ACCT])


def _multipart(text=None, html=None, attach=False):
    msg = email.message.EmailMessage()
    msg["From"] = "Pat Example <pat@example.com>"
    msg["To"] = "owner@example.com"
    msg["Subject"] = "Dinner plans"
    msg["Message-ID"] = "<orig-123@example.com>"
    msg["Date"] = "Tue, 04 Aug 2026 10:00:00 -0400"
    msg.set_content(text or "fallback")
    if html is not None:
        msg.add_alternative(html, subtype="html")
    if attach:
        msg.add_attachment(b"PDFDATA", maintype="application",
                           subtype="pdf", filename="doc.pdf")
    return msg


class HtmlToTextTests(unittest.TestCase):
    def test_paragraphs_survive(self):
        t = mailread.html_to_text(
            "<html><body><p>First line.</p><p>Second line.</p>"
            "<div>Third</div></body></html>")
        self.assertEqual(t, "First line.\n\nSecond line.\n\nThird")

    def test_br_and_entities(self):
        t = mailread.html_to_text("a&amp;b<br>next &mdash; end")
        self.assertEqual(t, "a&b\nnext — end")

    def test_style_and_script_stripped(self):
        t = mailread.html_to_text(
            "<style>.x{color:red}</style><script>alert(1)</script>hello")
        self.assertEqual(t, "hello")


class BodyExtractionTests(unittest.TestCase):
    def test_plain_part_keeps_lines(self):
        msg = _multipart(text="line one\n\nline two", html="<p>ignored</p>")
        text, html = mailread.message_bodies(msg)
        self.assertEqual(text, "line one\n\nline two")
        self.assertIn("ignored", html)

    def test_html_only_falls_back_to_stripped_text(self):
        msg = email.message.EmailMessage()
        msg.set_content("<p>only html here</p>", subtype="html")
        text, html = mailread.message_bodies(msg)
        self.assertEqual(text, "only html here")
        self.assertTrue(html)

    def test_attachment_never_becomes_the_body(self):
        msg = _multipart(text="real body", attach=True)
        text, _ = mailread.message_bodies(msg)
        self.assertEqual(text, "real body")

    def test_unknown_charset_degrades_not_raises(self):
        msg = email.message.EmailMessage()
        msg.set_content("hi")
        del msg["Content-Type"]
        msg["Content-Type"] = 'text/plain; charset="unknown-8bit"'
        text, _ = mailread.message_bodies(msg)
        self.assertIn("hi", text)


class HelperTests(unittest.TestCase):
    def test_imap_uid_parses_and_refuses(self):
        self.assertEqual(
            mailread._imap_uid("mail-owner@example.com-4471",
                               "owner@example.com"), 4471)
        self.assertIsNone(
            mailread._imap_uid("mail-owner@example.com-AAQkAD-x",
                               "owner@example.com"))
        self.assertIsNone(mailread._imap_uid("wa-123", "owner@example.com"))

    def test_re_subject(self):
        self.assertEqual(mailread.re_subject("Hello"), "Re: Hello")
        self.assertEqual(mailread.re_subject("RE: Hello"), "RE: Hello")
        self.assertEqual(mailread.re_subject("re: Hello"), "re: Hello")

    def test_smtp_host_derivation(self):
        self.assertEqual(mailread.smtp_host_for(IMAP_ACCT),
                         "smtp.example.com")
        self.assertEqual(
            mailread.smtp_host_for({"host": "mail.odd.example",
                                    "smtp_host": "out.odd.example"}),
            "out.odd.example")
        with self.assertRaises(RuntimeError):
            mailread.smtp_host_for({"host": "mail.odd.example"})

    def test_unknown_account_raises_value_error(self):
        with _accounts_patch():
            with self.assertRaises(ValueError):
                mailread.get_message("nobody@example.com")


class SmtpReplyTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        fake = self

        class FakeSMTP:
            def __init__(s, host, port, timeout=None):
                fake.smtp_args = (host, port, timeout)

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def login(s, user, pw):
                fake.login_args = (user, pw)

            def send_message(s, msg):
                fake.sent.append(msg)

        self._patches = [
            _accounts_patch(),
            mock.patch.object(mailread.smtplib, "SMTP_SSL", FakeSMTP),
            mock.patch.object(mailread.mailmod, "keychain_password",
                              return_value="app-pw"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self._patches:
            p.start()
        os.environ.pop("VIRA_PASSIVE", None)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_reply_threads_and_sends(self):
        r = mailread.send_reply(
            "owner@example.com", "sounds good",
            to="pat@example.com", subject="Dinner plans",
            message_id="<orig-123@example.com>",
            references="<older@example.com>")
        self.assertTrue(r["sent"])
        self.assertEqual(r["channel"], "smtp")
        msg = self.sent[0]
        self.assertEqual(msg["To"], "pat@example.com")
        self.assertEqual(msg["Subject"], "Re: Dinner plans")
        self.assertEqual(msg["In-Reply-To"], "<orig-123@example.com>")
        self.assertEqual(msg["References"],
                         "<older@example.com> <orig-123@example.com>")
        self.assertEqual(self.smtp_args,
                         ("smtp.example.com", 465, mailread.SMTP_TIMEOUT))
        self.assertEqual(self.login_args, ("owner@example.com", "app-pw"))

    def test_passive_blocks_the_send(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(RuntimeError):
                mailread.send_reply("owner@example.com", "hi",
                                    to="pat@example.com")
        self.assertEqual(self.sent, [])

    def test_empty_reply_refused(self):
        with self.assertRaises(RuntimeError):
            mailread.send_reply("owner@example.com", "   ",
                                to="pat@example.com")

    def test_missing_recipient_refused(self):
        with self.assertRaises(RuntimeError):
            mailread.send_reply("owner@example.com", "hi", to=None)


class GraphReplyTests(unittest.TestCase):
    def setUp(self):
        p = _accounts_patch()
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("VIRA_PASSIVE", None)

    def test_sent_when_scope_consented(self):
        calls = []

        def fake(addr, path, method="GET", payload=None, scope=None, **kw):
            calls.append((path, method, scope))
            return {}

        with mock.patch.object(mailread.msgraph, "_graph_request", fake):
            r = mailread.send_reply("work@example.com", "on it\nthanks",
                                    graph_id="AAQkAD-full")
        self.assertTrue(r["sent"])
        path, method, scope = calls[0]
        self.assertEqual(path, "/me/messages/AAQkAD-full/reply")
        self.assertEqual(method, "POST")
        self.assertEqual(scope, mailread.SCOPE_SEND)

    def test_unconsented_scope_degrades_to_reply_draft(self):
        calls = []

        def fake(addr, path, method="GET", payload=None, scope=None, **kw):
            calls.append((path, scope))
            if scope == mailread.SCOPE_SEND:
                raise RuntimeError(
                    "token refresh failed: AADSTS65001: The user or "
                    "administrator has not consented")
            return {}

        with mock.patch.object(mailread.msgraph, "_graph_request", fake):
            r = mailread.send_reply("work@example.com", "on it",
                                    graph_id="AAQkAD-full")
        self.assertFalse(r["sent"])
        self.assertTrue(r["drafted"])
        self.assertIn("Mail.Send", r["note"])
        self.assertEqual(calls[-1][0], "/me/messages/AAQkAD-full/createReply")

    def test_other_graph_failure_raises(self):
        def fake(addr, path, method="GET", payload=None, scope=None, **kw):
            raise RuntimeError("token refresh failed: something else")

        with mock.patch.object(mailread.msgraph, "_graph_request", fake):
            with self.assertRaises(RuntimeError):
                mailread.send_reply("work@example.com", "on it",
                                    graph_id="AAQkAD-full")

    def test_comment_is_escaped_html(self):
        self.assertEqual(mailread._comment_html("a<b\nc & d"),
                         "a&lt;b<br>c &amp; d")


class GraphFetchTests(unittest.TestCase):
    def test_lookup_by_internet_message_id(self):
        captured = {}

        def fake(addr, path, method="GET", payload=None, scope=None, **kw):
            captured["path"] = path
            return {"value": [{
                "id": "AAQkAD-full",
                "subject": "Q3 numbers",
                "from": {"emailAddress": {"name": "Sam Example",
                                          "address": "Sam@Example.com"}},
                "toRecipients": [
                    {"emailAddress": {"address": "work@example.com"}}],
                "ccRecipients": [],
                "replyTo": [],
                "receivedDateTime": "2026-08-04T14:00:00Z",
                "body": {"contentType": "html",
                         "content": "<p>see attached</p>"},
                "internetMessageId": "<graph-1@example.com>",
            }]}

        with _accounts_patch(), \
                mock.patch.object(mailread.msgraph, "_graph_request", fake):
            m = mailread.get_message("work@example.com",
                                     message_id="<graph-1@example.com>")
        self.assertEqual(m["kind"], "graph")
        self.assertEqual(m["graph_id"], "AAQkAD-full")
        self.assertEqual(m["from_addr"], "sam@example.com")
        self.assertEqual(m["text"], "see attached")
        self.assertIn("<p>see attached</p>", m["html"])
        self.assertIn("internetMessageId%20eq", captured["path"])

    def test_no_message_id_is_a_named_refusal(self):
        with _accounts_patch():
            with self.assertRaises(RuntimeError):
                mailread.get_message("work@example.com",
                                     rowid="mail-work@example.com-abc")


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.main = main
        cls.client = TestClient(main.app)

    def test_message_route_maps_errors(self):
        with mock.patch.object(self.main.mailread, "get_message",
                               side_effect=ValueError("unknown mail account")):
            r = self.client.get("/api/mail/message",
                                params={"account": "x@example.com"})
        self.assertEqual(r.status_code, 404)
        with mock.patch.object(self.main.mailread, "get_message",
                               return_value={"subject": "ok"}):
            r = self.client.get("/api/mail/message",
                                params={"account": "x@example.com",
                                        "mid": "<a@b>"})
        self.assertEqual(r.json()["subject"], "ok")

    def test_reply_route_passive_403(self):
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            r = self.client.post("/api/mail/reply", json={
                "account": "x@example.com", "text": "hi"})
        self.assertEqual(r.status_code, 403)

    def test_reply_route_passes_through(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIRA_PASSIVE", None)
            with mock.patch.object(self.main.mailread, "send_reply",
                                   return_value={"sent": True}) as sr:
                r = self.client.post("/api/mail/reply", json={
                    "account": "x@example.com", "text": "hi",
                    "to": "y@example.com", "subject": "s",
                    "message_id": "<m@x>", "graph_id": None})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["sent"])
        kwargs = sr.call_args.kwargs
        self.assertEqual(kwargs["to"], "y@example.com")
        self.assertEqual(kwargs["message_id"], "<m@x>")


if __name__ == "__main__":
    unittest.main()
