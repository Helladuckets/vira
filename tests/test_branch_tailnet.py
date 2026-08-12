"""Tailnet reachability and handoff URLs for passive branch instances."""
import json
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
        # Real `tailscale serve status --json` output for a node with no
        # handlers (verified against 1.98.8); serve_config() swaps in a
        # populated one.
        self.serve_config = self.bindir / "tailscale-serve-config.json"
        self.serve_config.write_text("{}\n", encoding="utf-8")
        tailscale = self.bindir / "tailscale"
        tailscale.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = status ]; then\n"
            "  printf '%s' '{\"Self\":{\"DNSName\":"
            "\"vira-mac.example.ts.net.\"}}'\n"
            "elif [ \"$1\" = serve ] && [ \"$2\" = status ]; then\n"
            "  cat \"$TAILSCALE_SERVE_CONFIG\"\n"
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
        self.env["TAILSCALE_SERVE_CONFIG"] = str(self.serve_config)

    def write_executable(self, name, body):
        path = self.bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def write_launchctl_stub(self, unload_ticks=0, bootstrap_failures=0):
        """A launchctl that models the two ways a label stays occupied.

        `unload_ticks` is how many `print` calls still find the label after a
        `bootout` — launchd's teardown is asynchronous. `bootstrap_failures`
        is how many loads are refused with EIO even once `print` reports the
        label gone, which is the harder half: the observed 2026-08-12 failure
        had launchd already denying knowledge of the label.
        """
        state = self.bindir / "launchd-state"
        failures = self.bindir / "launchd-failures"
        failures.write_text(f"{bootstrap_failures}\n", encoding="utf-8")
        self.env["LAUNCHD_STATE"] = str(state)
        self.env["LAUNCHD_FAILURES"] = str(failures)
        self.env["LAUNCHD_UNLOAD_TICKS"] = str(unload_ticks)
        return self.write_executable("launchctl", r"""#!/bin/sh
state=$(cat "$LAUNCHD_STATE" 2>/dev/null || printf gone)
case "$1" in
  bootout)
    printf 'unloading:%s\n' "$LAUNCHD_UNLOAD_TICKS" > "$LAUNCHD_STATE"
    ;;
  print)
    case "$state" in
      loaded)       printf '\tpid = 4242\n' ;;
      unloading:0)  printf 'gone\n' > "$LAUNCHD_STATE"; exit 1 ;;
      unloading:*)  ticks=${state#unloading:}
                    printf 'unloading:%s\n' "$((ticks - 1))" \
                      > "$LAUNCHD_STATE" ;;
      *)            exit 1 ;;
    esac
    ;;
  bootstrap)
    left=$(cat "$LAUNCHD_FAILURES" 2>/dev/null || printf 0)
    if [ "$left" -gt 0 ]; then
      printf '%s\n' "$((left - 1))" > "$LAUNCHD_FAILURES"
      echo "Bootstrap failed: 5: Input/output error" >&2
      exit 5
    fi
    case "$state" in
      unloading:*)
        echo "Bootstrap failed: 5: Input/output error" >&2
        exit 5 ;;
    esac
    printf 'loaded\n' > "$LAUNCHD_STATE"
    ;;
esac
exit 0
""")

    def preview_worktree(self):
        """uname + a fake live venv, enough for start_test_process."""
        self.write_executable("uname", "#!/bin/sh\nprintf '%s\\n' Darwin\n")
        home = self.bindir / "home"
        preview = self.bindir / "preview"
        preview.mkdir(exist_ok=True)
        live = self.bindir / "live"
        python = live / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("unused by the launchctl stub\n", encoding="utf-8")
        self.env["HOME"] = str(home)
        return live, preview

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

    def running_instance(self, port):
        """A live checkout with one worktree whose instance is RUNNING.

        The pid is this test process, so instance_pid's `kill -0` probe is
        answered by a real process rather than a stub.
        """
        # Resolved, because `git worktree list` reports real paths and
        # cmd_list skips the live line by comparing against $LIVE.
        root = Path(self.tmp.name).resolve()
        live = root / "live-repo"
        worktree = root / "preview-worktree"
        git = ["git", "-C", str(live)]
        subprocess.run(["git", "init", "-b", "main", str(live)],
                       check=True, capture_output=True)
        subprocess.run(
            git + ["-c", "user.email=t@example.com", "-c", "user.name=t",
                   "commit", "--allow-empty", "-m", "init"],
            check=True, capture_output=True)
        subprocess.run(
            git + ["worktree", "add", "-b", "claude/preview", str(worktree)],
            check=True, capture_output=True)
        (worktree / ".test-instance.json").write_text(
            f'{{"pid": {os.getpid()}, "port": {port}}}', encoding="utf-8")
        return live

    def serve_handler_config(self, port):
        """Real `serve status --json` for one --http handler (1.98.8)."""
        host = f"vira-mac.example.ts.net:{port}"
        return json.dumps({
            "TCP": {str(port): {"HTTP": True}},
            "Web": {host: {"Handlers": {
                "/": {"Proxy": f"http://127.0.0.1:{port}"}}}},
        })

    def test_listing_prints_localhost_for_an_unbridged_instance(self):
        # `serve --local` creates no Serve handler, so its MagicDNS URL would
        # be dead. Both shapes of "not bridged" count: no handlers at all, and
        # handlers that belong to some OTHER instance.
        live = self.running_instance(8391)
        for label, config in (
                ("no serve config", "{}"),
                ("another port bridged", self.serve_handler_config(8392))):
            with self.subTest(label):
                self.serve_config.write_text(config, encoding="utf-8")
                result = run_shell(f'LIVE="{live}"\ncmd_list', self.env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("claude/preview", result.stdout)
                self.assertIn("RUNNING :8391", result.stdout)
                self.assertIn("http://localhost:8391/", result.stdout)
                self.assertNotIn("vira-mac.example.ts.net:8391",
                                 result.stdout)
                # CLAUDE.md: Claude Code's Browser pane blocks the numeric
                # loopback form, so a printed URL never uses it.
                self.assertNotIn("127.0.0.1", result.stdout)

    def test_listing_prints_the_tailnet_url_for_a_bridged_instance(self):
        live = self.running_instance(8391)
        self.serve_config.write_text(
            self.serve_handler_config(8391), encoding="utf-8")
        result = run_shell(f'LIVE="{live}"\ncmd_list', self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RUNNING :8391", result.stdout)
        self.assertIn("http://vira-mac.example.ts.net:8391/", result.stdout)

    def test_served_ports_reads_every_shape_of_handler(self):
        self.serve_config.write_text(json.dumps({
            "TCP": {"8391": {"HTTPS": True}},
            "AllowFunnel": {"vira-mac.example.ts.net:8392": True},
            # A foreground `tailscale serve` (no --bg) nests its config here.
            "Foreground": {"sess-1": {
                "Web": {"vira-mac.example.ts.net:8393": {"Handlers": {}}}}},
        }), encoding="utf-8")
        result = run_shell("tailnet_served_ports", self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["8391", "8392", "8393"])

    def start_preview(self, unload_ticks=0, bootstrap_failures=0):
        self.write_launchctl_stub(unload_ticks, bootstrap_failures)
        live, preview = self.preview_worktree()
        return preview, run_shell(
            f'LIVE="{live}"\nstart_test_process racy "{preview}" 8381',
            self.env)

    def test_preview_starts_while_the_old_job_is_still_unloading(self):
        # `stop` returns before launchd has finished tearing the label down.
        # Bootstrapping into that window is the EIO the old code died on.
        preview, result = self.start_preview(unload_ticks=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4242")
        self.assertTrue((preview / ".test-instance.json").exists())
        # Waiting was enough here: nothing had to be retried.
        self.assertNotIn("Bootstrap failed", result.stderr)
        self.assertNotIn("retrying", result.stderr)
        # And the window is real — bootstrapping without the wait still EIOs,
        # so the assertions above are not passing vacuously.
        immediate = run_shell(
            "launchctl bootout gui/$(id -u)/racy\n"
            "launchctl bootstrap gui/$(id -u) /dev/null", self.env)
        self.assertEqual(immediate.returncode, 5)
        self.assertIn("Input/output error", immediate.stderr)

    def test_preview_retries_a_bootstrap_launchd_refuses(self):
        # The harder half: launchd reports the label gone and still declines
        # to load it. Waiting alone would not survive this.
        preview, result = self.start_preview(bootstrap_failures=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4242")
        self.assertIn("retrying", result.stderr)
        self.assertTrue((preview / ".test-instance.json").exists())

    def test_preview_gives_up_loudly_when_launchd_never_accepts(self):
        preview, result = self.start_preview(bootstrap_failures=99)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("launchd refused to bootstrap", result.stderr)
        self.assertIn("Input/output error", result.stderr)
        # Never a pidfile for an instance that was never loaded, and no plist
        # stranded in ~/Library/LaunchAgents either — stop_test_process would
        # never reach it, since it returns early without a pidfile.
        self.assertFalse((preview / ".test-instance.json").exists())
        self.assertFalse(
            (Path(self.env["HOME"]) / "Library" / "LaunchAgents" /
             "nyc.durham.vira.test.racy.plist").exists())

    def test_uvicorn_stays_loopback_only(self):
        source = BRANCH_SH.read_text(encoding="utf-8")
        self.assertIn('--host 127.0.0.1 --port "$port"', source)
        self.assertNotIn("--host 0.0.0.0 --port", source)

    def test_macos_preview_keep_awake_window_is_bounded(self):
        self.write_launchctl_stub()
        fake_live, preview = self.preview_worktree()
        home = Path(self.env["HOME"])
        python = fake_live / ".venv" / "bin" / "python"

        env = dict(self.env)
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
