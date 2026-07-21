"""AI-backend health — the deterministic self-check that keeps an AI product
survivable when its own model auth fails.

The governing principle: troubleshooting infrastructure must never depend on
the thing it troubleshoots. When the Claude login expires, the LLM is exactly
what is unreachable — so detection, classification, the fallback decision, and
the owner alert are all plain Python + shell + the iMessage notify path. None
of them touch the model. This is the four-rung insurance the rest of Vira's
"everything that can be deterministic is" philosophy implies:

  1. probe()             is the AI backend authenticated and reachable? (green/red)
  2. classify(text)      map a raw auth failure to a friendly, actionable state
                         (never leak a stack trace; the caller never loses work)
  3. preferred_backend() the fallback ladder — a dead CLI login with a
                         VIRA_ANTHROPIC_KEY present routes to the API instead
  4. Watcher             a daemon that probes on a cadence and pings the owner
                         over iMessage on the green->red edge, so a silently
                         dead cockpit job never recurs

Single-tenant by design: one instance, one owner, their own login. The owner
IS the admin, so "notify + guide the human to re-login" fully closes the loop —
which is exactly why a personal-login deployment is viable rather than a
compromise. In a shared-key deployment the same probe still detects; only the
remediation channel changes (page on-call instead of text the owner).

Why no proactive "your token expires in N days" rung: the Max-plan OAuth
access token is short-lived and auto-refreshed hourly, so warning on it would
false-alarm constantly; the thing that actually lapses is the refresh token,
whose expiry `claude auth status` does not expose. The honest proactive signal
is instead the fast cadence probe catching loggedIn=false within one interval
of the refresh dying — before the owner ever discovers it via a dead job.

The probe is cheap and spends no tokens: `claude auth status` reports the
login state directly, and an API key is validated with a free GET /v1/models.
"""
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import filelock

_DATA = Path(__file__).resolve().parent.parent / "data"
STATE = _DATA / "ai-health.json"
CONFIG = _DATA / "config.json"

# Signatures of an auth/credit failure in CLI stderr or a session result_text.
# Matched case-insensitively — the deterministic tells that the model backend
# is unreachable for an AUTH reason (as opposed to a transient network blip,
# which is not our concern here and should just be retried by the caller).
_AUTH_TELLS = (
    "oauth session expired",
    "session expired and could not be refreshed",
    "failed to authenticate",
    "not logged in",
    "please run /login",
    "run `claude auth login`",
    "invalid api key",
    "invalid x-api-key",
    "authentication_error",
    "401 unauthorized",
    "credit balance is too low",
    "insufficient credit",
)


# ---------- config (read config.json directly, like notify.py) ----------

def _raw_cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cfg_bool(key, default):
    v = _raw_cfg().get(key)
    return default if v is None else bool(v)


def _cfg_num(key, default):
    v = _raw_cfg().get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _backend_config():
    c = _raw_cfg()
    return (c.get("ai_backend") or "cli",
            c.get("api_key_env") or "VIRA_ANTHROPIC_KEY")


def _api_key():
    """Env var first (existing installs), then the Keychain (a key pasted
    into Setup). models.api_key holds both halves of that lookup."""
    from . import models as provider
    pid = str(_raw_cfg().get("ai_provider") or "anthropic")
    if pid in provider.PROVIDERS:
        key = provider.api_key(pid)
        if key:
            return key
    return os.environ.get(_backend_config()[1], "")


def _strip_env():
    # Same gotcha as suggest.py: a session-scoped ANTHROPIC_*/CLAUDE* var makes
    # the child CLI ignore its stored login. Strip them so `auth status`
    # reports the REAL persistent credential state, not a var-shadowed one.
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE"))}


# ---------- rung 2: classify a raw failure ----------

def is_auth_failure(text):
    return any(t in (text or "").lower() for t in _AUTH_TELLS)


def classify(text):
    """Map a raw failure string from a model call to a friendly, actionable
    state. Deterministic string match — safe to call from any except-block,
    never raises. The message always tells the user their work was kept."""
    low = (text or "").lower()
    credit = "credit balance is too low" in low or "insufficient credit" in low
    auth = is_auth_failure(text) and not credit
    if auth:
        return {"kind": "auth", "needs_reauth": True,
                "message": "AI is temporarily unavailable — the Claude login "
                           "needs reconnecting (run `claude auth login`). Your "
                           "request was kept, not lost."}
    if credit:
        return {"kind": "credit", "needs_reauth": False,
                "message": "AI is temporarily unavailable — the API account is "
                           "out of credit. Your request was kept, not lost."}
    return {"kind": "other", "needs_reauth": False,
            "message": "AI is temporarily unavailable — please try again "
                       "shortly. Your request was kept, not lost."}


