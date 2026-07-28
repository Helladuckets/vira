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

MODEL SOURCES — the one rule (2026-07-28)
-----------------------------------------
**Vira offers a model name only if it can verify that name right now.**
Exactly three sources can be verified, and every picker in the app is fed
from them:

  1. **An ALIAS the provider's own CLI resolves** (`opus`, `sonnet`,
     `haiku`, `fable`). An alias names a TIER, never a generation, so it
     is right the week Opus 5 ships and no one has to edit anything.
  2. **The LIVE `/v1/models` list**, when a key is on file. The provider
     is the authority on its own catalog.
  3. **A value read out of the provider's OWN CONFIG on this machine**
     (`cli_config` below — codex keeps its model in ~/.codex/config.toml).
     Reading it is a probe like find_binary, not a pin.

Anything else is a guess with a shelf life, and a guess renders exactly
like a fact. That is how "Opus 4.8" and "GPT-5.6 Sol" stayed on screen
long after both were stale: they lived in curated fallback lists that
only an admin edit and a push could ever refresh. Those lists are gone.
Where nothing is verifiable the picker says so and offers the custom-id
hatch — an honest empty beats a confident wrong name.

So: do NOT reintroduce a hardcoded model id here, in suggest.DEFAULTS, in
config.example.json, or as an <option> in index.html. If a picker looks
empty, the fix is a key or an alias, never a literal.
"""
import json
import os
import re
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
        # Where a browser gets the owner unstuck: the exact key-console page,
        # and the install page for the subscription CLI. The first-run flow
        # opens these in the default browser so "get a key" is one click.
        "key_url": "https://console.anthropic.com/settings/keys",
        "install_url": "https://claude.com/claude-code",
        "install_cmd": "npm install -g @anthropic-ai/claude-code",
        # What each backend accepts. The CLI entries are ALIASES the binary
        # resolves itself — generation-free by construction, so they cannot
        # rot. The API list is deliberately EMPTY: see MODEL SOURCES below.
        "models": {
            "cli": [("sonnet", "Sonnet (latest)"), ("opus", "Opus (latest)"),
                    ("haiku", "Haiku (latest)"), ("fable", "Fable (latest)")],
            "api": [],
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
        "key_url": "https://platform.openai.com/api-keys",
        "install_url": "https://openai.com/codex",
        "install_cmd": "npm install -g @openai/codex",
        # codex has no model ALIASES — it takes real ids, which rot loudly
        # (a ChatGPT-account codex hard-rejects a stale generation name:
        # "gpt-5.1-codex is not supported"). So its CLI list is read from
        # codex's own config instead of pinned here: whatever codex is set
        # to run is what Vira offers, and it updates when codex does.
        "models": {"cli": [], "api": []},
        "cli_config": {"path": "~/.codex/config.toml", "key": "model",
                       "label": "codex's own configured model"},
        "models_url": "https://api.openai.com/v1/models",
        "config_keys": {"cli": "openai_cli_model", "api": "openai_api_model"},
        # codex exec serves drafts AND, since 2026-07-28, live agent
        # sessions — best-effort grade: the runner drives `codex exec`
        # inside its own harness, containment is codex's sandbox rather
        # than Vira's per-tool gate (server/agentbackend.py says the rest).
        "can": {"draft": True, "sessions": True},
    },
    # The API-only rows. Both have CLIs that may exist on a machine (gemini,
    # grok) — presence is probed and shown — but Vira talks to them through
    # their APIs: neither exposes the cheap auth-status probe the two rows
    # above rely on, so status_cmd is None and auth derives from the key.
    "google": {
        "label": "Google",
        "sub_name": "Gemini",
        "bin": "gemini",
        "paths": ["/opt/homebrew/bin/gemini", "~/.local/bin/gemini"],
        "status_cmd": None,
        "login_args": [],
        "api_env": "VIRA_GOOGLE_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        "install_url": "",
        # No CLI draft path, and no curated API list: a key is required to
        # use this provider at all, and a key means the live list works.
        "models": {"cli": [], "api": []},
        "models_url": ("https://generativelanguage.googleapis.com"
                       "/v1beta/models?pageSize=200"),
        "config_keys": {"api": "google_api_model"},
        "can": {"draft": True, "sessions": False},
    },
    "xai": {
        "label": "xAI",
        "sub_name": "Grok",
        "bin": "grok",
        "paths": ["~/.grok/bin/grok", "/opt/homebrew/bin/grok"],
        "status_cmd": None,
        "login_args": [],
        "api_env": "VIRA_XAI_KEY",
        "key_url": "https://console.x.ai",
        "install_url": "",
        "models": {"cli": [], "api": []},
        "models_url": "https://api.x.ai/v1/models",
        "config_keys": {"api": "xai_api_model"},
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


# A top-level `key = "value"` in a TOML file — i.e. before the first
# [section] header. Deliberately not a TOML parser: the one key we want is
# always top-level, tomllib is 3.11+ while this ships on 3.10, and a
# dependency for one line would have to be declared and installed
# everywhere (preflight's `deps` check exists because of exactly that).
_TOML_TOP = re.compile(r'^\s*([A-Za-z_][\w-]*)\s*=\s*["\']([^"\']*)["\']')


def cli_default_model(pid):
    """The model this provider's CLI is itself configured to run, or "".

    Source 3 of the three verifiable ones (see MODEL SOURCES): rather than
    pinning codex's model ids in PROVIDERS — where they rot silently and
    only a push can fix them — read the id out of codex's own config. It
    is the same class of probe as find_binary: look, don't assume."""
    spec = (PROVIDERS.get(pid) or {}).get("cli_config")
    if not spec:
        return ""
    try:
        text = Path(spec["path"]).expanduser().read_text(encoding="utf-8",
                                                         errors="replace")
    except OSError:
        return ""                       # no config yet, or unreadable
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            break                       # past the top-level table
        m = _TOML_TOP.match(line)
        if m and m.group(1) == spec["key"] and m.group(2).strip():
            return m.group(2).strip()
    return ""


