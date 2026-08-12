"""branch.sh serve — every flag is read, whatever order it arrives in.

`cmd_serve` used to read `${2:-}` and nothing else, so the SECOND argument was
the only one that could mean anything. `serve <slug> --fresh --local` therefore
dropped --local silently and bridged a personal-data snapshot to the tailnet
(2026-08-12). These drive the real shell function with the heavy parts stubbed
and assert the EFFECT — whether the bridge was opened, which snapshot ran —
rather than grepping the source for a spelling.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

BRANCH_SH = Path(__file__).resolve().parents[1] / "scripts" / "branch.sh"

# branch.sh is the owner's Mac-side worktree tooling, sourced through /bin/zsh
# and leaning on launchd; a Windows install never runs it. Skipping states that
# fact rather than papering over a portability gap in the app.
posix_only = unittest.skipUnless(
    os.name == "posix", "branch.sh is POSIX dev tooling, not a shipped path")


@posix_only
class ServeFlagParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name) / "worktree"
        (self.wt / "data").mkdir(parents=True)
        # a snapshot already exists, so only an explicit --fresh re-clones
        (self.wt / "data" / ".test-snapshot").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, *args):
        """Run cmd_serve with everything that touches the machine stubbed.

        The stubs are defined AFTER sourcing, so they replace branch.sh's own
        definitions; the parser and the local_only/data_mode branches are the
        only real code left running.
        """
        stubs = f'''
source "{BRANCH_SH}"
wt_dir() {{ echo "{self.wt}"; }}
provision() {{ :; }}
instance_pid() {{ echo ""; }}
clone_data() {{ echo "STUB clone_data"; }}
fixture_data() {{ echo "STUB fixture_data"; }}
start_test_process() {{ echo 99999; }}
stop_test_process() {{ :; }}
tailnet_serve() {{ echo "STUB tailnet_serve $1"; }}
print_instance_urls() {{ echo "STUB print_instance_urls"; }}
curl() {{ return 0; }}
lsof() {{ return 1; }}
cmd_serve {" ".join(args)}
'''
        return subprocess.run(["/bin/zsh", "-c", stubs],
                              cwd=BRANCH_SH.parents[1],
                              capture_output=True, text=True)

    def test_local_is_honored_when_it_is_not_the_second_argument(self):
        """The incident: --local after --fresh was silently dropped."""
        out = self.serve("demo", "--fresh", "--local")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("tailnet_serve", out.stdout)
        self.assertIn("LOCAL ONLY", out.stdout)
        self.assertIn("STUB clone_data", out.stdout)   # --fresh still read

    def test_local_first_also_honors_fresh(self):
        out = self.serve("demo", "--local", "--fresh")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("tailnet_serve", out.stdout)
        self.assertIn("STUB clone_data", out.stdout)

    def test_local_alone_reuses_the_existing_snapshot(self):
        out = self.serve("demo", "--local")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("tailnet_serve", out.stdout)
        self.assertNotIn("STUB clone_data", out.stdout)
        self.assertNotIn("STUB fixture_data", out.stdout)

    def test_without_local_the_bridge_is_opened(self):
        out = self.serve("demo", "--fixture")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("STUB tailnet_serve", out.stdout)
        self.assertIn("STUB fixture_data", out.stdout)

    def test_fixture_and_local_compose(self):
        out = self.serve("demo", "--fixture", "--local")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("tailnet_serve", out.stdout)
        self.assertIn("STUB fixture_data", out.stdout)

    def test_an_unknown_flag_is_refused_rather_than_ignored(self):
        """A misspelled --local must never read as 'bridge it'."""
        out = self.serve("demo", "--loca1")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("unknown flag", out.stderr)
        self.assertNotIn("tailnet_serve", out.stdout)

    def test_two_snapshot_modes_are_refused(self):
        out = self.serve("demo", "--fresh", "--fixture")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("different snapshots", out.stderr)

    def test_the_slug_survives_the_shift(self):
        """Every later use of the slug reads $slug, not a consumed $1."""
        out = self.serve("demo", "--local")
        self.assertIn("scripts/branch.sh stop demo", out.stdout)


if __name__ == "__main__":
    unittest.main()
