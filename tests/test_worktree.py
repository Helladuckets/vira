"""The branch-first guard.

These tests pin the behaviour that would have prevented the 2026-07-25
incident: a Vira-dispatched session edited static/index.html in the LIVE
checkout, left the change half-applied, and the next restart came up with
no dock and no layout button.

The guard is deliberately two-part — place the session in a worktree, then
REFUSE writes aimed back at the live tree — because placement alone does not
hold: an agent can still name an absolute path.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from server import worktree


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=False)


class PathGuard(unittest.TestCase):
    """violates() — the question the permission gate asks on every write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = Path(self.tmp.name) / "repo"
        (self.live / "server").mkdir(parents=True)
        self.wt = Path(self.tmp.name) / "repo-feature"
        (self.wt / "server").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_write_into_the_live_tree_is_refused(self):
        self.assertTrue(worktree.violates(
            str(self.live / "static" / "index.html"), self.live, self.wt))

    def test_a_write_into_the_worktree_is_allowed(self):
        self.assertFalse(worktree.violates(
            str(self.wt / "static" / "index.html"), self.live, self.wt))

    def test_a_path_outside_both_is_not_this_guards_business(self):
        """Scratch files, /tmp, another repo — the branch rule says nothing
        about them, and denying them would be a different policy."""
        self.assertFalse(worktree.violates("/tmp/scratch.txt",
                                           self.live, self.wt))

    def test_a_nested_worktree_is_not_a_violation(self):
        """The app's own worktree toggle puts worktrees INSIDE the live root
        (.claude/worktrees/<slug>), so a naive live-root-first check would
        deny the session's own legitimate edits — the guard would be worse
        than useless. The worktree test has to win."""
        nested = self.live / ".claude" / "worktrees" / "feature"
        (nested / "static").mkdir(parents=True)
        self.assertFalse(worktree.violates(
            str(nested / "static" / "app.js"), self.live, nested))
        # ...while the live tree proper is still refused from that session
        self.assertTrue(worktree.violates(
            str(self.live / "static" / "app.js"), self.live, nested))

    def test_relative_traversal_out_of_the_worktree_is_caught(self):
        """Resolution happens before comparison, so ../ cannot smuggle a
        write into the live tree."""
        sneaky = str(self.wt / ".." / "repo" / "server" / "main.py")
        self.assertTrue(worktree.violates(sneaky, self.live, self.wt))

    def test_no_worktree_assigned_means_no_guard(self):
        """Sessions outside a branch-first repo are unaffected — the guard
        must not quietly become a global write ban."""
        self.assertFalse(worktree.violates(
            str(self.live / "x.py"), self.live, ""))
        self.assertFalse(worktree.violates(
            str(self.live / "x.py"), "", self.wt))


class TargetPaths(unittest.TestCase):
    """What the gate extracts from a tool call before asking violates()."""

    def test_write_and_edit_shapes(self):
        self.assertEqual(worktree.target_paths({"file_path": "/a/b.py"}),
                         ["/a/b.py"])
        self.assertEqual(worktree.target_paths({"notebook_path": "/a/n.ipynb"}),
                         ["/a/n.ipynb"])

    def test_multiedit_lists_every_file(self):
        got = worktree.target_paths(
            {"edits": [{"file_path": "/a/1.py"}, {"file_path": "/a/2.py"}]})
        self.assertEqual(got, ["/a/1.py", "/a/2.py"])

    def test_blank_and_missing_paths_are_dropped(self):
        self.assertEqual(worktree.target_paths({"file_path": "  "}), [])
        self.assertEqual(worktree.target_paths({}), [])
        self.assertEqual(worktree.target_paths(None), [])

    def test_the_write_tool_set_is_what_the_gate_checks(self):
        for t in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            self.assertIn(t, worktree.WRITE_TOOLS)
        # Bash joined the set on 2026-07-29, when bypassPermissions became
        # the default rung: with every shell call running unasked, a guard
        # that only reads file_path arguments does not guard anything.
        # Which Bash calls are inspected is bash_targets' business, below.
        self.assertIn("Bash", worktree.WRITE_TOOLS)
        # Reading the live tree stays fine — it is how a session learns
        # what to change.
        for t in ("Read", "Grep", "Glob"):
            self.assertNotIn(t, worktree.WRITE_TOOLS)


