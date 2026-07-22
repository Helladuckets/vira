"""branch.sh clone_data — the data snapshot taken from a live, churning tree.

`serve` clones live data/ while the live server is still writing to it. These
tests drive the shell function directly against a synthetic source, stubbing
`cp` to make the race deterministic instead of waiting for a real checkpoint.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

BRANCH_SH = Path(__file__).resolve().parents[1] / "scripts" / "branch.sh"


def run_clone(body: str) -> subprocess.CompletedProcess:
    """Source branch.sh (which leaves `set -eu` on, as in a real run) and go.

    From the repo root, because sourcing resolves the live checkout up front.
    """
    script = f'source "{BRANCH_SH}"\n{body}\n'
    return subprocess.run(
        ["/bin/zsh", "-c", script], cwd=BRANCH_SH.parents[1],
        capture_output=True, text=True,
    )


class CloneDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "live" / "data"
        self.wt = root / "worktree"
        self.dst = self.wt / "data"
        self.stage = self.wt / ".data-snapshot.tmp"
        (self.src / "jobs").mkdir(parents=True)
        self.wt.mkdir()
        # a plausible live data/: stores, a running server's log, a sqlite
        # database with its live sidecars, and a nested sidecar too
        (self.src / "config.json").write_text('{"a": 1}')
        (self.src / ".DS_Store").write_text("x")
        (self.src / "launchd.log").write_text("live server chatter")
        (self.src / "media-index.sqlite").write_text("db")
        (self.src / "media-index.sqlite-shm").write_text("shm")
        (self.src / "media-index.sqlite-wal").write_text("wal")
        (self.src / "jobs" / "j1.json").write_text("{}")
        (self.src / "jobs" / "nested.sqlite-wal").write_text("wal")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clone_skips_sqlite_sidecars(self):
        r = run_clone(f'clone_data "{self.src}" "{self.dst}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dst / "config.json").read_text(), '{"a": 1}')
        self.assertEqual((self.dst / "media-index.sqlite").read_text(), "db")
        self.assertEqual((self.dst / "jobs" / "j1.json").read_text(), "{}")
        self.assertTrue((self.dst / ".DS_Store").exists())  # dotfiles ride along
        # sidecars are rebuildable and a copied mid-transaction WAL is worse
        # than none — top level and nested alike
        self.assertFalse((self.dst / "media-index.sqlite-shm").exists())
        self.assertFalse((self.dst / "media-index.sqlite-wal").exists())
        self.assertFalse((self.dst / "jobs" / "nested.sqlite-wal").exists())
        # the live server's log does not follow into the clone
        self.assertFalse((self.dst / "launchd.log").exists())
        # marker written, staging directory consumed by the rename
        self.assertTrue((self.dst / ".test-snapshot").read_text().strip())
        self.assertFalse(self.stage.exists())

    def test_entry_that_vanishes_mid_clone_is_skipped_not_fatal(self):
        # the real failure: a file listed by the walk is gone by the time cp
        # reaches it, and `set -eu` turned that into a hard abort
        stub = '''
        cp() {
          if [[ "$2" == */config.json ]]; then rm -f "$2"; return 1; fi
          command cp "$@"
        }
        '''
        r = run_clone(f'{stub}\nclone_data "{self.src}" "{self.dst}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.dst / "config.json").exists())
        # everything else still landed, and the snapshot is usable
        self.assertEqual((self.dst / "media-index.sqlite").read_text(), "db")
        self.assertEqual((self.dst / "jobs" / "j1.json").read_text(), "{}")
        self.assertTrue((self.dst / ".test-snapshot").exists())
        self.assertIn("mid-clone", r.stdout)

    def test_real_copy_failure_is_fatal_and_leaves_nothing_behind(self):
        # source still there afterwards: that is a genuine failure, not churn
        stub = '''
        cp() {
          [[ "$2" == */config.json ]] && return 1
          command cp "$@"
        }
        '''
        r = run_clone(f'{stub}\nclone_data "{self.src}" "{self.dst}"')
        self.assertEqual(r.returncode, 1)
        self.assertIn("clone incomplete", r.stderr)
        # no half-copied data/ for the next serve to mistake for a snapshot,
        # and no staging directory left lying around either
        self.assertFalse(self.dst.exists())
        self.assertFalse(self.stage.exists())

    def test_stale_destination_is_replaced_wholesale(self):
        self.dst.mkdir(parents=True)
        (self.dst / "leftover.json").write_text("stale")
        r = run_clone(f'clone_data "{self.src}" "{self.dst}"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.dst / "leftover.json").exists())
        self.assertTrue((self.dst / ".test-snapshot").exists())


if __name__ == "__main__":
    unittest.main()
