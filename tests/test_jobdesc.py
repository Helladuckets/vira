"""Native Applications description reader.

All posting fixtures are synthetic. No live board is contacted.

Run: .venv/bin/python -m unittest tests.test_jobdesc
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import jobdesc


class MarkdownConversionTest(unittest.TestCase):
    def test_escaped_greenhouse_markup_keeps_document_structure(self):
        raw = (
            "&lt;div&gt;&lt;h2&gt;About Example&lt;/h2&gt;"
            "&lt;p&gt;Build useful systems.&lt;/p&gt;"
            "&lt;ul&gt;&lt;li&gt;Own the roadmap&lt;/li&gt;"
            "&lt;li&gt;Ship the work&lt;/li&gt;&lt;/ul&gt;&lt;/div&gt;"
        )
        out = jobdesc.to_markdown(raw)
        self.assertIn("## About Example", out)
        self.assertIn("Build useful systems.", out)
        self.assertIn("- Own the roadmap\n- Ship the work", out)
        self.assertNotIn("<div", out)

    def test_real_html_and_plain_text_are_both_readable(self):
        self.assertEqual(jobdesc.to_markdown("<h3>Work</h3><p>Do it.</p>"),
                         "## Work\n\nDo it.")
        self.assertEqual(jobdesc.to_markdown("First line\n\nSecond line"),
                         "First line\n\nSecond line")

    def test_output_is_bounded(self):
        self.assertEqual(len(jobdesc.to_markdown("x" * 70000)),
                         jobdesc.TEXT_CAP)


class LiveFetchTest(unittest.TestCase):
    def test_live_targets_only_single_posting_apis(self):
        self.assertEqual(
            jobdesc.live_target(
                "https://job-boards.greenhouse.io/example/jobs/123"),
            ("greenhouse", "example", "123"))
        self.assertEqual(
            jobdesc.live_target(
                "https://jobs.lever.co/example/12345678-1234-1234-1234-123456789abc"),
            ("lever", "example", "12345678-1234-1234-1234-123456789abc"))
        self.assertIsNone(jobdesc.live_target(
            "https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc"))

    def test_greenhouse_live_fetch_uses_public_posting_endpoint(self):
        payload = {"content": "&lt;h2&gt;Role&lt;/h2&gt;&lt;p&gt;Build.&lt;/p&gt;"}
        with mock.patch.object(jobdesc.jobshared, "http_get",
                               return_value=payload) as get:
            out = jobdesc.fetch_live(
                "https://job-boards.greenhouse.io/example/jobs/123")
        self.assertEqual(out, "## Role\n\nBuild.")
        get.assert_called_once_with(
            "https://boards-api.greenhouse.io/v1/boards/example/jobs/123",
            timeout=jobdesc.TIMEOUT)


class DescriptionLadderTest(unittest.TestCase):
    ROLE = {
        "uid": "g-example-123", "title": "Systems Builder",
        "company": "Example Labs",
        "url": "https://job-boards.greenhouse.io/example/jobs/123",
        "blurb": "Short catalog excerpt.",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(jobdesc, "STORE",
                                  Path(self.tmp.name) / "job-descriptions.json")
        patch.start()
        self.addCleanup(patch.stop)

    def test_snapshot_wins_without_network(self):
        with mock.patch.object(jobdesc, "_snapshot",
                               return_value=("<h2>Details</h2><p>Full text.</p>",
                                             "2026-08-06T12:00:00+00:00")), \
                mock.patch.object(jobdesc, "fetch_live") as fetch:
            out = jobdesc.describe(dict(self.ROLE))
        self.assertEqual(out["source"], "snapshot")
        self.assertEqual(out["text"], "## Details\n\nFull text.")
        self.assertTrue(out["live"])
        fetch.assert_not_called()

    def test_refresh_fetches_and_caches_live_posting(self):
        with mock.patch.object(jobdesc, "_snapshot", return_value=("old", "then")), \
                mock.patch.object(jobdesc, "fetch_live", return_value="current"), \
                mock.patch.object(jobdesc.jobshared, "now_iso",
                                   return_value="2026-08-06T13:00:00+00:00"):
            out = jobdesc.describe(dict(self.ROLE), refresh=True)
        self.assertEqual(out["source"], "live")
        self.assertEqual(out["text"], "current")
        self.assertEqual(jobdesc._cache_read()[self.ROLE["uid"]]["text"],
                         "current")

    def test_failed_refresh_falls_back_to_snapshot_and_says_why(self):
        with mock.patch.object(
                jobdesc, "_snapshot",
                return_value=("saved copy", "2026-08-06T12:00:00+00:00")), \
                mock.patch.object(jobdesc, "fetch_live",
                                  side_effect=TimeoutError):
            out = jobdesc.describe(dict(self.ROLE), refresh=True)
        self.assertEqual(out["source"], "snapshot")
        self.assertEqual(out["text"], "saved copy")
        self.assertIn("could not reach the posting", out["reason"])

    def test_non_fetchable_role_falls_back_to_honest_excerpt(self):
        role = {**self.ROLE,
                "url": "https://jobs.ashbyhq.com/example/12345678-1234-1234-1234-123456789abc"}
        with mock.patch.object(jobdesc, "_snapshot", return_value=("", "")):
            out = jobdesc.describe(role)
        self.assertEqual(out["source"], "blurb")
        self.assertTrue(out["partial"])
        self.assertFalse(out["live"])


class DescriptionRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from server import main
        cls.client = TestClient(main.app)
        cls.main = main

    def test_unknown_role_is_404(self):
        with mock.patch.object(self.main.applications, "find_role",
                               return_value=None):
            r = self.client.get("/api/applications/missing/description")
        self.assertEqual(r.status_code, 404)

    def test_route_passes_refresh_to_description_engine(self):
        role = {"uid": "g-example-123", "title": "Builder"}
        payload = {"uid": role["uid"], "title": role["title"],
                   "text": "Description", "source": "live"}
        with mock.patch.object(self.main.applications, "find_role",
                               return_value=role), \
                mock.patch.object(self.main.jobdesc, "describe",
                                  return_value=payload) as describe:
            r = self.client.get(
                "/api/applications/g-example-123/description?refresh=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), payload)
        describe.assert_called_once_with(role, refresh=True)


if __name__ == "__main__":
    unittest.main()
