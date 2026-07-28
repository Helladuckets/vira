"""scripts/preflight.sh — the executable form of the process's own lessons.

The most important test here is REGISTRY CONTRACT: every check must carry a
description, the incident that earned it, and a fix. That is what stops this
from decaying back into prose — a check nobody can act on is a complaint, and a
check with no incident behind it is a style rule that will be argued about.

Run: .venv/bin/python -m unittest tests.test_preflight
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "preflight.sh"
DEPS = ROOT / "scripts" / "preflight_deps.py"
BASH = shutil.which("bash") or "/bin/bash"

posix_only = unittest.skipUnless(os.name == "posix", "bash script")


def run(*args, cwd=ROOT, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["bash", str(PREFLIGHT), *args], cwd=str(cwd),
                          capture_output=True, text=True, env=e)


@posix_only
class RegistryContract(unittest.TestCase):
    """Adding a lesson means adding a ROW — and a row is all four parts."""

    def setUp(self):
        self.src = PREFLIGHT.read_text(encoding="utf-8")
        m = re.search(r"^CHECKS=\(([^)]*)\)", self.src, re.M)
        self.assertIsNotNone(m, "CHECKS registry not found")
        self.ids = m.group(1).split()
        self.assertTrue(self.ids, "registry is empty")

    def _has(self, pattern):
        # re.M: these are line-anchored declarations. assertRegex would not
        # only miss them, it would dump the whole script into the failure.
        return re.search(pattern, self.src, re.M) is not None

    def test_every_check_has_all_four_parts(self):
        for cid in self.ids:
            for part in ("desc", "incident", "fix"):
                self.assertTrue(
                    self._has(rf"^{part}_{cid}="),
                    f"check '{cid}' has no {part}_ — a check without one is not "
                    f"actionable, see the header of preflight.sh")
            self.assertTrue(self._has(rf"^check_{cid}\(\)"),
                            f"check '{cid}' is registered but not implemented")

    def test_every_incident_is_dated(self):
        """An incident without a date is a hunch. Each must cite when it bit."""
        for cid in self.ids:
            m = re.search(rf'^incident_{cid}="(.*?)"', self.src, re.M | re.S)
            self.assertIsNotNone(m, cid)
            self.assertRegex(m.group(1), r"\d{4}-\d{2}-\d{2}",
                             f"incident_{cid} cites no date")

    def test_list_names_every_check(self):
        r = run("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        for cid in self.ids:
            self.assertIn(cid, r.stdout)


@posix_only
class DepsCheck(unittest.TestCase):
    """The Pillow class: a module the code imports but nothing declares."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "server").mkdir()
        (self.root / "tests").mkdir()
        shutil.copy(DEPS, self.root / "scripts" / "preflight_deps.py")
        (self.root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    def _run(self):
        return subprocess.run(
            ["python3", str(self.root / "scripts" / "preflight_deps.py")],
            capture_output=True, text=True)

    def test_undeclared_import_fails_and_names_the_file(self):
        (self.root / "server" / "x.py").write_text(
            "import fastapi\nimport nowhere_declared\n", encoding="utf-8")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("nowhere_declared", r.stdout)
        self.assertIn("server/x.py", r.stdout)

    def test_declared_import_passes(self):
        (self.root / "server" / "x.py").write_text("import fastapi\n", encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)

    def test_stdlib_and_relative_imports_are_not_flagged(self):
        (self.root / "server" / "x.py").write_text(
            "import json, os, sqlite3\nfrom . import sibling\n", encoding="utf-8")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_alias_maps_import_name_to_distribution(self):
        """PIL is declared as 'pillow' — the incident that started this."""
        (self.root / "requirements.txt").write_text("pillow\n", encoding="utf-8")
        (self.root / "server" / "x.py").write_text("from PIL import Image\n",
                                                   encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)

    def test_extras_marker_does_not_hide_a_real_import(self):
        """uvicorn[standard] declares 'uvicorn'."""
        (self.root / "requirements.txt").write_text("uvicorn[standard]\n", encoding="utf-8")
        (self.root / "server" / "x.py").write_text("import uvicorn\n", encoding="utf-8")
        self.assertEqual(self._run().returncode, 0)


@posix_only
class Ratchet(unittest.TestCase):
    """Pre-existing debt is tolerated; NEW debt is not."""

    def test_baseline_matches_reality_right_now(self):
        """If this drifts, the ratchet is silently either useless or blocking."""
        r = run("encoding")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("at baseline", r.stdout,
                      "baseline is stale — update scripts/preflight-baseline.txt")

    def test_baseline_file_is_parseable_and_nonzero(self):
        txt = (ROOT / "scripts" / "preflight-baseline.txt").read_text(encoding="utf-8")
        rows = [l.split() for l in txt.splitlines()
                if l.strip() and not l.startswith("#")]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 2, row)
            self.assertTrue(row[1].isdigit(), row)