class BashTargets(unittest.TestCase):
    """The heuristic half. It exists to catch an agent naming the live root
    in a mutating command; it must NEVER cost a session its ability to read
    the live tree, which is the one thing worktree.py's own rule protects."""

    def test_a_read_only_command_is_never_inspected(self):
        for cmd in ("grep -rn foo ~/workspace/vira/server",
                    "cat ~/workspace/vira/CLAUDE.md",
                    "git -C ~/workspace/vira log --oneline",
                    "ls -la ~/workspace/vira",
                    "diff ~/workspace/vira/a.py ./a.py",
                    "python3 -m unittest discover tests"):
            self.assertEqual(worktree.bash_targets({"command": cmd}), [],
                             f"a read must not be inspected: {cmd}")

    def test_redirects_and_mutating_utilities_are_inspected(self):
        for cmd in ("echo x > ~/workspace/vira/static/app.js",
                    "cat a >> ~/workspace/vira/notes.md",
                    "sed -i '' s/a/b/ ~/workspace/vira/server/main.py",
                    "rm -rf ~/workspace/vira/data",
                    "cp ./x.py ~/workspace/vira/server/x.py",
                    "mv ./x ~/workspace/vira/x",
                    "true && tee ~/workspace/vira/out.txt"):
            self.assertTrue(worktree.bash_targets({"command": cmd}),
                            f"a mutation must be inspected: {cmd}")

    def test_a_redirect_to_stderr_is_not_a_write(self):
        self.assertEqual(
            worktree.bash_targets({"command": "make 2>&1 | tail"}), [])

    def test_dev_null_suppression_is_not_a_write(self):
        """The false positive that denied an agent an `ls` within minutes of
        the guard going live (2026-07-29). `2>/dev/null` is how shell reads
        suppress noise; counting it as a mutation blocks ordinary work,
        which is how a guard gets switched off."""
        for cmd in ("ls ~/.venvs/ 2>/dev/null",
                    "ls ~/workspace/vira/.venv/bin/python3 2>/dev/null && echo ok",
                    "which foo >/dev/null 2>&1",
                    "ls -la .venv 2>/dev/null | head -3"):
            self.assertEqual(worktree.bash_targets({"command": cmd}), [],
                             f"a suppressed read is still a read: {cmd}")

    def test_only_the_destination_counts(self):
        """A command may read one path and write another. Denying the READ
        would stop legitimate work — copying a file out of the live tree
        into the worktree is exactly what a placed session does."""
        got = worktree.bash_targets(
            {"command": "cp ~/workspace/vira/a.py ./b.py"})
        self.assertEqual(got, ["./b.py"])
        got = worktree.bash_targets(
            {"command": "cat ~/workspace/vira/a.py > ./b.py"})
        self.assertEqual(got, ["./b.py"])

    def test_destructive_verbs_claim_every_argument(self):
        got = worktree.bash_targets({"command": "rm -rf ~/workspace/vira/data"})
        self.assertIn("~/workspace/vira/data", got)

    def test_one_mutating_clause_does_not_taint_the_whole_line(self):
        """Per-segment, so a read earlier in the line stays a read."""
        got = worktree.bash_targets(
            {"command": "grep -rn x ~/workspace/vira/server; echo y > ./out"})
        self.assertEqual(got, ["./out"])

    def test_paths_stop_at_shell_metacharacters(self):
        got = worktree.bash_targets(
            {"command": "cp ./a.py ~/workspace/vira/b.py; echo done"})
        self.assertIn("~/workspace/vira/b.py", got)
        self.assertNotIn("~/workspace/vira/b.py;", got)

    def test_non_dict_and_empty_input(self):
        self.assertEqual(worktree.bash_targets(None), [])
        self.assertEqual(worktree.bash_targets({}), [])
        self.assertEqual(worktree.bash_targets({"command": "   "}), [])


