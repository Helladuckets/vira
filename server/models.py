"""The capability probe: what AI backends does THIS Mac actually have?

Setup's first step asks the owner to connect a model, and the honest way to
ask is to look rather than assume. Two things make assumption fail:

  1. **A binary on disk is not a binary on PATH.** The Codex CLI ships
     inside ChatGPT.app (`/Applications/ChatGPT.app/Contents/Resources/
     codex`) and is not linked anywhere `which` would find it. A PATH check
     alone reports "OpenAI not installed" to someone who is signed in and
     has the app open.
  2. **Installed is not signed in, and signed in is not capable.** A
     provider can be present but logged out, or authenticated by
     subscription rather than key, and the two auth modes do different
     things for cost.

So each provider is a row in PROVIDERS: where its binary hides, how to ask
it about auth without spending a token, and what Vira can actually do with
it. Adding xAI or a local runtime is a data edit, not new branching.

Everything here is deterministic and free — the same contract as
aihealth.probe(). No model call, no token spend, and nothing raises: a
probe that crashes the caller is worse than one that says "unknown".
"""
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import secrets, settings

# Auth states, worst to best: the provider isn't here at all; it's here but
# nobody is signed in; it's authenticated by a pasted key; it's
# authenticated by the owner's own subscription login.
ABSENT, LOGGED_OUT, KEY, SIGNED_IN = "absent", "logged_out", "key", "signed_in"

PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "sub_name": "Claude",                     # what the subscription is called
        "bin": "claude",
        # Hunted in order after PATH. App bundles included deliberately.
        "paths": ["/opt/homebrew/bin/claude", "~/.local/bin/claude",
                  "~/.claude/local/claude"],
        "status_cmd": ["auth", "status"],
        "login_args": ["auth", "login"],
        "api_env": "VIRA_ANTHROPIC_KEY",
        # What each backend accepts. CLI entries are the aliases the binary
        # resolves itself (session.MODEL_ALIASES widens the young ones);
        # API entries are the fallback for when the live models call can't
        # run. Both carry the label the UI shows, so a dropdown never has
        # to guess a marketing name from an id.
        "models": {
            "cli": [("sonnet", "Sonnet 5"), ("opus", "Opus 4.8"),
                    ("haiku", "Haiku 4.5"), ("fable", "Fable 5")],
            "api": [("claude-sonnet-5", "Sonnet 5"),
                    ("claude-opus-4-8", "Opus 4.8"),
                    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
                    ("claude-fable-5", "Fable 5")],
        },
        "models_url": "https://api.anthropic.com/v1/models?limit=100",
        # The data/config.json keys each dropdown writes (suggest.DEFAULTS).
        "config_keys": {"cli": "cli_model", "api": "api_model"},
        # Live agent sessions are Claude Agent SDK — only this provider
        # drives Circuits, Judge, Agent Loops and the Ideas cockpit.
        "can": {"draft": True, "sessions": True},
    },
    "openai": {
        "label": "OpenAI",
        "sub_name": "ChatGPT",
        "bin": "codex",
        "paths": ["/Applications/ChatGPT.app/Contents/Resources/codex",
                  "/opt/homebrew/bin/codex", "~/.local/bin/codex"],
        "status_cmd": ["login", "status"],
        "login_args": ["login"],
        "api_env": "VIRA_OPENAI_KEY",
        "models": {
            "cli": [("gpt-5.1-codex", "GPT-5.1 Codex"), ("gpt-5.1", "GPT-5.1")],
            "api": [("gpt-5.1", "GPT-5.1"), ("gpt-5.1-codex", "GPT-5.1 Codex")],
        },
        "models_url": "https://api.openai.com/v1/models",
        "config_keys": {"cli": "openai_cli_model", "api": "openai_api_model"},
        # codex exec serves every suggest.complete path (drafts, dossiers,
        # the brief narrative). It cannot host live agent sessions.
        "can": {"draft": True, "sessions": False},
    },
}

# Discovery hits the filesystem for every provider, and Setup polls. Cache
# the resolved paths for the process; a login state change does NOT need
# this invalidated because auth is probed separately every time.
_bin_cache = {}
_lock = threading.Lock()