def cli_models(pid):
    """The CLI list for a provider: its aliases, or — for a CLI that has
    no aliases — whatever that CLI is configured to run today."""
    spec = PROVIDERS.get(pid)
    if not spec:
        return []
    listed = [{"id": i, "label": lb} for i, lb in spec["models"]["cli"]]
    if listed:
        return listed
    found = cli_default_model(pid)
    if found and spec.get("cli_config"):
        return [{"id": found, "label": spec["cli_config"]["label"]}]
    return []


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
    if not spec or not spec.get("login_args"):
        return ""                     # API-only providers have no login flow
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
    if not spec.get("status_cmd"):
        # No cheap way to ask this CLI about its login; the API key is the
        # auth that matters, and the caller folds it in right after this.
        return LOGGED_OUT, (f"{spec['bin']} CLI found — Vira talks to "
                            f"{spec['sub_name']} through an API key")
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
        "models": [m["id"] for m in cli_models(pid)],
        "can": dict(spec["can"]),
        "sessions_quality": _sessions_quality(pid, spec["can"]["sessions"]),
        "login_cmd": login_cmd,
        "key_url": spec.get("key_url", ""),
        "install_url": spec.get("install_url", ""),
        "install_cmd": spec.get("install_cmd", ""),
        "connected": auth in (SIGNED_IN, KEY),
        "action": _action_for(spec, binary, auth, login_cmd),
    }


def _sessions_quality(pid, can_sessions):
    """"" | "best_effort" | "gated" — how good a live session is on this
    provider. Lazy import: agentbackend imports this module at its top."""
    if not can_sessions:
        return ""
    from . import agentbackend
    return agentbackend.sessions_quality(pid)