# ---------- rung 1: the deterministic probe ----------

def _probe_cli():
    """The CONFIGURED provider's subscription login, checked through its own
    CLI. No model call, no token spend. Returns (state, detail, extra).

    Delegates to models.probe so the check follows the provider actually in
    use — and so it finds a CLI that is installed but not on PATH, like the
    codex binary inside ChatGPT.app. The signature stays fixed: this is the
    seam the health tests stub."""
    from . import models as provider
    pid = str(_raw_cfg().get("ai_provider") or "anthropic")
    if pid not in provider.PROVIDERS:
        pid = "anthropic"
    try:
        rec = provider.probe(pid)
    except Exception as e:  # noqa: BLE001 — a probe must never crash the caller
        return "unknown", f"probe error: {str(e)[:120]}", {}
    if not rec:
        return "unknown", f"unknown provider {pid}", {}
    extra = {"provider": rec["id"], "authMethod": rec["auth"]}
    if rec["auth"] == provider.SIGNED_IN:
        return "green", rec["detail"], extra
    if rec["auth"] == provider.KEY:
        return "green", rec["detail"], extra
    if rec["auth"] == provider.ABSENT:
        return "red", rec["detail"], extra
    return "red", rec["detail"] or "not signed in", extra


def _probe_api(key):
    """Validate an API key WITHOUT spending tokens: GET /v1/models is free and
    returns 200 for a valid key, 401 for a bad one."""
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code = getattr(r, "status", 200)
        return ("green", "api key valid (/v1/models 200)", {}) if code == 200 \
            else ("unknown", f"/v1/models status {code}", {})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "red", "api key rejected (401)", {}
        return "unknown", f"/v1/models HTTP {e.code}", {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "unknown", f"api unreachable: {str(e)[:120]}", {}


def _action_for(state, backend, fallback):
    if state == "green":
        return ""
    if state == "unknown":
        return "AI status unknown — could not reach the backend to check."
    if backend == "cli":
        from . import models as provider
        pid = str(_raw_cfg().get("ai_provider") or "anthropic")
        if pid not in provider.PROVIDERS:
            pid = "anthropic"
        spec = provider.PROVIDERS[pid]
        login = provider.login_command(pid) or f"{spec['bin']} {' '.join(spec['login_args'])}"
        base = (f"AI is paused — the {spec['sub_name']} login is not active. "
                f"Open a terminal and run `{login}` to reconnect.")
        if fallback:
            base += " Reply drafting is falling back to the API key meanwhile."
        return base
    return "AI is paused — the API key was rejected. Check it in Setup > Connect your AI."


def probe(write=True):
    """Run the deterministic health check against the CONFIGURED primary
    backend and return a status dict. Cheap: no model call, no token spend.
    Does NOT alert — the Watcher / recheck endpoint decide that."""
    backend, _ = _backend_config()
    key = _api_key()
    if backend == "api" and key:
        state, detail, extra = _probe_api(key)
        checked = "api"
    else:
        state, detail, extra = _probe_cli()
        checked = "cli"
    # Is there a second rung if the primary is down? A dead CLI login can route
    # to an API key; a dead API key has no cheaper fallback here.
    fallback = "api" if (checked == "cli" and key) else None
    result = {"state": state, "detail": detail, "backend": checked,
              "fallback": fallback,
              "checked_at": datetime.now().isoformat(timespec="seconds"),
              **extra}
    result["action"] = _action_for(state, checked, fallback)
    if write:
        _record(result)
    return result


# ---------- rung 3: the fallback ladder ----------

def preferred_backend(configured, api_key):
    """Given the configured backend and whether an API key is present, return
    the backend a model call should ACTUALLY use right now. A configured CLI
    backend whose login is dead routes to the API when a key exists; otherwise
    the configured backend stands (the call fails honestly and note_failure
    flags it). Reads the LAST probe from the store — cheap, non-blocking — so a
    hot reply path never waits on a subprocess. A stale/absent store just means
    'trust the configured backend'."""
    if configured == "cli" and api_key:
        last = last_state()
        if last.get("state") == "red" and last.get("backend") == "cli":
            return "api"
    return configured


# ---------- store I/O (cross-process safe via filelock) ----------

def _record(result):
    prev = last_state()
    prev_state = prev.get("state")
    result["prev_state"] = prev_state
    result.setdefault("checked_at",
                      datetime.now().isoformat(timespec="seconds"))
    with filelock.locked(STATE):
        try:
            store = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            store = {}
        # A real transition needs a real prior state — the first-ever probe
        # (prev None) is initialization, not a state change, so it is not
        # logged to history (but latest is still written, and the alert layer
        # still treats a first-probe red as worth a heads-up).
        if prev_state and prev_state != result["state"]:
            result["changed_at"] = result["checked_at"]
            store.setdefault("history", []).append(
                {"at": result["checked_at"], "from": prev_state,
                 "to": result["state"], "detail": result.get("detail")})
            store["history"] = store["history"][-50:]
        else:
            result["changed_at"] = prev.get("changed_at", result["checked_at"])
        store["latest"] = result
        tmp = STATE.with_suffix(".json.tmp")
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(store, indent=1))
        tmp.replace(STATE)
    return result


