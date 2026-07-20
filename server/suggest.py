"""Reply suggestions. Default backend is the local claude CLI (Max plan);
optional API backend via ANTHROPIC_API_KEY-style key in config.

CLI gotchas inherited from crm/scripts/synthesize_profiles.py: strip
ANTHROPIC_*/CLAUDE* env vars so the child CLI authenticates with its own
stored login instead of 401ing on session-scoped vars.
"""
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from . import data as crm
from . import imessage

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"

DEFAULTS = {
    "ai_backend": "cli",          # "cli" (Max plan) | "api"
    "cli_model": "sonnet",
    "api_model": "claude-sonnet-5",
    "api_key_env": "VIRA_ANTHROPIC_KEY",
    "timeout": 120,
}


def config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(updates):
    cfg = config()
    cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


PROMPT = """You are drafting reply suggestions for {owner}.

Channel: {channel}
Contact dossier (from {owner}'s CRM; may be partial):
{profile}

Recent conversation (chronological; "me" = {owner}):
{thread}

{extra}

Write 3 candidate replies {owner} could send next on this channel. Match
{owner}'s own voice as evidenced in the thread (their texts are the "me"
lines) — length, warmth, punctuation habits. Vary the three: one direct/minimal, one warmer,
one that moves the relationship or open loop forward. Never invent facts not
in the dossier or thread.

Return ONLY a JSON object:
{{"suggestions": [{{"text": "...", "tone": "direct|warm|forward", "why": "one short line"}}]}}
"""

HOOK_PROMPT = """You are drafting one conversation-opener iMessage for {owner}.

Contact dossier (from {owner}'s CRM; may be partial):
{profile}

Recent conversation (chronological; "me" = {owner}):
{thread}

The opener should act on this conversation hook:
{extra}

Write ONE message {owner} could send to open this thread of conversation. Match
{owner}'s own voice as evidenced in the thread (their texts are the "me" lines) —
length, warmth, punctuation habits. Natural, not salesy. Never invent facts
not in the dossier or thread.

Return ONLY a JSON object:
{{"suggestions": [{{"text": "...", "tone": "opener", "why": "one short line"}}]}}
"""


def _strip_env():
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE"))}


def _call_cli(prompt, model, timeout):
    cmd = ["claude", "--print", "--output-format", "json", "--model", model]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                         timeout=timeout, env=_strip_env())
    if res.returncode != 0:
        raise RuntimeError(f"claude exit {res.returncode}: {res.stderr.strip()[-400:]}")
    try:
        envelope = json.loads(res.stdout)
        text = envelope.get("result", "")
        if envelope.get("is_error"):
            raise RuntimeError(f"claude error: {text[:300]}")
    except json.JSONDecodeError:
        text = res.stdout
    return text


def _call_api(prompt, model, timeout, key):
    body = json.dumps({
        "model": model,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return "".join(b.get("text", "") for b in payload.get("content", []))


def _run(prompt, cfg):
    """Pick the EFFECTIVE backend, call it, and on failure record the auth
    state so the app degrades gracefully. Returns (text, backend_used).

    Backend selection is the fallback ladder (aihealth rung 3):
      - configured "api" but no key present  -> fall back to cli
      - configured "cli" but the login is dead + a key IS present -> use api
    A dead cli login with no key stands as cli: the call then fails honestly
    and note_failure flips the health state red + alerts the owner."""
    from . import aihealth
    backend = cfg["ai_backend"]
    key = os.environ.get(cfg["api_key_env"], "")
    if backend == "api" and not key:
        backend = "cli"
    if backend == "cli":
        backend = aihealth.preferred_backend("cli", key)
    try:
        if backend == "api":
            return _call_api(prompt, cfg["api_model"], cfg["timeout"], key), backend
        return _call_cli(prompt, cfg["cli_model"], cfg["timeout"]), backend
    except Exception as e:  # noqa: BLE001 — classify + record, then re-raise
        aihealth.note_failure(str(e), source="reply-draft")
        raise


def complete(prompt):
    """One-shot plain-text completion on the configured backend (used by the
    daily-brief narrative). Same CLI/API selection and fallback as suggest()."""
    return _run(prompt, config())[0]


def _extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in model output: {text[:200]!r}")
    return json.loads(m.group(0))


def suggest(person_id, channel="imessage", extra="", mode="replies"):
    cfg = config()
    detail = crm.get_person(person_id)
    if not detail:
        raise KeyError(person_id)

    prof = detail.get("profile")
    if prof:
        profile_txt = json.dumps({k: prof.get(k) for k in (
            "name", "relationship_class", "relationship_summary", "comms_style",
            "open_loops", "hooks", "personal_facts", "cadence")}, indent=1)[:6000]
    else:
        m = detail.get("master") or {}
        profile_txt = json.dumps({"name": detail["person"]["name"],
                                  "relationship": m.get("relationship"),
                                  "company": m.get("company"),
                                  "evidence": m.get("evidence")}, indent=1)

    msgs = imessage.thread_for_person(person_id, limit=30)
    thread_txt = "\n".join(
        f"[{m['when'][:16] if m['when'] else '?'}] {'me' if m['from_me'] else 'them'}: {m['text']}"
        for m in msgs) or "(no recent iMessage thread on file)"

    owner = config().get("owner_name") or "the user"
    if mode == "hook":
        prompt = HOOK_PROMPT.format(owner=owner, profile=profile_txt,
                                    thread=thread_txt[:12000], extra=extra)
    else:
        prompt = PROMPT.format(owner=owner, channel=channel, profile=profile_txt,
                               thread=thread_txt[:12000],
                               extra=f"Guidance from {owner}: {extra}" if extra else "")

    text, backend = _run(prompt, cfg)

    result = _extract_json(text)
    result["backend"] = backend
    result["thread_len"] = len(msgs)
    return result
