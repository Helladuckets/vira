"""scripts/sandbox.sh — the contract between its help text and its dispatch.

Written after `replay` shipped documented, implemented, and UNREACHABLE
(2026-07-30): the patch adding its case arm matched text the file no longer
carried, so it was a silent no-op and `sandbox.sh replay` printed usage. The
suite was green the whole time, because nothing had ever read this file.

The rule these pin: a subcommand named in the header must be dispatched, and
a dispatched subcommand must have a function. Either half alone looks correct
in review — the help text reads right, and the function exists — which is what
made the gap invisible.
"""
import os
import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sandbox.sh"


@unittest.skipUnless(os.name == "posix", "sandbox.sh is Mac-side dev tooling")
class SandboxScriptContract(unittest.TestCase):
    def setUp(self):
        self.src = SCRIPT.read_text(encoding="utf-8")

    def _documented(self):
        # The header block: lines like "#   sandbox.sh <cmd> ..." — the
        # continuation lines that describe a flag carry no command name.
        return set(re.findall(r"^#\s+sandbox\.sh (\w+)", self.src, re.M))

    def _dispatched(self):
        case = self.src[self.src.index('case "$cmd" in'):]
        case = case[:case.index("esac")]
        return set(re.findall(r"^\s{2}(\w+)\)", case, re.M))

    def test_every_documented_command_is_dispatched(self):
        missing = self._documented() - self._dispatched()
        self.assertEqual(missing, set(),
                         f"documented but unreachable: {sorted(missing)}")

    def test_every_dispatched_command_has_a_function(self):
        for cmd in self._dispatched():
            self.assertIn(f"cmd_{cmd}()", self.src,
                          f"{cmd}) dispatches to a function that isn't defined")

    def test_usage_range_covers_the_whole_command_block(self):
        # usage() prints a fixed line range. Commands were appended twice
        # without it following, so `sandbox.sh` stopped listing its newest.
        end = int(re.search(r"sed -n '2,(\d+)p' \"\$SELF\"", self.src).group(1))
        lines = self.src.splitlines()
        last = max(i for i, ln in enumerate(lines, 1)
                   if re.match(r"^#\s+sandbox\.sh \w+", ln))
        self.assertGreaterEqual(end, last,
                                "usage() cuts off before the last command")

    def test_replay_resets_the_welcome_flag_and_restamps(self):
        # The two things that make a replay a FIRST BOOT rather than a
        # restart: data/ (which holds the server-side once-per-install flag)
        # and a fresh instance stamp (so a browser adopts instead of pushing
        # its stale copy back up).
        body = self.src[self.src.index("cmd_replay()"):]
        body = body[:body.index("\ncmd_serve()")]
        self.assertIn('rm -rf "$APP/data"', body)
        self.assertIn(".instance-stamp", body)
        self.assertNotIn('rm -rf "$APP/.venv"', body)   # keeps the venv


if __name__ == "__main__":
    unittest.main()