def last_state():
    try:
        return json.loads(STATE.read_text()).get("latest", {})
    except (OSError, json.JSONDecodeError):
        return {}


def history(limit=20):
    try:
        h = json.loads(STATE.read_text()).get("history", [])
    except (OSError, json.JSONDecodeError):
        h = []
    return list(reversed(h))[:limit]


def summary():
    """Compact shape folded into /api/config for the header banner."""
    s = last_state()
    return {"state": s.get("state", "unknown"),
            "detail": s.get("detail", ""),
            "action": s.get("action", ""),
            "backend": s.get("backend", ""),
            "checked_at": s.get("checked_at", "")}


# ---------- rung 2 (passive) + rung 4 (alert) ----------

def note_failure(text, source="model-call"):
    """Any in-server model-call failure path can report its raw error here. An
    auth/credit failure flips the health state to red and alerts the owner once
    — so a failed reply draft surfaces the same way a failed cockpit job would,
    instead of dying as a raw traceback. Returns the classification. Never
    raises (a health side-effect must not break the caller's own error path)."""
    info = classify(text)
    try:
        if info["kind"] in ("auth", "credit"):
            backend, _ = _backend_config()
            fallback = "api" if (backend == "cli" and _api_key()) else None
            result = {"state": "red",
                      "detail": f"{info['kind']} failure from {source}: "
                                f"{(text or '')[:140]}",
                      "backend": backend, "fallback": fallback,
                      "checked_at": datetime.now().isoformat(timespec="seconds"),
                      "source": source}
            result["action"] = _action_for("red", backend, fallback)
            _record(result)
            maybe_alert(result)
    except Exception:  # noqa: BLE001
        pass
    return info


def maybe_alert(result):
    """Fire ONE out-of-band iMessage on a green/unknown -> red transition, and
    one on recovery. Deterministic — reuses notify.py (the same iMessage path
    the rest of Vira uses, needs no model). Deduped by notify's own throttle so
    a persistent red never storms the phone. Dormant unless a notify handle is
    configured, exactly like every other Vira notification."""
    if not _cfg_bool("ai_health_notify", True):
        return
    state, prev = result.get("state"), result.get("prev_state")
    try:
        from . import notify
        if state == "red" and prev != "red":
            notify.agent_ping(
                "Vira: AI backend is DOWN. "
                + (result.get("action") or result.get("detail") or ""),
                key="ai-health-red")
        elif state == "green" and prev == "red":
            notify.agent_ping("Vira: AI backend recovered — reconnected and "
                              "cockpit jobs will run again.", key="ai-health-ok")
    except Exception:  # noqa: BLE001 — alerting must never crash the watcher
        pass


class Watcher:
    """Rung 4: probe the AI backend on a cadence and alert the owner on the
    green->red edge. Runs only in the live server (VIRA_PASSIVE skips it, like
    every other worker). Deterministic end to end — the one thing guaranteed to
    still work when the model itself cannot."""

    def __init__(self):
        self._stop = threading.Event()
        self._t = None

    def start(self):
        if not _cfg_bool("ai_health_enabled", True):
            print("ai-health: watcher disabled by config")
            return
        self._t = threading.Thread(target=self._loop, daemon=True,
                                   name="vira-ai-health")
        self._t.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._stop.wait(20)  # let startup settle before the first probe
        while not self._stop.is_set():
            try:
                maybe_alert(probe(write=True))
            except Exception:  # noqa: BLE001 — the watcher must never die
                pass
            interval = max(60, int(_cfg_num("ai_health_interval_min", 5) * 60))
            self._stop.wait(interval)
