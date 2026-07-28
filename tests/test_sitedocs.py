"""The documents-merge: site plans + ops dossiers become Vira-served docs.

The behaviours worth pinning, each a decision:
  1. migrate() carries the site's own metadata (plans.json titles/dates) and
     provenance (the inverted _provenance index) — a migrated document keeps
     its history, it does not restart it.
  2. Producer registration mirrors backfill's freshness rule: first-sight
     entries older than FRESH_DAYS are filed as already read, never queued —
     58 historical plans must not flood the queue.
  3. registry_rows() folds the hook manifest, so a plan the repointed
     plan-mode hook wrote five minutes ago is sweepable before any sitedocs
     command has run.
  4. Everything is idempotent: re-running migrate/sweep duplicates nothing
     and never resurrects a read entry.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from server import readinglist, sitedocs


def _days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.site = root / "site"
        self.docs = root / "docs"
        self.vault_plans = root / "vault" / "plans"
        self.store = root / "reading-list.json"
        for p in (mock.patch.object(sitedocs, "DOCS_DIR", self.docs),
                  mock.patch.object(readinglist, "STORE", self.store)):
            p.start()
            self.addCleanup(p.stop)

    def build_site(self, *, old_days=30, fresh_days=2):
        """A synthetic site tree: two plans (one stale, one fresh), a src md,
        a dossier dir, and a provenance index."""
        plans = self.site / "lab" / "plans"
        plans.mkdir(parents=True)
        self.old_plan = f"{_days_ago(old_days)}-1200-widget-rework.html"
        self.new_plan = f"{_days_ago(fresh_days)}-0900-fresh-idea.html"
        (plans / self.old_plan).write_text(
            "<html><head><title>Widget Rework</title></head><body>old</body></html>",
            encoding="utf-8")
        (plans / self.new_plan).write_text(
            "<html><head><title>Fresh Idea</title></head><body>new</body></html>",
            encoding="utf-8")
        (plans / "plans.json").write_text(json.dumps([
            {"filename": self.old_plan, "title": "Widget Rework",
             "summary": "Rework the widget.", "date": _days_ago(old_days)},
            {"filename": self.new_plan, "title": "Fresh Idea",
             "summary": "A fresh idea.", "date": _days_ago(fresh_days)},
        ]), encoding="utf-8")
        src = plans / "src"
        src.mkdir()
        (src / (self.old_plan[:-5] + ".md")).write_text(
            "# Widget Rework\n\nbody\n", encoding="utf-8")
        d = self.site / "lab" / "vira-audit"
        d.mkdir(parents=True)
        (d / "index.html").write_text(
            "<html><head><title>Module Audit</title></head><body>a</body></html>",
            encoding="utf-8")
        (d / "extra.css").write_text("body{}", encoding="utf-8")
        prov = self.site / "lab" / "_provenance"
        prov.mkdir(parents=True)
        (prov / "index.json").write_text(json.dumps({
            "(Claude Code session — commit abc1234)": {
                "count": 1, "role": "process",
                "pages": [f"/lab/plans/{self.old_plan}"]},
            "audit sweep": {
                "count": 1, "role": "process",
                "pages": ["/lab/vira-audit/index.html"]},
        }), encoding="utf-8")

    def migrate(self):
        return sitedocs.migrate(site=self.site, docs_dir=self.docs,
                                vault_plans=self.vault_plans)


class MigrateTests(Base):
    def test_copies_plans_and_dossiers_with_metadata(self):
        self.build_site()
        out = self.migrate()
        self.assertEqual(out["copied"]["plans"], 2)
        self.assertTrue((self.docs / "plans" / self.old_plan).is_file())
        self.assertTrue((self.docs / "vira-audit" / "index.html").is_file())
        self.assertTrue((self.docs / "vira-audit" / "extra.css").is_file())
        reg = json.loads((self.docs / "registry.json").read_text(encoding="utf-8"))
        titles = {e["title"] for e in reg["items"]}
        self.assertIn("Widget Rework", titles)
        self.assertIn("Module Audit", titles)

    def test_provenance_inverted_and_alias_recorded(self):
        self.build_site()
        self.migrate()
        prov = json.loads((self.docs / "provenance.json").read_text(encoding="utf-8"))
        entry = prov[f"/docs/plans/{self.old_plan}"]
        self.assertEqual(entry["site_url"],
                         f"https://thedurham.nyc/lab/plans/{self.old_plan}")
        self.assertIn("(Claude Code session — commit abc1234)", entry["sources"])
        self.assertIn("audit sweep", prov["/docs/vira-audit/"]["sources"])

    def test_stale_filed_read_fresh_queued(self):
        self.build_site()
        out = self.migrate()
        self.assertEqual(out["registered"], 3)
        # The 30-day-old plan files as read; the fresh plan queues; the
        # dossier's created date is its index.html mtime — today in this
        # fixture — so it queues too.
        self.assertEqual(out["filed_read"], 1)
        self.assertEqual(out["queued"], 2)
        old = readinglist.find_by_locator(f"/docs/plans/{self.old_plan}", "url")
        self.assertIsNotNone(old["completed"])

    def test_rerun_is_idempotent_and_never_resurrects(self):
        self.build_site()
        self.migrate()
        row = readinglist.find_by_locator(f"/docs/plans/{self.new_plan}", "url")
        readinglist.complete(row["id"])
        out = self.migrate()
        self.assertEqual(out["registered"], 0)
        self.assertEqual(out["new_in_registry"], 0)
        row = readinglist.find_by_locator(f"/docs/plans/{self.new_plan}", "url")
        self.assertIsNotNone(row["completed"])

    def test_vault_md_copied_unless_passive(self):
        self.build_site()
        self.migrate()
        self.assertTrue(
            (self.vault_plans / (self.old_plan[:-5] + ".md")).is_file())

    def test_vault_md_skipped_under_passive(self):
        self.build_site()
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            out = self.migrate()
        self.assertEqual(out["copied"]["vault_md"], 0)
        self.assertFalse(self.vault_plans.exists())

    def test_missing_site_reports_error(self):
        # site=None means "use the configured site_root" — which EXISTS on
        # the owner's machine (and on any worktree whose data/ was cloned by
        # branch.sh serve), so the config fallback must be pinned shut or
        # this test asserts against whatever the host happens to have.
        with mock.patch.object(sitedocs, "site_root", return_value=None):
            out = sitedocs.migrate(site=None, docs_dir=self.docs)
        self.assertIn("error", out)

    def test_orphan_html_migrates_with_stem_title(self):
        self.build_site()
        (self.site / "lab" / "plans" / "2026-01-05-0800-orphan.html").write_text(
            "<html><body>o</body></html>", encoding="utf-8")
        self.migrate()
        row = readinglist.find_by_locator("/docs/plans/2026-01-05-0800-orphan.html",
                                          "url")
        self.assertIsNotNone(row)
        self.assertIn("orphan", row["title"])


class SweepTests(Base):
    def test_manifest_plans_fold_into_rows_and_sweep(self):
        self.build_site()
        self.migrate()
        plans = self.docs / "plans"
        fn = f"{_days_ago(0)}-1400-hook-born.html"
        (plans / fn).write_text("<html><body>h</body></html>", encoding="utf-8")
        (plans / "manifest.json").write_text(json.dumps([
            {"filename": fn, "title": "Hook Born",
             "summary": "s", "date": _days_ago(0)},
        ]), encoding="utf-8")
        rows = sitedocs.registry_rows(self.docs)
        self.assertIn("Hook Born", {r["title"] for r in rows})
        with mock.patch.object(sitedocs, "DOCS_DIR", self.docs):
            out = sitedocs.sweep()
        self.assertEqual(out["registered"], 1)
        self.assertEqual(out["queued"], 1)
        row = readinglist.find_by_locator(f"/docs/plans/{fn}", "url")
        self.assertIsNone(row["completed"])

    def test_manifest_entry_without_file_is_ignored(self):
        (self.docs / "plans").mkdir(parents=True)
        (self.docs / "plans" / "manifest.json").write_text(json.dumps([
            {"filename": "ghost.html", "title": "Ghost", "date": "2026-01-01"},
        ]), encoding="utf-8")
        self.assertEqual(sitedocs.registry_rows(self.docs), [])


class IndexTests(Base):
    def test_index_written_and_escaped(self):
        self.build_site()
        (self.site / "lab" / "plans" / "plans.json").write_text(json.dumps([
            {"filename": self.old_plan, "title": "Widget <Rework> & Co",
             "summary": "s", "date": _days_ago(30)},
        ]), encoding="utf-8")
        self.migrate()
        page = (self.docs / "index.html").read_text(encoding="utf-8")
        self.assertIn("Widget &lt;Rework&gt; &amp; Co", page)
        self.assertIn("color-scheme", page)          # standalone-document rule
        self.assertIn("::-webkit-scrollbar", page)


class BackfillIntegrationTests(Base):
    def test_backfill_sweeps_sitedocs_rows(self):
        self.build_site()
        # Populate registry WITHOUT registering (simulate a machine where
        # migrate ran but the reading list was wiped/rebuilt).
        with mock.patch.object(sitedocs, "_register_direct",
                               return_value={"registered": 0, "queued": 0,
                                             "filed_read": 0, "errors": 0}):
            self.migrate()
        self.assertIsNone(
            readinglist.find_by_locator(f"/docs/plans/{self.new_plan}", "url"))
        for name in ("_explainer_dossiers", "_plans", "_vault_documents"):
            p = mock.patch.object(readinglist, name, return_value=[])
            p.start()
            self.addCleanup(p.stop)
        out = readinglist.backfill()
        self.assertGreaterEqual(out["added"], 3)
        row = readinglist.find_by_locator(f"/docs/plans/{self.new_plan}", "url")
        self.assertIsNotNone(row)
        self.assertIsNone(row["completed"])          # fresh → queued
        old = readinglist.find_by_locator(f"/docs/plans/{self.old_plan}", "url")
        self.assertIsNotNone(old["completed"])       # stale → filed read


if __name__ == "__main__":
    unittest.main()