def _action_for(spec, binary, auth, login_cmd):
    if auth in (SIGNED_IN, KEY):
        return ""
    if not spec.get("login_args"):     # API-only: no login flow exists
        return (f"{spec['label']}: paste a {spec['sub_name']} API key "
                f"to connect.")
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
    elif pid == "google":
        # Gemini's list nests ids as "models/<id>" and mixes in embedders;
        # only rows that can generateContent belong in a chat picker.
        for r in rows:
            methods = r.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            mid = str(r.get("name") or "").split("/")[-1]
            if not mid or any(t in mid for t in _NOT_CHAT):
                continue
            out.append({"id": mid,
                        "label": str(r.get("displayName") or mid)})
    elif pid == "openai":
        chat = [r for r in rows
                if str(r.get("id", "")).startswith(("gpt", "o1", "o3", "o4",
                                                    "codex"))
                and not any(t in str(r.get("id", "")) for t in _NOT_CHAT)]
        chat.sort(key=lambda r: r.get("created") or 0, reverse=True)
        out = [{"id": str(r["id"]), "label": str(r["id"])} for r in chat]
    else:
        # xAI and future OpenAI-compatible providers: small catalogs, so
        # allow-by-default minus the non-chat modalities.
        kept = [r for r in rows
                if str(r.get("id", ""))
                and not any(t in str(r.get("id", "")) for t in _NOT_CHAT)]
        kept.sort(key=lambda r: r.get("created") or 0, reverse=True)
        out = [{"id": str(r["id"]), "label": str(r["id"])} for r in kept]
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
               if pid == "anthropic" else
               {"x-goog-api-key": key} if pid == "google" else
               {"authorization": f"Bearer {key}"})
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=MODELS_TIMEOUT) as r:
            payload = json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — never raise out of a lookup
        return [], f"live list unavailable ({str(e)[:100]})"
    # Anthropic/OpenAI/xAI answer under "data"; Gemini under "models".
    rows = (payload.get("data") or payload.get("models")
            if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        return [], "unexpected models response"
    got = _shape_models(pid, rows)
    return got, (f"live from your API key — {len(got)} models" if got
                 else "the API key returned no usable models")


def _live_models(pid, refresh=False):
    key = api_key(pid)
    if not key:
        return [], ("no API key on file — connect one and this list comes "
                    "from the provider itself")
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

    The CLI list starts from the alias set its binary accepts — neither
    CLI has a "list models" subcommand to ask, and an alias is the
    spelling that keeps working across releases. When a key is on file the
    LIVE model list is unioned in after the aliases: the CLIs accept full
    model ids too, so a brand-new model is pickable the day it ships.

    The API list IS the live answer, and there is no fallback. A curated
    fallback is what put stale names on screen for months (MODEL SOURCES),
    so an unverifiable API list comes back EMPTY with a detail line saying
    what would fill it. The custom-id hatch in the UI covers the gap."""
    spec = PROVIDERS.get(pid)
    if not spec:
        return {"cli": [], "api": [], "api_live": False,
                "api_detail": "", "cli_detail": ""}
    live, detail = _live_models(pid, refresh)
    cli = cli_models(pid)
    if spec["models"]["cli"]:
        cli_detail = "aliases the CLI resolves to its newest models"
    elif cli:
        cli_detail = f"read from {spec['cli_config']['path']}"
    else:
        cli_detail = ""
    if cli and live:
        known = {m["id"] for m in cli}
        cli = cli + [m for m in live if m["id"] not in known]
        cli_detail += f" + {len(live)} live ids from your API key"
    return {"cli": cli,
            "api": live,
            "api_live": bool(live),
            "api_detail": detail,
            "cli_detail": cli_detail}


def default_api_model(pid, tier=""):
    """Which model an API call runs on when the owner has picked none.

    Vira ships NO default api_model — a shipped id is the thing that goes
    stale (MODEL SOURCES). So the default is derived at call time from the
    key's own live list: the newest model of the requested tier, else the
    newest model there is. Returns "" when nothing is verifiable, and the
    caller raises a named error rather than guessing a spelling."""
    live, _ = _live_models(pid)
    if not live:
        return ""
    tier = (tier or "").strip().lower()
    if tier:
        # The live list is newest-first, so the first tier match is the
        # newest of that tier — "sonnet" tracks Sonnet across generations
        # exactly the way the CLI alias does.
        for m in live:
            if tier in m["id"].lower():
                return m["id"]
    return live[0]["id"]


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
            "sessions_quality": _sessions_quality(pid,
                                                  spec["can"]["sessions"]),
            "config_keys": dict(spec["config_keys"]),
            **catalog(pid, refresh),
        })
    # active()'s ladder, re-derived from the records already probed above
    # rather than probing every provider a second time.
    want = str(settings.raw().get("ai_provider") or "anthropic")
    usable = [p["id"] for p in provs if p["connected"]]
    # The owner's curated roster (config model_roster): the ids every
    # picker should offer. Empty = uncurated, offer everything.
    roster = settings.raw().get("model_roster")
    payload = {"providers": provs,
               "active": want if want in usable else next(iter(usable), ""),
               "roster": [str(m) for m in roster]
               if isinstance(roster, list) else []}
    with _lock:
        _options_cache.update(at=now, payload=payload)
    return payload