@posix_only
class BaseCheck(unittest.TestCase):
    """The incident this whole file exists because of: a branch whose base was
    rewritten out from under it. A plain merge contributed 146 commits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "server").mkdir()
        (self.root / "tests").mkdir()
        for f in ("preflight.sh", "preflight-baseline.txt",
                  "preflight_deps.py", "preflight_encoding.py"):
            shutil.copy(ROOT / "scripts" / f, self.root / "scripts" / f)
        shutil.copy(ROOT / "scripts" / "check-pii.sh", self.root / "scripts")
        (self.root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self.base = self._out("rev-parse", "main")

    def _git(self, *a):
        return subprocess.run(["git", *a], cwd=str(self.root),
                              capture_output=True, text=True)

    def _out(self, *a):
        return self._git(*a).stdout.strip()

    def _record(self, slug, sha):
        d = self.root / ".git" / "vira-bases"
        d.mkdir(parents=True, exist_ok=True)
        (d / slug).write_text(sha, encoding="utf-8")

    def _preflight(self, slug):
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "preflight.sh"), "base"],
            cwd=str(self.root), capture_output=True, text=True,
            env={**os.environ, "PREFLIGHT_SLUG": slug})

    def test_live_base_passes(self):
        self._git("branch", "claude/feat", "main")
        self._record("feat", self.base)
        r = self._preflight("feat")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("still in main", r.stdout)

    def test_rewritten_base_fails_and_prints_the_onto_fix(self):
        """main is rewritten; the branch's recorded base is no longer in it."""
        self._git("branch", "claude/feat", "main")
        self._record("feat", self.base)
        # rewrite main: amend the root so every sha changes
        self._git("commit", "-q", "--amend", "-m", "base (rewritten)")
        r = self._preflight("feat")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("history was rewritten", r.stdout)
        self.assertIn("--onto", r.stdout)
        self.assertNotIn("git rebase main\n", r.stdout)

    def test_unrecorded_base_falls_back_to_the_count_tell(self):
        """Legacy branches have no record; a huge contribution is the signal."""
        self._git("branch", "claude/legacy", "main")
        self._git("checkout", "-q", "claude/legacy")
        for i in range(30):
            (self.root / f"f{i}").write_text("x", encoding="utf-8")
            self._git("add", "-A")
            self._git("commit", "-qm", f"c{i}")
        self._git("checkout", "-q", "main")
        r = self._preflight("legacy")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("base is almost certainly dead", r.stdout)


