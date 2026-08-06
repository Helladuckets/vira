"""Vira's blog: canonical posts here, projections to the site, gate closed.

The decisions worth pinning:
  1. The anonymization gate FAILS CLOSED — a scanner that cannot run blocks
     the publish exactly like a scanner hit, and nothing lands in the site
     repo when it does.
  2. Passive instances cannot publish (site-repo write + push).
  3. The markdown renderer is a deterministic minimal subset with HTML
     escaping everywhere text flows in.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import blog


class RendererTests(unittest.TestCase):
    def test_headings_paragraphs_and_inline(self):
        html = blog.md_to_html(
            "# Title\n\nOne **bold** and *soft* and `code`.\nSame para.\n\n"
            "## Sub\n\nNext.")
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>soft</em>", html)
        self.assertIn("<code>code</code>", html)
        self.assertIn("<p>One <strong>bold</strong> and <em>soft</em> and "
                      "<code>code</code>. Same para.</p>", html)
        self.assertIn("<h2>Sub</h2>", html)

    def test_html_is_escaped_in_text_and_fences(self):
        html = blog.md_to_html("A <script>x</script> tag.\n\n```\n<b>raw</b>\n```")
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<pre><code>&lt;b&gt;raw&lt;/b&gt;</code></pre>", html)
        self.assertNotIn("<script>", html)

    def test_links_escape_the_href(self):
        html = blog.md_to_html('See [the site](https://example.com/a"b).')
        self.assertIn('href="https://example.com/a&quot;b"', html)
        self.assertIn(">the site</a>", html)

    def test_lists_quotes_hr(self):
        html = blog.md_to_html(
            "- one\n- two\n\n1. first\n2. second\n\n> quoted line\n\n---\n")
        self.assertIn("<ul>\n<li>one</li>\n<li>two</li>\n</ul>", html)
        self.assertIn("<ol>\n<li>first</li>\n<li>second</li>\n</ol>", html)
        self.assertIn("<blockquote>\n<p>quoted line</p>\n</blockquote>", html)
        self.assertIn("<hr>", html)

    def test_unterminated_fence_still_closes(self):
        html = blog.md_to_html("```\ncode forever")
        self.assertIn("<pre><code>code forever</code></pre>", html)


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = mock.patch.object(blog, "BLOG_DIR", self.root / "blog")
        p.start()
        self.addCleanup(p.stop)


class AddPostTests(StoreBase):
    def test_add_and_collision_suffix(self):
        a = blog.add_post("Hello World", "# Hi\n\nBody.")
        b = blog.add_post("Hello World", "# Hi again\n\nBody.")
        self.assertEqual(a["slug"], "hello-world")
        self.assertEqual(b["slug"], "hello-world-2")
        self.assertEqual(a["status"], "draft")
        self.assertEqual(blog.post_md("hello-world-2").startswith("# Hi again"),
                         True)
        self.assertEqual(len(blog.list_posts()), 2)

    def test_empty_inputs_refused(self):
        with self.assertRaises(ValueError):
            blog.add_post("", "body")
        with self.assertRaises(ValueError):
            blog.add_post("Title", "   ")

    def test_add_dossier_copies_portable_bundle(self):
        source = self.root / "bundle"
        source.mkdir()
        (source / "index.html").write_text("<h1>Atlas</h1>", encoding="utf-8")
        (source / "app.js").write_text("document.body.dataset.ready='1'",
                                        encoding="utf-8")
        entry = blog.add_dossier("AI Atlas", source, summary="Signals")
        self.assertEqual(entry["kind"], "dossier")
        self.assertTrue((blog.dossier_dir("ai-atlas") / "app.js").is_file())
        self.assertEqual(blog.get_post("ai-atlas")["status"], "draft")

    def test_add_dossier_requires_index_and_rejects_symlink(self):
        source = self.root / "bundle"
        source.mkdir()
        with self.assertRaises(ValueError):
            blog.add_dossier("Missing", source)
        (source / "index.html").write_text("ok", encoding="utf-8")
        target = self.root / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        try:
            (source / "outside.txt").symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            blog.add_dossier("Linked", source)

    def test_post_and_dossier_share_one_slug_namespace(self):
        source = self.root / "bundle"
        source.mkdir()
        (source / "index.html").write_text("ok", encoding="utf-8")
        self.assertEqual(blog.add_post("Same title", "body")["slug"],
                         "same-title")
        self.assertEqual(blog.add_dossier("Same title", source)["slug"],
                         "same-title-2")

    def test_render_post_is_a_standalone_document(self):
        e = blog.add_post("A <Post> & Title", "Text.")
        page = blog.render_post(e, "Text.")
        self.assertIn("A &lt;Post&gt; &amp; Title", page)
        self.assertIn("By Vira", page)
        self.assertIn("color-scheme", page)
        self.assertIn("::-webkit-scrollbar", page)


def _git(repo, *args):
    import subprocess
    res = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError(f"git {args}: {res.stderr}")
    return res.stdout


class PublishTests(StoreBase):
    def setUp(self):
        super().setUp()
        self.bare = self.root / "origin.git"
        self.site = self.root / "site"
        _git(self.root, "init", "--bare", str(self.bare))
        self.site.mkdir()
        _git(self.site, "init")
        _git(self.site, "config", "user.email", "owner@example.com")
        _git(self.site, "config", "user.name", "Owner")
        _git(self.site, "checkout", "-b", "main")
        (self.site / "README.md").write_text("site\n", encoding="utf-8")
        _git(self.site, "add", "README.md")
        _git(self.site, "commit", "-m", "init")
        _git(self.site, "remote", "add", "origin", str(self.bare))
        _git(self.site, "push", "-u", "origin", "main")
        p = mock.patch.object(blog.settings, "get",
                              side_effect=lambda k: str(self.site)
                              if k == "site_root" else "")
        p.start()
        self.addCleanup(p.stop)

    def test_publish_stages_scans_commits_pushes(self):
        blog.add_post("First Light", "# Hello\n\nA post.", summary="The first.")
        with mock.patch.object(blog, "_anon_scan",
                               return_value=(True, "clean")) as scan:
            out = blog.publish("first-light")
        self.assertTrue(scan.called)
        self.assertEqual(out["url"],
                         "https://thedurham.nyc/lab/blog/first-light/")
        page = (self.site / "lab" / "blog" / "first-light" / "index.html")
        self.assertTrue(page.is_file())
        idx = json.loads((self.site / "lab" / "blog" / "blog.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(idx[0]["slug"], "first-light")
        self.assertIn("add: blog/first-light", _git(self.site, "log", "-1",
                                                    "--format=%s"))
        # push landed on the bare origin
        self.assertIn("add: blog/first-light",
                      _git(self.root, "--git-dir", str(self.bare),
                           "log", "-1", "--format=%s", "main"))
        self.assertEqual(blog.get_post("first-light")["status"], "published")

    def test_publish_dossier_copies_all_assets(self):
        source = self.root / "bundle"
        source.mkdir()
        (source / "index.html").write_text("<h1>Atlas</h1>", encoding="utf-8")
        (source / "signals.html").write_text("<h1>Signals</h1>", encoding="utf-8")
        (source / "app.js").write_text("document.body.dataset.ready='1'",
                                        encoding="utf-8")
        blog.add_dossier("AI Atlas", source, summary="Signals")
        with mock.patch.object(blog, "_anon_scan", return_value=(True, "clean")):
            out = blog.publish("ai-atlas", push=False)
        dest = self.site / "lab" / "blog" / "ai-atlas"
        self.assertTrue((dest / "index.html").is_file())
        self.assertTrue((dest / "signals.html").is_file())
        self.assertTrue((dest / "app.js").is_file())
        self.assertIn("ai-atlas", out["url"])

    def test_republish_dossier_removes_stale_assets(self):
        source = self.root / "bundle"
        source.mkdir()
        (source / "index.html").write_text("<h1>Atlas</h1>", encoding="utf-8")
        (source / "stale.js").write_text("old", encoding="utf-8")
        blog.add_dossier("AI Atlas", source)
        with mock.patch.object(blog, "_anon_scan", return_value=(True, "clean")):
            blog.publish("ai-atlas", push=False)
        (blog.dossier_dir("ai-atlas") / "stale.js").unlink()
        (blog.dossier_dir("ai-atlas") / "fresh.js").write_text(
            "new", encoding="utf-8")
        with mock.patch.object(blog, "_anon_scan", return_value=(True, "clean")):
            blog.publish("ai-atlas", push=False)
        dest = self.site / "lab" / "blog" / "ai-atlas"
        self.assertFalse((dest / "stale.js").exists())
        self.assertTrue((dest / "fresh.js").is_file())

    def test_scan_failure_blocks_and_stages_nothing(self):
        blog.add_post("Leaky", "Contains something private.")
        with mock.patch.object(blog, "_anon_scan",
                               return_value=(False, "HIT: a real name")):
            with self.assertRaises(RuntimeError) as ctx:
                blog.publish("leaky")
        self.assertIn("anonymization gate", str(ctx.exception))
        self.assertFalse((self.site / "lab" / "blog" / "leaky").exists())
        self.assertEqual(blog.get_post("leaky")["status"], "draft")

    def test_domain_only_hits_are_excused_but_mixed_hits_are_not(self):
        domain_only = ("  [text/pii-pattern] /tmp/x/index.html\n"
                       "      'thedurham'  in: Part of thedurham.nyc\n")
        mixed = domain_only + "      'Realname'  in: said Realname today\n"
        fake = mock.Mock()
        with mock.patch.object(blog.subprocess, "run",
                               return_value=mock.Mock(returncode=1,
                                                      stdout=domain_only,
                                                      stderr="")):
            ok, report = blog._anon_scan(self.root)
        self.assertTrue(ok)
        self.assertIn("accepted false positive", report)
        with mock.patch.object(blog.subprocess, "run",
                               return_value=mock.Mock(returncode=1,
                                                      stdout=mixed,
                                                      stderr="")):
            ok, _ = blog._anon_scan(self.root)
        self.assertFalse(ok)
        del fake

    def test_scanner_unable_to_run_fails_closed(self):
        blog.add_post("Gate Down", "Body.")
        # The real _anon_scan with a nonexistent scanner module inside this
        # venv returns ok=False (nonzero rc); simulate the harder case — the
        # subprocess itself failing to launch.
        with mock.patch.object(blog.subprocess, "run",
                               side_effect=OSError("no python")):
            ok, report = blog._anon_scan(self.root)
        self.assertFalse(ok)
        self.assertIn("scanner failed to run", report)

    def test_passive_refuses(self):
        blog.add_post("Nope", "Body.")
        with mock.patch.dict(os.environ, {"VIRA_PASSIVE": "1"}):
            with self.assertRaises(RuntimeError) as ctx:
                blog.publish("nope")
        self.assertIn("passive", str(ctx.exception))

    def test_unknown_slug_and_missing_site(self):
        with self.assertRaises(ValueError):
            blog.publish("ghost")
        blog.add_post("Homeless", "Body.")
        with mock.patch.object(blog.settings, "get", return_value=""):
            with mock.patch.object(blog, "_anon_scan",
                                   return_value=(True, "clean")):
                with self.assertRaises(RuntimeError) as ctx:
                    blog.publish("homeless")
        self.assertIn("site_root", str(ctx.exception))

    def test_second_publish_keeps_both_in_site_index(self):
        blog.add_post("One", "Body 1.", date="2026-07-20")
        blog.add_post("Two", "Body 2.", date="2026-07-27")
        with mock.patch.object(blog, "_anon_scan", return_value=(True, "clean")):
            blog.publish("one")
            blog.publish("two")
        idx = json.loads((self.site / "lab" / "blog" / "blog.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual([p["slug"] for p in idx], ["two", "one"])


if __name__ == "__main__":
    unittest.main()
