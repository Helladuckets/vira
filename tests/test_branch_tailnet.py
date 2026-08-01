"""Tailnet reachability and handoff URLs for passive branch instances."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRANCH_SH = ROOT / "scripts" / "branch.sh"

posix_only = unittest.skipUnless(
    os.name == "posix", "branch.sh is POSIX dev tooling, not a shipped path")


def run_shell(body, env=None):
    return subprocess.run(
        ["/bin/zsh", "-c", f'source "{BRANCH_SH}"\n{body}\n'],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )


@posix_only
class TailnetBranchInstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bindir = Path(self.tmp.name)
        tailscale = bindir / "tailscale"
        tailscale.write_text(
            "#!/bin/sh\n"
            "printf '%s' '{\"Self\":{\"DNSName\":"
            "\"vira-mac.example.ts.net.\"}}'\n",
            encoding="utf-8",
        )
        tailscale.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env.get("PATH", "")

    def test_magicdns_name_is_normalized(self):
        result = run_shell("tailnet_host", self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "vira-mac.example.ts.net")

    def test_handoff_prints_desktop_and_mobile_tailnet_urls(self):
        result = run_shell("print_instance_urls 8381", self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://localhost:8381/stage.html", result.stdout)
        self.assertIn(
            "http://vira-mac.example.ts.net:8381/stage.html", result.stdout)
        self.assertIn("http://vira-mac.example.ts.net:8381/", result.stdout)

    def test_uvicorn_uses_live_vira_network_posture(self):
        source = BRANCH_SH.read_text(encoding="utf-8")
        self.assertIn("--host 0.0.0.0 --port", source)
        self.assertNotIn("--host 127.0.0.1 --port", source)


if __name__ == "__main__":
    unittest.main()
