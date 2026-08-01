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
        self.calls = bindir / "tailscale-calls"
        tailscale = bindir / "tailscale"
        tailscale.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then\n"
            "  printf '%s' '{\"Self\":{\"DNSName\":"
            "\"vira-mac.example.ts.net.\"}}'\n"
            "else\n"
            "  printf '%s\\n' \"$*\" >> \"$TAILSCALE_CALLS\"\n"
            "fi\n",
            encoding="utf-8",
        )
        tailscale.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env.get("PATH", "")
        self.env["TAILSCALE_CALLS"] = str(self.calls)

    def test_fixture_snapshot_contains_only_neutral_test_notes(self):
        root = Path(self.tmp.name) / "preview"
        fake_live = Path(self.tmp.name) / "live"
        fake_python = fake_live / ".venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_python.chmod(0o755)
        result = run_shell(
            f'LIVE="{fake_live}"\nfixture_data "{root}"', self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = root / "data"
        self.assertTrue((data / ".test-snapshot").is_file())
        self.assertTrue((data / "test-vault" / "wiki" /
                         "Durable previews.md").is_file())
        config = (data / "config.json").read_text(encoding="utf-8")
        self.assertIn('"fixture_mode": true', config)
        self.assertIn(str(data / "test-vault"), config)

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

    def test_tailnet_serve_proxies_loopback_and_can_be_removed(self):
        result = run_shell("tailnet_serve 8381\ntailnet_unserve 8381", self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            calls[0],
            "serve --bg --yes --http=8381 http://127.0.0.1:8381")
        self.assertEqual(calls[1], "serve --yes --http=8381 off")

    def test_uvicorn_stays_loopback_only(self):
        source = BRANCH_SH.read_text(encoding="utf-8")
        self.assertIn('--host 127.0.0.1 --port "$port"', source)
        self.assertNotIn("--host 0.0.0.0 --port", source)

    def test_macos_preview_is_launchd_keepalive(self):
        source = BRANCH_SH.read_text(encoding="utf-8")
        self.assertIn('"KeepAlive": True', source)
        self.assertIn('$HOME/Library/LaunchAgents/$label.plist', source)
        self.assertIn('"/usr/bin/caffeinate", "-i", "-s", "-t", "43200"',
                      source)
        self.assertIn('launchctl bootstrap "gui/$(id -u)" "$plist"', source)
        self.assertIn('launchctl bootout "gui/$(id -u)/$label"', source)


if __name__ == "__main__":
    unittest.main()