@posix_only
class CiCheck(unittest.TestCase):
    """2026-07-28: red CI blocked nothing, so red CI changed nothing — two
    branches merged with a failing Windows job hours apart. This check is the
    close. Its hard part is not detecting failure, it is NOT crying wolf: a
    run in flight, an unpushed commit, and CI grading itself are all normal
    states that must never block a merge, or the gate gets routed around and
    the real red goes with it. gh is stubbed so these are offline and exact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        shutil.copy(PREFLIGHT, self.root / "scripts" / "preflight.sh")
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(self.root))
        for k, v in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "config", k, v], cwd=str(self.root))
        (self.root / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.root))
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(self.root))

    def _stub_gh(self, payload):
        """A fake gh that answers `run list` with `payload` and `run view`
        with one failing job line."""
        (self.bin / "runs.json").write_text(payload, encoding="utf-8")
        gh = self.bin / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            f'  *"run list"*) cat {self.bin / "runs.json"} ;;\n'
            '  *"run view"*) printf "        failure\\ttest-windows\\n" ;;\n'
            "esac\n", encoding="utf-8")
        gh.chmod(0o755)

    def _run(self, path=None):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        env["PATH"] = (f"{self.bin}:{os.environ['PATH']}" if path is None
                       else path)
        # bash by absolute path: one case deliberately empties PATH, and
        # resolving the interpreter through it would fail the test for the
        # wrong reason.
        return subprocess.run(
            [BASH, str(self.root / "scripts" / "preflight.sh"), "ci"],
            cwd=str(self.root), capture_output=True, text=True, env=env)

    def test_a_red_run_blocks_and_names_the_failing_job(self):
        self._stub_gh('[{"status":"completed","conclusion":"failure",'
                      '"databaseId":42}]')
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("CI is failure", r.stdout)
        self.assertIn("test-windows", r.stdout)
        self.assertIn("--log-failed 42", r.stdout)

    def test_a_green_run_passes(self):
        self._stub_gh('[{"status":"completed","conclusion":"success",'
                      '"databaseId":7}]')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("CI green", r.stdout)

    def test_a_run_still_in_flight_warns_but_does_not_block(self):
        self._stub_gh('[{"status":"in_progress","conclusion":null,'
                      '"databaseId":9}]')
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("WARN", r.stdout)
        self.assertIn("no verdict yet", r.stdout)

    def test_an_unpushed_commit_warns_but_does_not_block(self):
        """Working locally before a first push is normal, not an error."""
        self._stub_gh("[]")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("has not run", r.stdout)

    def test_missing_gh_warns_but_does_not_block(self):
        """A check that cannot run says so; it does not fail the merge."""
        r = self._run(path=str(self.bin))          # empty dir: no gh anywhere
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("gh is not installed", r.stdout)

    def test_inside_ci_the_check_skips_itself(self):
        """A run cannot grade itself — otherwise --all is circular in CI."""
        self._stub_gh('[{"status":"completed","conclusion":"failure",'
                      '"databaseId":42}]')
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        env["GITHUB_ACTIONS"] = "true"
        r = subprocess.run(
            ["bash", str(self.root / "scripts" / "preflight.sh"), "ci"],
            cwd=str(self.root), capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("cannot grade itself", r.stdout)

    def test_ci_is_in_the_pre_merge_gate(self):
        """The whole point: it has to run where merges happen."""
        src = PREFLIGHT.read_text(encoding="utf-8")
        m = re.search(r"^PRE_MERGE=\(([^)]*)\)", src, re.M)
        self.assertIsNotNone(m)
        self.assertIn("ci", m.group(1).split())


@posix_only
class PiiHonesty(unittest.TestCase):
    """The false comfort was the real bug: a pass must state its strength."""

    def test_pass_states_which_mode_it_ran_in(self):
        r = run("pii")
        self.assertTrue(re.search(r"FULL|REDUCED", r.stdout),
                        "the PII check must name its strength on a PASS, not "
                        "only on a failure")

    def test_reduced_mode_is_a_warning_not_a_silent_pass(self):
        if (ROOT / "data" / "pii-patterns.txt").is_file():
            self.skipTest("this checkout has the patterns file (FULL mode)")
        r = run("pii")
        self.assertIn("REDUCED", r.stdout)
        self.assertIn("does NOT mean", r.stdout)


if __name__ == "__main__":
    unittest.main()
