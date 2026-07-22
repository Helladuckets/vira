"""branch.sh worktree resolution — finding a branch's worktree wherever it is.

`start` creates worktrees at ../vira-<slug>, but the app's worktree toggle
creates them under .claude/worktrees/<slug>. wt_dir used to hardcode the
former, so serve/stop/discard died with "no worktree at ../vira-<slug>" on
every worktree the script hadn't made itself — and discard, finding no
directory to remove, fell through to a branch delete that git refuses while
the branch is checked out somewhere.

These tests build real throwaway git repos with worktrees in both layouts.

Run: .venv/bin/python -m unittest tests.test_branch_worktree
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

BRANCH_SH = Path(__file__).resolve().parents[1] / "scripts" / "branch.sh"


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


def run_in(live: Path, body: str) -> subprocess.CompletedProcess:
    """Source branch.sh from inside `live` so it resolves that checkout."""
    return subprocess.run(
        ["/bin/zsh", "-c", f'source "{BRANCH_SH}"\n{body}\n'],
        cwd=live, capture_output=True, text=True)


class WorktreeResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # resolve(): macOS hands out /var/... temp dirs that git reports back
        # as /private/var/..., and the paths are compared as strings here
        self.root = Path(self.tmp.name).resolve()
        self.live = self.root / "vira"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "base", cwd=self.live)
        # what the live checkout provisions from
        (self.live / "CLAUDE.md").write_text("the operational spec")
        (self.live / ".venv").mkdir()
        (self.live / ".claude").mkdir()
        (self.live / ".claude" / "launch.json").write_text("{}")

    def _add_worktree(self, path: Path, slug: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "-q", "-b", f"claude/{slug}", str(path), "main",
            cwd=self.live)

    def test_resolves_harness_style_worktree(self):
        wt = self.live / ".claude" / "worktrees" / "feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'wt_dir feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(wt))

    def test_resolves_canonical_sibling_worktree(self):
        wt = self.root / "vira-feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'wt_dir feat')
        self.assertEqual(r.stdout.strip(), str(wt))

    def test_unknown_slug_falls_back_to_canonical_path(self):
        # `start` needs a path to create, and merge/discard accept a branch
        # whose worktree is already gone
        r = run_in(self.live, 'wt_dir nope')
        self.assertEqual(r.stdout.strip(), str(self.root / "vira-nope"))

    def test_discard_removes_a_harness_worktree_and_its_branch(self):
        wt = self.live / ".claude" / "worktrees" / "feat"
        self._add_worktree(wt, "feat")
        r = run_in(self.live, 'cmd_discard feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(wt.exists())
        branches = git("branch", "--format=%(refname:short)",
                       cwd=self.live).stdout.split()
        self.assertNotIn("claude/feat", branches)


class Provisioning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # resolve(): macOS hands out /var/... temp dirs that git reports back
        # as /private/var/..., and the paths are compared as strings here
        self.root = Path(self.tmp.name).resolve()
        self.live = self.root / "vira"
        self.live.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=self.live)
        git("-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "base", cwd=self.live)
        (self.live / "CLAUDE.md").write_text("the operational spec")
        (self.live / ".venv").mkdir()
        (self.live / ".claude").mkdir()
        (self.live / ".claude" / "launch.json").write_text("{}")
        self.wt = self.live / ".claude" / "worktrees" / "feat"
        self.wt.parent.mkdir(parents=True)
        git("worktree", "add", "-q", "-b", "claude/feat", str(self.wt), "main",
            cwd=self.live)

    def test_adopt_installs_the_gitignored_pieces(self):
        # the state a harness-made worktree starts in: no spec, no venv
        self.assertFalse((self.wt / "CLAUDE.md").exists())
        self.assertFalse((self.wt / ".venv").exists())
        r = run_in(self.live, 'cmd_adopt feat')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.wt / "CLAUDE.md").read_text(),
                         "the operational spec")
        self.assertTrue((self.wt / ".venv").is_symlink())
        self.assertEqual((self.wt / ".venv").resolve(),
                         (self.live / ".venv").resolve())
        self.assertTrue((self.wt / ".claude" / "launch.json").exists())

    def test_provision_never_clobbers_worktree_edits(self):
        (self.wt / "CLAUDE.md").write_text("edited in this worktree")
        r = run_in(self.live, f'provision "{self.wt}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.wt / "CLAUDE.md").read_text(),
                         "edited in this worktree")

    def test_provision_is_idempotent(self):
        for _ in range(2):
            r = run_in(self.live, f'provision "{self.wt}"')
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.wt / ".venv").is_symlink())

    def test_adopt_refuses_the_live_tree(self):
        r = run_in(self.live, 'cmd_adopt')     # no slug = adopt cwd
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("live tree", r.stderr)


if __name__ == "__main__":
    unittest.main()