def find_binary(pid):
    """Absolute path to the provider's CLI, or "" if it isn't on this Mac.
    PATH first (the normal install), then the known hiding places."""
    spec = PROVIDERS.get(pid)
    if not spec:
        return ""
    with _lock:
        if pid in _bin_cache and (not _bin_cache[pid]
                                  or Path(_bin_cache[pid]).exists()):
            return _bin_cache[pid]
    found = shutil.which(spec["bin"]) or ""
    if not found:
        for raw in spec["paths"]:
            p = Path(raw).expanduser()
            if p.exists() and os.access(p, os.X_OK):
                found = str(p)
                break
    with _lock:
        _bin_cache[pid] = found
    return found


def login_command(pid, binary=None):
    """The exact command a terminal needs to sign this provider in.

    Composed from the RESOLVED binary, never assumed: a CLI living inside
    an app bundle is not on PATH, so printing the bare name hands the owner
    a command that fails with "command not found" (the sandbox caught codex
    doing exactly this). And under the sandbox the server's HOME is the
    fake home — a login run in the owner's real terminal would sign in the
    wrong home, so the card must route through sandbox.sh (Anthropic's
    documented flow) or carry the HOME prefix explicitly."""
    spec = PROVIDERS.get(pid)
    if not spec:
        return ""
    if binary is None:
        binary = find_binary(pid)
    if not binary:
        return ""
    if settings.sandboxed() and pid == "anthropic":
        script = Path(__file__).resolve().parent.parent / "scripts" / "sandbox.sh"
        return f"{shlex.quote(str(script))} login"
    # find_binary consults PATH first, so a PATH-resolved binary equals
    # which()'s answer exactly; anything else came from the hiding places.
    head = spec["bin"] if shutil.which(spec["bin"]) == binary else shlex.quote(binary)
    cmd = f"{head} {' '.join(spec['login_args'])}"
    if settings.sandboxed():
        cmd = f"HOME={shlex.quote(str(Path.home()))} {cmd}"
    return cmd


def api_key(pid):
    """The provider's API key: env var first (existing installs and the
    documented VIRA_ANTHROPIC_KEY path), then the secrets ladder — the
    Keychain on a Mac, Credential Manager on Windows, the locked file
    elsewhere — where Setup puts a key pasted by someone with no shell
    profile to edit."""
    spec = PROVIDERS.get(pid) or {}
    val = os.environ.get(spec.get("api_env", ""), "")
    if val:
        return val
    if pid not in PROVIDERS:
        return ""
    try:
        return secrets.get(settings.keychain_service("vira-model-key"), pid)
    except Exception:  # noqa: BLE001 — never raise out of a lookup
        return ""


