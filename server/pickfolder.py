"""A real folder picker — the OS's own open panel, not a path to type.

Vira runs on the owner's machine, so the honest way to ask "which folder?"
is to open the panel they already know: navigate, select, Open. Every
alternative a browser offers is worse:

  * `window.showDirectoryPicker()` opens a real panel and then deliberately
    withholds the filesystem path — Vira needs the path, so a handle is
    useless here.
  * `<input webkitdirectory>` gives a RELATIVE path (the folder name), and
    hands the page every file inside it.
  * "Paste the path" is the thing being replaced.

So the panel is opened SERVER-SIDE, the same trick `onboard.fda_assist`
uses: Vira is local, so a window it opens is a window on the owner's own
desk. This is the one place that is true — see WHEN NOT TO below.

WHEN NOT TO OPEN IT
-------------------
A panel on the owner's desktop is the correct answer only when the person
driving the browser is sitting at the machine running the server. Three
cases where that fails, all refused rather than half-served:

  * **demo mode** — a sandbox walkthrough must never put a real window on
    the real desktop (learned 2026-07-30, the whole reason `--demo` exists).
  * **VIRA_PASSIVE** — a test clone acting on the world.
  * **a remote browser** — the phone over Tailscale. The caller passes
    `local=False` and gets `unavailable` with a reason, so the UI can fall
    back to the text field instead of popping a panel on a Mac nobody is
    looking at.

Callers must handle `unavailable`: this is an ENHANCEMENT over the text
field, never a replacement for it.
"""
import os
import subprocess

from . import settings

# A forgotten panel must not hold a request open forever. Generous, because
# the owner really is browsing their disk — but bounded.
TIMEOUT_S = 300

# macOS. `choose folder` belongs to whatever app runs it, and osascript is
# background-only — the panel would open behind everything or not at all.
# Handing it to System Events with an explicit `activate` is what fronts it.
_MAC = '''
tell application "System Events"
  activate
  try
    set f to choose folder with prompt {prompt}
    return POSIX path of f
  on error number -128
    return "__CANCELLED__"
  end try
end tell
'''

# Windows. WinForms' FolderBrowserDialog is stock on any machine that has
# .NET, which is every supported Windows. TopMost via a dummy owner form,
# same fronting problem as the Mac.
_WIN = '''
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = {prompt}
$d.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {{
  Write-Output $d.SelectedPath
}} else {{
  Write-Output "__CANCELLED__"
}}
'''


def _mac_literal(s):
    """AppleScript string literal. Only backslash and quote need escaping,
    and the prompt is composed by Vira — but it reaches osascript as a
    script body, so quoting it properly is not optional."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def available(local=True):
    """Can a native panel be opened for THIS caller, right now?

    Returns (ok, reason). The reason is written for the owner, because the
    UI shows it beside the fallback field.
    """
    if not local:
        return False, ("this browser is not on the machine running Vira, so "
                       "a folder window would open on the wrong screen")
    if settings.demo():
        return False, "demo mode — the real flow opens a folder window here"
    if os.environ.get("VIRA_PASSIVE"):
        return False, "test instance — not opening windows on your desktop"
    if settings.IS_MAC or settings.IS_WIN:
        return True, ""
    return False, "no folder window on this platform — type the path instead"


def pick(prompt="Choose a folder", local=True):
    """Open the panel and block until the owner picks or cancels.

    Returns {"path": "/abs/path"} | {"cancelled": True} |
            {"unavailable": True, "reason": "..."}.
    Never raises: a picker that 500s is worse than one that says it cannot
    run, because the text field is right there either way.
    """
    ok, reason = available(local)
    if not ok:
        return {"unavailable": True, "reason": reason}
    prompt = (prompt or "Choose a folder")[:200]
    try:
        if settings.IS_MAC:
            script = _MAC.format(prompt=_mac_literal(prompt))
            cmd = ["osascript", "-e", script]
        else:
            script = _WIN.format(prompt=f"'{prompt}'")
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
                   "-Command", script]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"unavailable": True,
                "reason": "the folder window was left open — type the path, "
                          "or try again"}
    except Exception as e:                                    # noqa: BLE001
        return {"unavailable": True, "reason": f"could not open a folder "
                                               f"window: {e}"}
    out = (p.stdout or "").strip()
    if not out or out == "__CANCELLED__":
        # A cancel is an ANSWER, not a failure — the UI must not show an
        # error for it, and must not clear whatever was already typed.
        return {"cancelled": True}
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()
        return {"unavailable": True,
                "reason": tail[-1][:200] if tail else "folder window failed"}
    # macOS returns a trailing slash on directories; every consumer here
    # stores a bare path, so normalize once rather than at each call site.
    if len(out) > 1:
        out = out.rstrip("/") or "/"
    return {"path": out}