class Slugs(unittest.TestCase):
    def test_kebab_case_and_branch_sh_legal(self):
        self.assertEqual(worktree.slugify("Fix the Radar fold!"),
                         "fix-the-radar-fold")
        self.assertEqual(worktree.slugify("A/B  test__now"), "a-b-test-now")

    def test_leading_non_alnum_is_repaired(self):
        """branch.sh requires ^[a-z0-9]; a slug starting with a dash is
        rejected by it, so slugify must not produce one."""
        s = worktree.slugify("---")
        self.assertTrue(s and s[0].isalnum(), s)

    def test_empty_falls_back(self):
        self.assertEqual(worktree.slugify("", fallback="session"), "session")

    def test_length_is_bounded(self):
        self.assertLessEqual(len(worktree.slugify("x " * 200)), 32)


class RepoDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        _git("init", "-q", cwd=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_branch_first_is_declared_by_shipping_branch_sh(self):
        self.assertFalse(worktree.is_branch_first(self.root))
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "branch.sh").write_text("#!/bin/sh\n")
        self.assertTrue(worktree.is_branch_first(self.root))

    def test_branch_first_of_nothing_is_false(self):
        self.assertFalse(worktree.is_branch_first(None))
        self.assertFalse(worktree.is_branch_first("/nonexistent/xyz"))

    def test_repo_root_finds_the_toplevel_from_a_subdir(self):
        sub = self.root / "server"
        sub.mkdir()
        got = worktree.repo_root(sub)
        self.assertIsNotNone(got)
        self.assertEqual(Path(got).resolve(), self.root.resolve())

    def test_repo_root_outside_a_repo_is_none(self):
        outside = Path(self.tmp.name) / "loose"
        outside.mkdir()
        self.assertIsNone(worktree.repo_root(outside))

    def test_primary_checkout_is_not_a_worktree(self):
        """The launch path only creates a worktree when cwd is the PRIMARY
        checkout — otherwise a session already running in a worktree would
        get a worktree of its own on every dispatch."""
        self.assertFalse(worktree.is_worktree(self.root))

    def test_ensure_without_branch_sh_reports_rather_than_raises(self):
        """A failure to provision must never take the session down; it
        degrades to running in place, loudly."""
        path, created, detail = worktree.ensure(self.root, "some-slug")
        self.assertIsNone(path)
        self.assertFalse(created)
        self.assertIn("branch.sh", detail)


class DenyMessage(unittest.TestCase):
    def test_it_names_where_to_go_instead(self):
        """A denial that only says no invites a retry of the same call."""
        msg = worktree.deny_message("/tmp/repo-feature")
        self.assertIn("/tmp/repo-feature", msg)
        self.assertIn("Do not retry", msg)
        self.assertIn("do not merge", msg.lower())


@unittest.skipUnless(os.name == "posix",
                     "executes a /bin/sh stand-in for branch.sh")