def _probe_auth(pid, binary):
    """Ask the provider's own CLI about its login. Returns (auth, detail).

    Both CLIs answer a status subcommand cheaply. Their output formats
    differ and are not contractual, so parse loosely: JSON when we get it,
    otherwise look for the obvious negative tells and treat anything else
    from a zero exit as signed in."""
    spec = PROVIDERS[pid]
    try:
        res = subprocess.run([binary] + spec["status_cmd"],
                             capture_output=True, text=True, timeout=20,
                             env=settings.strip_env())
    except subprocess.TimeoutExpired:
        return LOGGED_OUT, f"{spec['bin']} {' '.join(spec['status_cmd'])} timed out"
    except Exception as e:  # noqa: BLE001
        return LOGGED_OUT, f"probe error: {str(e)[:120]}"

    out = ((res.stdout or "") + "\n" + (res.stderr or "")).strip()
    try:
        data = json.loads(res.stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if isinstance(data, dict) and "loggedIn" in data:
        if data.get("loggedIn"):
            who = data.get("email") or data.get("authMethod") or ""
            return SIGNED_IN, f"signed in{' — ' + who if who else ''}"
        return LOGGED_OUT, "not signed in"

    low = out.lower()
    if res.returncode != 0 or any(t in low for t in (
            "not logged in", "not signed in", "no credentials",
            "please log in", "please sign in", "unauthenticated")):
        return LOGGED_OUT, (out.splitlines() or ["not signed in"])[0][:160]
    return SIGNED_IN, (out.splitlines() or ["signed in"])[0][:160]


def probe(pid):
    """One provider's full record. Never raises."""
    spec = PROVIDERS.get(pid)
    if not spec:
        return None
    binary = find_binary(pid)
    key = api_key(pid)
    if binary:
        auth, detail = _probe_auth(pid, binary)
        # A logged-out CLI with a key on file is still usable, via the API.
        if auth == LOGGED_OUT and key:
            auth, detail = KEY, "using the API key on file"
    elif key:
        auth, detail = KEY, "using the API key on file"
    else:
        where = ("PC" if settings.IS_WIN else
                 "Mac" if settings.IS_MAC else "machine")
        auth, detail = ABSENT, f"{spec['bin']} not found on this {where}"

    login_cmd = login_command(pid, binary)
    return {
        "id": pid,
        "label": spec["label"],
        "sub_name": spec["sub_name"],
        "binary": binary,
        "present": bool(binary),
        "auth": auth,
        "detail": detail,
        "has_key": bool(key),
        "models": [m for m, _ in spec["models"]["cli"]],
        "can": dict(spec["can"]),
        "login_cmd": login_cmd,
        "connected": auth in (SIGNED_IN, KEY),
        "action": _action_for(spec, binary, auth, login_cmd),
    }


def _action_for(spec, binary, auth, login_cmd):
    if auth in (SIGNED_IN, KEY):
        return ""
    if not binary:
        return (f"{spec['label']}: install the {spec['bin']} CLI to sign in "
                f"with a {spec['sub_name']} subscription, or paste an API key.")
    return (f"{spec['label']}: run `{login_cmd}` in a terminal to sign "
            f"in with your {spec['sub_name']} subscription, or paste an API key.")


def discover():
    """Every known provider, probed. The Setup window's AI step renders
    this list verbatim, so it shows what is really here — including a CLI
    hiding inside an app bundle — rather than a fixed menu."""
    return [probe(pid) for pid in PROVIDERS]


def connected():
    """Just the usable ones."""
    return [p for p in discover() if p["connected"]]


def active():
    """The provider Vira will actually call, as a record — the configured
    one when it is usable, else the first connected provider, else None.
    Mirrors suggest._run's ladder so Setup and the health banner cannot
    disagree with what a real call would do."""
    want = str(settings.raw().get("ai_provider") or "anthropic")
    rec = probe(want)
    if rec and rec["connected"]:
        return rec
    return next(iter(connected()), None)


def auth_mode(pid=None):
    """"subscription" | "key" | "" — what a run will bill against. The
    dossier step reads this to say "included in your plan" or a dollar
    estimate, so it must reflect the EFFECTIVE provider, not the config."""
    rec = probe(pid) if pid else active()
    if not rec or not rec["connected"]:
        return ""
    return "subscription" if rec["auth"] == SIGNED_IN else "key"


# ---------- the model catalog: what a picker is allowed to offer ----------
#
# A hardcoded model menu goes stale the week a model ships, and it lies in
# the other direction too — offering a provider's models to someone who
# never connected it. So every dropdown in the app (Setup's default
# models, a circuit stage's model, the idea-run sheet) is fed from here.

MODELS_TTL = 600.0        # how long a live /v1/models answer is reused
MODELS_TIMEOUT = 8
MODELS_CAP = 40
OPTIONS_TTL = 30.0        # options() shells out per provider — don't per-card

# Modalities a text pipeline can't drive; OpenAI's list mixes them in.
_NOT_CHAT = ("audio", "realtime", "transcribe", "tts", "embedding", "image",
             "moderation", "dall-e", "whisper", "sora")

_models_cache = {}        # pid -> (fetched_at, [{"id","label"}], detail)
_options_cache = {"at": 0.0, "payload": None}


def _shape_models(pid, rows):
    """A provider's raw /v1/models rows -> the picker's [{id, label}].

    Anthropic hands back a display name and newest-first order, so the
    rows are already the answer. OpenAI returns its whole catalog —
    embeddings, speech, image models — in arbitrary order, so the chat
    families are filtered out of it and sorted newest-first."""
    out = []
    if pid == "anthropic":
        for r in rows:
            mid = str(r.get("id") or "")
            if mid:
                out.append({"id": mid,
                            "label": str(r.get("display_name") or mid)})
    else:
        chat = [r for r in rows
                if str(r.get("id", "")).startswith(("gpt", "o1", "o3", "o4",
                                                    "codex"))
                and not any(t in str(r.get("id", "")) for t in _NOT_CHAT)]
        chat.sort(key=lambda r: r.get("created") or 0, reverse=True)
        out = [{"id": str(r["id"]), "label": str(r["id"])} for r in chat]
    return out[:MODELS_CAP]


def _fetch_models(pid, key):
    """Ask the provider itself. Returns (models, detail); ([], reason) on
    any failure — a stale or missing live list falls back to the curated
    one, it never breaks the picker."""
    spec = PROVIDERS[pid]
    url = spec.get("models_url")
    if not url:
        return [], "no models endpoint"
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
               if pid == "anthropic" else {"authorization": f"Bearer {key}"})
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as r:
            payload = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — never raise out of a lookup
        return [], f"live list unavailable ({str(e)[:100]})"
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return [], "unexpected models response"
    got = _shape_models(pid, rows)
    return got, (f"live from your API key — {len(got)} models" if got
                 else "the API key returned no usable models")


