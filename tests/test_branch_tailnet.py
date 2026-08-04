"""Tailnet reachability and handoff URLs for passive branch instances."""
import os
import plistlib
import signal
import subprocess
import tempfile
import time
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
        self.bindir = Path(self.tmp.name)
        self.calls = self.bindir / "tailscale-calls"
        tailscale = self.bindir / "tailscale"
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
        self.env["PATH"] = (
            str(self.bindir) + os.pathsep + self.env.get("PATH", ""))
        self.env["TAILSCALE_CALLS"] = str(self.calls)

    def write_executable(self, name, body):
        path = self.bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

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

    def test_macos_preview_keep_awake_window_is_bounded(self):
        self.write_executable(
            "uname", "#!/bin/sh\nprintf '%s\\n' Darwin\n")
        self.write_executable(
            "launchctl",
            "#!/bin/sh\n"
            "if [ \"$1\" = print ]; then\n"
            "  printf '\\tpid = 4242\\n'\n"
            "fi\n",
        )
        home = self.bindir / "home"
        preview = self.bindir / "preview"
        preview.mkdir()
        fake_live = self.bindir / "live"
        python = fake_live / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("unused by the launchctl stub\n", encoding="utf-8")

        env = dict(self.env)
        env["HOME"] = str(home)
        result = run_shell(
            f'LIVE="{fake_live}"\n'
            f'start_test_process bounded "{preview}" 8381',
            env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        plist = home / "Library" / "LaunchAgents" / (
            "nyc.durham.vira.test.bounded.plist")
        with plist.open("rb") as handle:
            payload = plistlib.load(handle)
        self.assertTrue(payload["KeepAlive"])
        self.assertFalse(payload["AbandonProcessGroup"])

        assertion_args = self.bindir / "caffeinate-args"
        assertion_pid = self.bindir / "caffeinate-pid"
        server_pid = self.bindir / "server-pid"
        # Model caffeinate's key lifecycle rule in milliseconds: a bare
        # assertion expires, but supplying a utility makes it live with that
        # utility instead. The old plist therefore leaves this process alive.
        fake_caffeinate = self.write_executable(
            "caffeinate",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$@\" > \"$CAFFEINATE_ARGS\"\n"
            "printf '%s\\n' \"$$\" > \"$CAFFEINATE_PID\"\n"
            "if [ \"$#\" -gt 4 ]; then\n"
            "  shift 4\n"
            "  exec \"$@\"\n"
            "fi\n"
            "sleep 0.1\n",
        )
        fake_python = self.write_executable(
            "python",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$$\" > \"$SERVER_PID\"\n"
            "while :; do sleep 1; done\n",
        )
        arguments = list(payload["ProgramArguments"])
        arguments[arguments.index("/usr/bin/caffeinate")] = str(
            fake_caffeinate)
        arguments[arguments.index(str(python))] = str(fake_python)
        process_env = dict(env)
        process_env.update({
            "CAFFEINATE_ARGS": str(assertion_args),
            "CAFFEINATE_PID": str(assertion_pid),
            "SERVER_PID": str(server_pid),
        })
        process = subprocess.Popen(
            arguments, cwd=preview, env=process_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 2
            while (not assertion_pid.exists() or not server_pid.exists()) \
                    and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(assertion_pid.exists())
            self.assertTrue(server_pid.exists())
            self.assertEqual(
                assertion_args.read_text(encoding="utf-8").splitlines(),
                ["-i", "-s", "-t", "43200"],
            )

            caffeinate_pid = int(assertion_pid.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(caffeinate_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail("bounded caffeinate outlived its timeout")
            self.assertIsNone(
                process.poll(), "preview stopped when caffeinate expired")
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
