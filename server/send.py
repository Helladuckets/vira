"""Send iMessages from the app via Messages.app (AppleScript). Deterministic —
no AI involved; the text is whatever the user approved in the UI.
"""
import os
import subprocess

from . import data as crm
from . import imessage, settings

SCRIPT = '''
on run {targetHandle, msgText}
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant targetHandle of targetService
        send msgText to targetBuddy
    end tell
end run
'''


def best_handle(pid):
    """Pick the handle the owner actually texts this person on: the one from the
    most recent direct-thread message, falling back to the first known."""
    msgs = imessage.thread_for_person(pid, limit=5)
    for m in reversed(msgs):
        if m.get("handle"):
            return m["handle"]
    p = crm._load()["by_id"].get(pid)
    if not p:
        return None
    handles = p.get("handles", {})
    ims = handles.get("imessage", [])
    if ims:
        return ims[0]
    phones = handles.get("phones10", [])
    return "+1" + phones[0] if phones else None


def send_imessage(text, person_id=None, handle=None, timeout=20):
    """Returns the handle used. Raises RuntimeError with the osascript error
    if Messages refuses (e.g. automation permission not yet granted)."""
    if os.environ.get("VIRA_PASSIVE"):
        raise RuntimeError(
            "passive test instance: outbound iMessage is blocked")
    if not settings.IS_MAC:
        raise RuntimeError(
            "iMessage sending needs macOS (Messages.app) — not available "
            "on this platform")
    target = handle or (best_handle(person_id) if person_id else None)
    if not target:
        raise ValueError("no iMessage handle for this person")
    if not text or not text.strip():
        raise ValueError("empty message")
    res = subprocess.run(
        ["osascript", "-", target, text],
        input=SCRIPT, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-400:] or "osascript failed")
    return target