def _live_models(pid, refresh=False):
    key = api_key(pid)
    if not key:
        return [], "no API key on file"
    now = time.monotonic()
    with _lock:
        hit = _models_cache.get(pid)
    if hit and not refresh and now - hit[0] < MODELS_TTL:
        return hit[1], hit[2]
    got, detail = _fetch_models(pid, key)
    with _lock:
        _models_cache[pid] = (now, got, detail)
    return got, detail


def catalog(pid, refresh=False):
    """What this provider can be pointed at, per backend.

    The CLI list is the alias set its binary accepts — neither CLI has a
    "list models" subcommand to ask, and an alias is the spelling that
    keeps working across releases. The API list IS asked live, against the
    key on file, because that is the one place a true answer exists."""
    spec = PROVIDERS.get(pid)
    if not spec:
        return {"cli": [], "api": [], "api_live": False, "api_detail": ""}
    live, detail = _live_models(pid, refresh)
    curated = [{"id": i, "label": lb} for i, lb in spec["models"]["api"]]
    return {"cli": [{"id": i, "label": lb} for i, lb in spec["models"]["cli"]],
            "api": live or curated,
            "api_live": bool(live),
            "api_detail": detail}


def options(refresh=False):
    """Everything a model picker needs, in one payload: each provider,
    whether it is usable here, what each of its backends accepts, and the
    config key a choice writes. Setup's default-model dropdowns and the
    Circuits stage tray both read this, so no picker can drift from what
    this machine actually has."""
    now = time.monotonic()
    with _lock:
        cached = _options_cache["payload"]
        fresh = now - _options_cache["at"] < OPTIONS_TTL
    if cached and fresh and not refresh:
        return cached
    provs = []
    for pid, spec in PROVIDERS.items():
        rec = probe(pid) or {}
        provs.append({
            "id": pid, "label": spec["label"],
            "connected": bool(rec.get("connected")),
            "auth": rec.get("auth", ABSENT),
            "has_key": bool(rec.get("has_key")),
            "sessions": bool(spec["can"]["sessions"]),
            "config_keys": dict(spec["config_keys"]),
            **catalog(pid, refresh),
        })
    # active()'s ladder, re-derived from the records already probed above
    # rather than probing every provider a second time.
    want = str(settings.raw().get("ai_provider") or "anthropic")
    usable = [p["id"] for p in provs if p["connected"]]
    payload = {"providers": provs,
               "active": want if want in usable else next(iter(usable), "")}
    with _lock:
        _options_cache.update(at=now, payload=payload)
    return payload