class EnsureAgainstARealRepo(unittest.TestCase):
    """The creation path, against a real git repo with a stand-in
    branch.sh. Proves ensure() actually yields a usable worktree and that a
    second call REUSES it — a re-dispatch of the same work must land back in
    the branch it started, not mint slug-2, slug-3.

    POSIX only: the stand-in is a shell script run as an executable, which
    Windows cannot exec. Production degrades honestly there — ensure() catches
    the OSError and the session runs in place with a transcript note — and the
    pure-python half of the guard (PathGuard, TargetPaths, DenyMessage) still
    runs on every platform."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=self.root)
        _git("config", "user.email", "t@example.com", cwd=self.root)
        _git("config", "user.name", "T", cwd=self.root)
        (self.root / "README.md").write_text("x\n")
        _git("add", "-A", cwd=self.root)
        _git("commit", "-qm", "init", cwd=self.root)
        # stand-in for scripts/branch.sh start <slug>
        sh = self.root / "scripts" / "branch.sh"
        sh.write_text(
            "#!/bin/sh\n"
            'git worktree add -b "claude/$2" "../repo-$2" >/dev/null 2>&1\n')
        sh.chmod(0o755)

    def test_creates_then_reuses(self):
        path, created, detail = worktree.ensure(self.root, "my-feature")
        self.assertIsNotNone(path, detail)
        self.assertTrue(created)
        self.assertTrue(Path(path).is_dir())
        # it is a linked worktree, so launch would not re-branch from it
        self.assertTrue(worktree.is_worktree(path))

        again, created2, detail2 = worktree.ensure(self.root, "my-feature")
        self.assertFalse(created2)
        self.assertEqual(detail2, "reused")
        self.assertEqual(Path(again).resolve(), Path(path).resolve())

    def test_the_guard_then_protects_the_live_tree(self):
        """End to end: place, then refuse a write aimed back at live."""
        path, _, _ = worktree.ensure(self.root, "my-feature")
        self.assertTrue(worktree.violates(
            str(self.root / "server" / "main.py"), self.root, path))
        self.assertFalse(worktree.violates(
            str(Path(path) / "server" / "main.py"), self.root, path))


@unittest.skipUnless(os.name == "posix",
                     "executes a /bin/sh stand-in for branch.sh")
class TidyAgainstARealRepo(unittest.TestCase):
    """tidy() — a finished session's worktree goes away only when empty.

    Against a real git repo, because every refusal here is a git question
    (is it dirty, does the branch carry commits) and a mocked git would only
    prove the mock. Keeping is the default: the asymmetry is that a stale
    directory is untidy while a wrongly-removed one destroys the only copy
    of a session's work, and the implement prompt tells sessions not to
    commit, so uncommitted IS the normal shape of delivered work."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        (self.root / "scripts").mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=self.root)
        _git("config", "user.email", "t@example.com", cwd=self.root)
        _git("config", "user.name", "T", cwd=self.root)
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        # The production .gitignore entries tidy() depends on. Not fixture
        # dressing: without `.test-instance.*` ignored, a worktree that was
        # ever served carries an untracked pidfile and would read as holding
        # work forever — so a stopped instance could pin its worktree for
        # good. Same for the `data/` clone and the `.venv` symlink.
        (self.root / ".gitignore").write_text(
            ".test-instance.*\ndata/\n.venv\n.worktrees/\n", encoding="utf-8")
        _git("add", "-A", cwd=self.root)
        _git("commit", "-qm", "init", cwd=self.root)
        # Stand-in for branch.sh: start puts the worktree INSIDE the repo
        # (.worktrees/<slug>, where the real script puts it since
        # 2026-07-29), discard removes it and the branch.
        sh = self.root / "scripts" / "branch.sh"
        sh.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  start) mkdir -p .worktrees && git worktree add -b "claude/$2" '
            '".worktrees/$2" >/dev/null 2>&1 ;;\n'
            '  discard) rm -rf ".worktrees/$2/data"; '
            'git worktree remove --force ".worktrees/$2" >/dev/null 2>&1; '
            'git branch -D "claude/$2" >/dev/null 2>&1 ;;\n'
            'esac\n', encoding="utf-8")
        sh.chmod(0o755)

    def _place(self, slug="my-feature"):
        path, created, detail = worktree.ensure(self.root, slug)
        self.assertIsNotNone(path, detail)
        return Path(path)

    def test_an_empty_worktree_is_removed_with_its_branch(self):
        wt = self._place()
        removed, detail = worktree.tidy(self.root, wt, "claude/my-feature")
        self.assertTrue(removed, detail)
        self.assertFalse(wt.is_dir())
        branches = _git("branch", "--list", "claude/my-feature",
                        cwd=self.root).stdout
        self.assertEqual(branches.strip(), "")

    def test_it_lands_inside_the_repo_not_beside_it(self):
        """The whole point of the move: a worktree is an implementation
        detail of a branch, so it must not sit in ~/workspace looking like
        a project. Nested placement is safe only because violates() tests
        the worktree first — pinned here alongside it."""
        wt = self._place()
        self.assertTrue(worktree._inside(wt, self.root))
        self.assertFalse(worktree.violates(str(wt / "a.py"), self.root, wt))

    def test_uncommitted_work_is_kept_and_said_so(self):
        wt = self._place()
        (wt / "README.md").write_text("edited by the session\n",
                                      encoding="utf-8")
        removed, detail = worktree.tidy(self.root, wt, "claude/my-feature")
        self.assertFalse(removed)
        self.assertTrue(wt.is_dir())
        self.assertIn("uncommitted", detail)

    def test_an_untracked_file_also_counts_as_work(self):
        """`git status --porcelain` reports untracked files too, and it
        should: a session's new module is untracked until someone adds it."""
        wt = self._place()
        (wt / "new_module.py").write_text("x = 1\n", encoding="utf-8")
        removed, detail = worktree.tidy(self.root, wt, "claude/my-feature")
        self.assertFalse(removed)
        self.assertIn("uncommitted", detail)

    def test_a_branch_with_commits_is_kept(self):
        wt = self._place()
        (wt / "README.md").write_text("committed\n", encoding="utf-8")
        _git("add", "-A", cwd=wt)
        _git("commit", "-qm", "session work", cwd=wt)
        removed, detail = worktree.tidy(self.root, wt, "claude/my-feature")
        self.assertFalse(removed)
        self.assertTrue(wt.is_dir())
        self.assertIn("commit", detail)

    def test_a_live_test_instance_stops_it(self):
        """Discarding kills the instance, and a served instance is the owner
        looking at the work."""
        wt = self._place()
        (wt / ".test-instance.json").write_text(
            '{"pid": %d, "port": 8378}' % os.getpid(), encoding="utf-8")
        removed, detail = worktree.tidy(self.root, wt, "claude/my-feature")
        self.assertFalse(removed)
        self.assertIn("test instance", detail)

    def test_a_dead_instance_pid_does_not_stop_it(self):
        """A leftover pidfile from a stopped instance must not pin the
        worktree forever."""
        wt = self._place()
        (wt / ".test-instance.json").write_text(
            '{"pid": 2147483646, "port": 8378}', encoding="utf-8")
        removed, detail = worktree.tidy(self.root, wt, "claude/my-feature")
        self.assertTrue(removed, detail)

    def test_it_refuses_the_live_checkout(self):
        removed, detail = worktree.tidy(self.root, self.root, "claude/main")
        self.assertFalse(removed)
        self.assertTrue(self.root.is_dir())

    def test_it_refuses_a_branch_it_did_not_name(self):
        """Only claude/<slug> branches are session branches. Anything else
        is the owner's own, and branch.sh discard would delete it."""
        wt = self._place()
        for branch in ("main", "feature/x", "claude/../../etc"):
            removed, detail = worktree.tidy(self.root, wt, branch)
            self.assertFalse(removed, branch)
            self.assertTrue(wt.is_dir())

    def test_missing_pieces_are_a_no_op(self):
        for args in ((None, None, None), (self.root, "", "claude/x"),
                     (self.root, str(self.root / "nope"), "claude/x")):
            removed, _ = worktree.tidy(*args)
            self.assertFalse(removed)

    def test_a_plain_directory_is_never_removed(self):
        """is_worktree() is the check: a path that is not a linked worktree
        is somebody else's directory."""
        d = self.root.parent / "not-a-worktree"
        d.mkdir()
        removed, detail = worktree.tidy(self.root, d, "claude/x")
        self.assertFalse(removed)
        self.assertTrue(d.is_dir())
        self.assertIn("not a linked worktree", detail)


if __name__ == "__main__":
    unittest.main()
