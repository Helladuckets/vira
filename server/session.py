"""Live two-way agent sessions — durable, detached, steerable.

Since the durable-runner build, every SDK job runs OUTSIDE the server as
its own detached process (server/runner.py, its own process group), so a
Vira restart no longer kills running jobs. This module is the supervisor
side of that split:

- launch: write the job dir (data/jobs/<id>/job.json), record the ledger
  launch row, spawn the runner detached, register a handle.
- observe: snapshots are assembled from the runner's state.json +
  output.log tail — the same legacy /api/jobs/{id} shape as ever (plus
  mode/awaiting/live/pending). A supervisor thread polls active job dirs,
  fans SSE pokes to the UI, and finalizes any runner that died without
  writing a finish (stale heartbeat + dead pid -> "orphaned").
- steer: say / permission / interrupt / close append command lines to the
  job's control.jsonl; the runner tails it. Because the whole exchange is
  file-based, a server booted AFTER the runner started re-attaches and
  keeps steering — permission cards survive restarts too.
- re-attach: at boot the supervisor scans data/jobs for state.json files
  still "running": live runners (fresh heartbeat or live pid) are
  re-registered as running sessions; dead ones are finalized. Only then is
  the joblog orphan sweep run, scoped to records with no live runner.

The permission gate, transcript rendering, and plan publishing live in the
runner now. If the SDK is not importable the registry falls back to the
legacy in-server subprocess --print path (steering and gating disabled,
loudly noted in the transcript) — the app never fails to boot because of
the SDK.
"""
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path

from . import ideas, jobfiles, joblog, plans, settings, viratools
from .suggest import _strip_env, config

try:
    import claude_agent_sdk  # noqa: F401 — presence check only; the runner
    SDK_AVAILABLE = True     # imports the real types in its own process
    SDK_IMPORT_ERROR = ""
except Exception as _e:  # noqa: BLE001 — any import failure means fallback
    SDK_AVAILABLE = False
    SDK_IMPORT_ERROR = str(_e)

LIB = Path.home() / ".claude"
# The plan-publish pipeline: a PreToolUse(ExitPlanMode) hook that renders
# plan markdown into a rich multi-page HTML page, deploys it to the owner's
# plan host, and prints the live URL. The RUNNER drives it directly (stdin
# JSON) so a Plan session stays read-only in the target repo — the session
# produces the markdown, Vira publishes it.
PLAN_HOOK = LIB / "scripts" / "plan-html-deploy.py"

OUTPUT_CAP = 200_000
SUPERVISOR_TICK = 0.4        # job-dir poll cadence (SSE pokes ride on this)
DIRS_KEEP = 400              # finished job dirs retained for History

# Session defaults — overridable per key in data/config.json (see
# config.example.json). session_auto_allow is the read-only tool set the
# gate approves without a UI round-trip; everything else that reaches the
# gate raises an Approve/Deny card.
SESSION_DEFAULTS = {
    "session_auto_allow": ["Read", "Grep", "Glob", "TodoWrite", "Task",
                           "NotebookRead", "WebSearch"],
    "session_permission_timeout": 600,   # seconds until default-deny
    "session_default_mode": "interactive",
    "session_max_live": 4,               # concurrent detached sessions cap
}

# Tools the READ-ONLY policy strips even when auto-allowed (audit P1-4):
# Task spawns subagents that answer to their own gate, WebSearch is network
# egress not a read, and update_module_map is the one true write on the
# vira native server. Read-only means reads.
READ_ONLY_EXCLUDE = {"Task", "WebSearch", "mcp__vira__update_module_map"}

# UI/circuit model keywords -> ids the CLI actually accepts. The short
# aliases (sonnet/opus/haiku) are CLI-native; fable is new enough that the
# full id is the safe spelling.
MODEL_ALIASES = {"fable": "claude-fable-5"}


def resolve_model(m):
    m = (m or "").strip()
    return MODEL_ALIASES.get(m.lower(), m) or None


def _scfg(key):
    v = config().get(key)
    return v if v not in (None, "") else SESSION_DEFAULTS[key]


# ---------- shared job helpers (runner.py imports these) ----------

def _extract_plan_md(output):
    """Pull the plan markdown out of a job's output — drop the CLI's leading
    connector warning and any wrapping code fence, start at the first
    '# ' title."""
    lines = [ln for ln in output.splitlines()
             if ln.strip() not in ("```", "```markdown", "```md")]
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("# "):
            return "\n".join(lines[i:]).strip()
    kept = [ln for ln in lines if not ln.lstrip().startswith("⚠")]
    return "\n".join(kept).strip()


def _publish_plan(md):
    """Publish plan markdown to the lab via the deploy hook; return the live
    URL (or None on failure). Blocking."""
    if not PLAN_HOOK.is_file() or not md.strip():
        return None
    payload = json.dumps({"tool_name": "ExitPlanMode",
                          "tool_input": {"plan": md}})
    try:
        res = subprocess.run(["python3", str(PLAN_HOOK)], input=payload,
                             capture_output=True, text=True, timeout=300,
                             env=_strip_env())
    except Exception:  # noqa: BLE001 — publish is best-effort
        return None
    m = re.search(r"https://\S+?/plans/\S+?\.html", res.stdout)
    return m.group(0) if m else None


def _finalize_plan(md, idea_id=None, job_id=None):
    """Finish a Plan-mode job: save the plan to the vault (universal — creates
    a Vira vault if none is connected) and, when the owner's private lab hook
    is present, ALSO publish the hosted page. Returns
    {plan_id, title, url} — url is None off the owner's machine, plan_id is
    None only if the vault save itself failed. Best-effort; never raises."""
    if os.environ.get("VIRA_PASSIVE"):
        # A test clone must never act on the world (send.py precedent): no lab
        # publish, and no write into the owner's REAL vault — vault_root lives
        # outside the cloned data/, so a save here would land in the live
        # Obsidian vault. The plan markdown stays in the terminal.
        return {"plan_id": None, "title": None, "url": None}
    url = _publish_plan(md)          # private hook; None where absent
    entry = None
    try:
        entry = plans.save_plan(md, idea_id=idea_id, job_id=job_id,
                                lab_url=url)
    except Exception:  # noqa: BLE001 — saving is best-effort, never fatal
        entry = None
    return {"plan_id": entry["id"] if entry else None,
            "title": entry["title"] if entry else None,
            "url": url}


def _plan_ref(res):
    """The reopenable in-app reference token stamped on idea notes and echoed
    in the job terminal: [plan <id>: <title>]. `]` is swapped out of the
    title so the client's linkifier (which stops at the first `]`) can never
    truncate the visible name."""
    title = (res.get("title") or "").replace("]", ")")
    return f"[plan {res['plan_id']}: {title}]"


def _mark_idea(job, ok, interrupted=False):
    """Final step of an idea-launched action: reflect the outcome back in the
    backlog. Implement success -> done; Plan success -> stays open, stamped
    with the published URL (a plan is a step toward the idea, not the
    finished work); interrupted -> stays open, noted; any failure -> stays
    open with a failure note (never silently marks done). Best-effort —
    never crash the session (the idea may have been edited or deleted
    meanwhile)."""
    stamp = date.today().isoformat()
    jid = job["id"][:8]
    try:
        if interrupted:
            ideas.update(job["idea_id"],
                         note=f"action interrupted {stamp} (job {jid}) — see terminal")
        elif not ok:
            ideas.update(job["idea_id"],
                         note=f"action failed {stamp} (job {jid}) — see terminal")
        elif job.get("publish_plan"):
            # The finalize step (server/session._finalize_plan) saved the plan
            # and stashed its {plan_id, title, url} on job["plan"]. Stamp the
            # idea with a reopenable in-app reference — the [plan <id>: <title>]
            # token linkifies in the Ideas note and the job terminal, opening
            # the plan viewer even after the terminal is gone. The idea STAYS
            # open: a plan is a step toward the idea, not the finished work.
            res = job.get("plan") or {}
            if res.get("plan_id"):
                note = f"plan saved {stamp} (job {jid}): {_plan_ref(res)}"
            elif res.get("url"):
                note = f"plan published {stamp} (job {jid}) — {res['url']}"
            else:
                note = f"plan produced {stamp} (job {jid}) — see terminal"
            ideas.update(job["idea_id"], note=note)
        else:
            ideas.update(job["idea_id"], status="done",
                         note=f"implemented by Vira {stamp} (job {jid})")
    except Exception:  # noqa: BLE001 — closing the loop is best-effort
        pass


def _tool_summary(block):
    """One-line label for a tool_use block, so the live log reads like a
    person watching the agent work."""
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        return f"{name} {inp.get('file_path', '')}".strip()
    if name == "Bash":
        return "Bash: " + (inp.get("command") or "").replace("\n", " ")[:100]
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern') or inp.get('query') or ''}".strip()
    if name == "TodoWrite":
        return "planning the steps…"
    if name == "Task":
        return "delegating a subtask…"
    return name


def _tool_preview(name, inp):
    """Multi-line detail for a permission card: the command for Bash, a
    content/diff preview for Write/Edit, compact JSON for anything else."""
    inp = inp or {}
    try:
        if name == "Bash":
            return (inp.get("command") or "")[:600]
        if name == "Write":
            body = (inp.get("content") or "")[:400]
            return f"{inp.get('file_path', '')}\n---\n{body}"
        if name in ("Edit", "NotebookEdit"):
            old = (inp.get("old_string") or "")[:200]
            new = (inp.get("new_string") or inp.get("new_source") or "")[:200]
            return f"{inp.get('file_path', '')}\n- {old}\n+ {new}"
        return json.dumps(inp, ensure_ascii=False, default=str)[:400]
    except Exception:  # noqa: BLE001 — a preview must never break the gate
        return ""


def _format_stream_line(line):
    """Turn one `--output-format stream-json` line into
    (human_progress_text, final_result_text, session_id). Used by the
    subprocess fallback path only; the runner renders typed SDK message
    objects to the same shapes."""
    line = line.rstrip("\n")
    if not line.strip():
        return "", None, None
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        if "claude.ai connectors are disabled" in line:
            return "", None, None
        return line + "\n", None, None
    t = ev.get("type")
    if t == "assistant":
        out = ""
        for b in ev.get("message", {}).get("content", []):
            bt = b.get("type")
            if bt == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    out += txt + "\n"
            elif bt == "tool_use":
                out += "  → " + _tool_summary(b) + "\n"
        return out, None, None
    if t == "result":
        return "", ev.get("result", "") or "", None
    if t == "system" and ev.get("subtype") == "init":
        sid = ev.get("session_id") or ""
        tail = f" (session {sid[:8]})" if sid else ""
        return f"[vira] {ev.get('model', 'claude')} working…{tail}\n", None, sid
    return "", None, None


def _sdk_env():
    """Env overrides for the SDK-spawned CLI so it authenticates with its own
    Max-plan login. The SDK transport MERGES ClaudeAgentOptions.env over the
    inherited os.environ (it cannot remove keys), so each unwanted inherited
    ANTHROPIC_*/CLAUDE* var is overridden with an empty string — the CLI
    treats empty as unset. CLAUDECODE is excluded (the SDK already filters
    it from the inherited env; blanking it here would re-add it), and so is
    CLAUDE_CODE_ENTRYPOINT (the SDK sets it to sdk-py; our blank would win
    the merge and clobber that). Never mutates os.environ — concurrent
    sessions each get their own copy."""
    skip = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
    # VIRA_ANTHROPIC_KEY matches neither prefix but is just as much an auth
    # source — without this line every spawned agent inherits the API key
    # whenever the optional API backend is configured (audit P1-1).
    return {k: "" for k in os.environ
            if (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE")
                or k == "VIRA_ANTHROPIC_KEY")
            and k not in skip}


# ---------- registry entries ----------

class DetachedJob:
    """Supervisor handle for one runner process. The truth lives on disk;
    `last_state` is the supervisor's cached copy (refreshed on its tick and
    on demand), `spec` is the immutable job.json content."""

    kind = "detached"

    def __init__(self, jid, jdir, spec, proc=None):
        self.id = jid
        self.dir = Path(jdir)
        self.spec = spec
        self.proc = proc                 # None on a post-boot re-attach
        self.last_state = None
        self._out_size = -1
        self._state_mtime = -1.0

    def read_state(self):
        st = jobfiles.read_json(self.dir / "state.json")
        if st:
            self.last_state = st
        return self.last_state

    def status(self):
        return (self.last_state or {}).get("status", "running")


class Session:
    """Legacy fallback run (SDK unavailable): an in-server subprocess with
    the exact legacy job shape in `data`."""

    kind = "legacy"

    def __init__(self, data):
        self.data = data


# ---------- the registry / supervisor ----------

class Sessions:
    """Registry of runs. SDK path: detached runner process per job,
    supervised through its job dir. Fallback path (SDK missing): the legacy
    subprocess thread, same public shape, steering disabled."""

    def __init__(self, keep=30):
        self.sessions = {}
        self.keep = keep
        self.lock = threading.Lock()
        self.listeners = []               # queue.Queue fan-out (SSE)
        self._sup = None                  # supervisor thread

    # ----- wiring -----

    def set_loop(self, loop):
        """Kept for compatibility — the registry no longer needs the event
        loop (all cross-process signalling is file-based)."""

    def subscribe(self, q):
        with self.lock:
            self.listeners.append(q)

    def unsubscribe(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def _emit(self, kind, sid, **payload):
        """Fan a session event out to every SSE subscriber. Events are
        pokes — the client refetches the session snapshot — so a dropped
        event only costs freshness until the 800ms poll catches up."""
        item = {"_sse": "session", "kind": kind, "id": sid, **payload}
        with self.lock:
            dead = []
            for q in self.listeners:
                try:
                    q.put_nowait(item)
                except Exception:  # noqa: BLE001 — full/closed queue
                    dead.append(q)
            for q in dead:
                self.listeners.remove(q)

    # ----- public registry API (thread-safe) -----

    def launch(self, prompt, cwd=None, permission_mode=None, model=None,
               publish_plan=False, idea_id=None, mode=None,
               read_only=False, meta=None):
        """Start a run; returns the job id. `mode` is "interactive"
        (gated) or "autopilot" (bypassPermissions, no gating); when absent
        it derives from the legacy permission_mode param, else the config
        default. read_only=True disallows write tools at the SDK level and
        the gate denies everything outside the auto-allow set instantly
        (judge sessions, circuit read stages). `meta` is a small dict
        recorded on the ledger row (circuit_run/stage/judge_of/routine_id).
        Raises ValueError when the live-session cap is hit."""
        if mode not in ("interactive", "autopilot"):
            mode = ("autopilot" if permission_mode == "bypassPermissions"
                    else str(_scfg("session_default_mode")))
            if mode not in ("interactive", "autopilot"):
                mode = "interactive"
        jid = uuid.uuid4().hex[:12]
        if cwd:
            cwd = str(Path(cwd).expanduser())
            if not Path(cwd).is_dir():
                cwd = None
        live = SDK_AVAILABLE
        data = {"id": jid, "prompt": prompt, "cwd": cwd or str(Path.home()),
                "status": "running", "output": "", "started": time.time(),
                "finished": None,
                "permission_mode": ("bypassPermissions" if mode == "autopilot"
                                    else permission_mode),
                "model": resolve_model(model), "publish_plan": publish_plan,
                "idea_id": idea_id, "session_id": "",
                "mode": mode, "awaiting": None, "live": live,
                "read_only": bool(read_only), "meta": meta or {}}
        with self.lock:
            if live:
                running = sum(
                    1 for x in self.sessions.values()
                    if x.kind == "detached" and x.status() == "running")
                cap = int(_scfg("session_max_live"))
                if running >= cap:
                    raise ValueError(
                        f"live-session cap reached ({running} running, "
                        f"cap {cap}) — wait for one to finish or close it")
            self._prune_registry()
        if live:
            self.sessions[jid] = self._spawn_runner(data)
        else:
            s = Session(data)
            self.sessions[jid] = s
            note = "claude-agent-sdk not installed"
            if mode == "interactive":
                self._append(s, "[vira] interactive session unavailable — "
                                f"{note}; running one-shot (no steering or "
                                "permission prompts)\n")
            threading.Thread(target=self._run_subprocess, args=(s,),
                             daemon=True, name=f"vira-job-{jid}").start()
        return jid

    def _spawn_runner(self, data):
        """Write the job dir and start the detached runner process (its own
        process group — it survives server restarts, launchd kills, us)."""
        jid = data["id"]
        jdir = jobfiles.job_dir(jid)
        jdir.mkdir(parents=True, exist_ok=True)
        spec = {
            "id": jid, "prompt": data["prompt"], "cwd": data["cwd"],
            "model": data["model"],
            "model_resolved": (data["model"]
                               or resolve_model(config()["cli_model"])),
            "permission_mode": data["permission_mode"],
            "publish_plan": data["publish_plan"],
            "idea_id": data["idea_id"], "mode": data["mode"],
            "read_only": data.get("read_only", False),
            "meta": data.get("meta") or {},
            "started": data["started"],
            "auto_allow": list(_scfg("session_auto_allow")),
            "permission_timeout": float(_scfg("session_permission_timeout")),
        }
        jobfiles.write_json_atomic(jdir / "job.json", spec)
        (jdir / "control.jsonl").touch()
        joblog.record_launch(data)
        log = open(jdir / "runner.log", "ab")
        # The runner must outlive this server (restart survival). POSIX:
        # its own session via setsid. Windows silently IGNORES
        # start_new_session, so detach explicitly with creationflags.
        if settings.IS_WIN:
            detach = {"creationflags": (subprocess.DETACHED_PROCESS
                                        | subprocess.CREATE_NEW_PROCESS_GROUP)}
        else:
            detach = {"start_new_session": True}
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "server.runner", str(jdir)],
                cwd=str(jobfiles.ROOT), stdout=log, stderr=subprocess.STDOUT,
                **detach)
        finally:
            log.close()
        handle = DetachedJob(jid, jdir, spec, proc)
        # Synthetic pre-state so snapshots work in the instant before the
        # runner writes its first state.json.
        handle.last_state = {
            "id": jid, "status": "running", "started": data["started"],
            "finished": None, "session_id": "", "awaiting": None,
            "pending": [], "result_text": "", "heartbeat": time.time(),
            "pid": proc.pid, "mode": data["mode"], "live": True, "error": "",
        }
        return handle

    def _prune_registry(self):
        """Drop the oldest finished entries past `keep` (dirs stay on disk
        for the History tab). Caller holds the lock."""
        if len(self.sessions) <= self.keep:
            return
        by_age = sorted(self.sessions.values(),
                        key=lambda x: (x.spec["started"]
                                       if x.kind == "detached"
                                       else x.data["started"]))
        for old in itertools.islice(iter(by_age), 5):
            status = (old.status() if old.kind == "detached"
                      else old.data["status"])
            if status != "running":
                oid = old.id if old.kind == "detached" else old.data["id"]
                self.sessions.pop(oid, None)

    def _snapshot_detached(self, h, with_output=True):
        st = h.read_state() or {}
        spec = h.spec
        snap = {
            "id": h.id, "prompt": spec["prompt"], "cwd": spec["cwd"],
            "status": st.get("status", "running"),
            "output": (jobfiles.tail_output(h.dir, OUTPUT_CAP)
                       if with_output else ""),
            "started": spec.get("started") or st.get("started"),
            "finished": st.get("finished"),
            "permission_mode": spec.get("permission_mode"),
            "model": spec.get("model"),
            "publish_plan": spec.get("publish_plan"),
            "idea_id": spec.get("idea_id"),
            "session_id": st.get("session_id", ""),
            "mode": spec.get("mode"),
            "read_only": spec.get("read_only", False),
            "meta": spec.get("meta") or {},
            "awaiting": st.get("awaiting"),
            "live": True,
            "result_text": st.get("result_text", ""),
            "pending": sorted(st.get("pending") or [],
                              key=lambda p: p.get("created", 0)),
        }
        return snap

    def get(self, jid):
        """JSON-safe snapshot in the legacy /api/jobs/{id} shape, plus the
        session fields (mode, awaiting, live, pending)."""
        obj = self.sessions.get(jid)
        if obj is None:
            return None
        if obj.kind == "detached":
            return self._snapshot_detached(obj)
        snap = dict(obj.data)
        snap["pending"] = []
        return snap

    def recent(self):
        with self.lock:
            rows = []
            for obj in self.sessions.values():
                if obj.kind == "detached":
                    st = obj.last_state or {}
                    rows.append({
                        "id": obj.id, "prompt": obj.spec["prompt"],
                        "status": st.get("status", "running"),
                        "started": obj.spec.get("started"),
                        "finished": st.get("finished"),
                        "mode": obj.spec.get("mode"),
                        "awaiting": st.get("awaiting")})
                else:
                    d = obj.data
                    rows.append({
                        "id": d["id"], "prompt": d["prompt"],
                        "status": d["status"], "started": d["started"],
                        "finished": d["finished"], "mode": d["mode"],
                        "awaiting": d["awaiting"]})
            return sorted(rows, key=lambda j: j["started"], reverse=True)

    # ----- session controls (control.jsonl appends; the runner tails) -----

    def _require_live(self, jid):
        obj = self.sessions.get(jid)
        if obj is None:
            raise KeyError(jid)
        if obj.kind != "detached":
            raise ValueError("not an interactive session — steering and "
                             "permissions need the claude-agent-sdk")
        if obj.status() != "running":
            raise ValueError("session is not running")
        return obj

    def say(self, jid, text):
        """Queue a steering message; the runner delivers it at the next turn
        boundary (and echoes it into the transcript within ~250ms)."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        h = self._require_live(jid)
        jobfiles.append_control(h.dir, {"op": "say", "text": text})

    def permission(self, jid, req_id, allow, scope="once", reason=None):
        """Resolve a pending Approve/Deny card."""
        h = self._require_live(jid)
        st = h.read_state() or {}
        if not any(p.get("req_id") == req_id
                   for p in st.get("pending") or []):
            raise KeyError(req_id)
        jobfiles.append_control(h.dir, {
            "op": "permission", "req_id": req_id, "allow": bool(allow),
            "scope": scope or "once", "reason": reason})

    def interrupt(self, jid):
        """End the current turn. Queued steering still delivers afterwards;
        an idle inbox ends the session."""
        h = self._require_live(jid)
        jobfiles.append_control(h.dir, {"op": "interrupt"})

    def close(self, jid):
        """End the session entirely: the runner discards queued steering,
        denies pending permissions, interrupts the current turn."""
        h = self._require_live(jid)
        jobfiles.append_control(h.dir, {"op": "close"})

    # ----- the supervisor (boot re-attach + poll loop) -----

    def start_supervisor(self):
        """Called once from server startup. Re-attaches to runners that
        survived the last server, finalizes the ones that didn't, sweeps
        the ledger, prunes ancient job dirs, then starts the poll thread."""
        alive = self._boot_reattach()
        joblog.sweep_orphans(alive)
        self._prune_dirs()
        if self._sup is None:
            self._sup = threading.Thread(target=self._poll_loop,
                                         daemon=True, name="vira-supervisor")
            self._sup.start()

    def _boot_reattach(self):
        alive = []
        if not jobfiles.JOBS_DIR.is_dir():
            return alive
        for jdir in jobfiles.JOBS_DIR.iterdir():
            state = jobfiles.read_json(jdir / "state.json")
            spec = jobfiles.read_json(jdir / "job.json")
            if not state or not spec:
                continue
            if state.get("status") != "running":
                continue
            if jobfiles.runner_dead(state):
                self._finalize_dead(jdir, state)
                continue
            h = DetachedJob(spec["id"], jdir, spec)
            h.last_state = state
            with self.lock:
                self.sessions[h.id] = h
            alive.append(h.id)
        return alive

    def _finalize_dead(self, jdir, state):
        state["status"] = "orphaned"
        state["finished"] = state.get("finished") or time.time()
        state["awaiting"] = None
        state["pending"] = []
        jobfiles.write_json_atomic(Path(jdir) / "state.json", state)
        joblog.mark_orphaned(state.get("id") or Path(jdir).name)

    def _prune_dirs(self):
        """Cap data/jobs to the newest DIRS_KEEP finished dirs (running jobs
        are never pruned)."""
        try:
            dirs = [d for d in jobfiles.JOBS_DIR.iterdir() if d.is_dir()]
        except OSError:
            return
        dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[DIRS_KEEP:]:
            state = jobfiles.read_json(d / "state.json") or {}
            if state.get("status") == "running" and not jobfiles.runner_dead(state):
                continue
            shutil.rmtree(d, ignore_errors=True)

    def _poll_loop(self):
        while True:
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — the supervisor never dies
                pass
            time.sleep(SUPERVISOR_TICK)

    def _poll_once(self):
        with self.lock:
            handles = [x for x in self.sessions.values()
                       if x.kind == "detached"]
        for h in handles:
            if h.proc is not None:
                h.proc.poll()        # reap the child if it exited (no zombies)
            if (h.last_state or {}).get("status") != "running":
                continue
            try:
                st_m = (h.dir / "state.json").stat().st_mtime
            except OSError:
                st_m = -1.0
            try:
                out_sz = (h.dir / "output.log").stat().st_size
            except OSError:
                out_sz = -1
            changed = (st_m != h._state_mtime or out_sz != h._out_size)
            if not changed:
                # No file movement: is the runner even alive anymore?
                st = h.last_state or {}
                if jobfiles.runner_dead(st):
                    self._finalize_dead(h.dir, dict(st))
                    h.read_state()
                    self._emit("status", h.id, status="orphaned")
                elif (h.proc is not None and h.proc.poll() is not None
                      and st_m < 0):
                    # Spawn failure: the runner exited before its first
                    # state write (e.g. SDK import error) — see runner.log.
                    dead = {
                        "id": h.id, "status": "error",
                        "started": h.spec.get("started"),
                        "finished": time.time(), "session_id": "",
                        "awaiting": None, "pending": [], "result_text": "",
                        "heartbeat": 0, "pid": None,
                        "mode": h.spec.get("mode"), "live": True,
                        "error": "runner failed to start — see runner.log",
                    }
                    jobfiles.write_json_atomic(h.dir / "state.json", dead)
                    h.last_state = dead
                    joblog.record_finish(h.id, "error", dead["error"])
                    self._emit("status", h.id, status="error")
                continue
            prev_status = (h.last_state or {}).get("status")
            h._state_mtime = st_m
            h._out_size = out_sz
            st = h.read_state() or {}
            if st.get("status") != prev_status and st.get("status") != "running":
                self._emit("status", h.id, status=st.get("status"))
            else:
                self._emit("update", h.id)

    # ----- transcript (legacy fallback only) -----

    def _append(self, s, piece):
        if not piece:
            return
        d = s.data
        d["output"] += piece
        if len(d["output"]) > OUTPUT_CAP:
            d["output"] = d["output"][-OUTPUT_CAP:]
        self._emit("update", d["id"])

    # ----- the subprocess fallback (legacy --print path) -----

    def _run_subprocess(self, s):
        d = s.data
        cfg = config()
        # stream-json (needs --verbose) gives a live event stream — tool
        # calls, assistant text — instead of one buffered dump at the end.
        cmd = ["claude", "--print", "--verbose", "--output-format",
               "stream-json", "--model",
               d.get("model") or resolve_model(cfg["cli_model"]) or "sonnet"]
        if d.get("permission_mode"):
            cmd += ["--permission-mode", d["permission_mode"]]
        # No SDK here, so no system-prompt append and no native tools — the
        # Vira preamble (HTTP-API flavor) rides the prompt instead.
        cmd.append(viratools.preamble(native=False) + "\n\n---\n\n"
                   + d["prompt"])
        result_text = ""
        joblog.record_launch(d)
        try:
            proc = subprocess.Popen(cmd, cwd=d["cwd"], env=_strip_env(),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                piece, rtext, sid = _format_stream_line(line)
                if piece:
                    self._append(s, piece)
                if rtext is not None:
                    result_text = rtext
                if sid:
                    d["session_id"] = sid
                    joblog.record_session(d["id"], sid)
            proc.wait(timeout=1800)
            ok = proc.returncode == 0
        except Exception as e:  # noqa: BLE001 — job surface, report all
            self._append(s, f"\n[vira] job failed: {e}")
            ok = False
        d["result_text"] = result_text
        if ok and d.get("publish_plan"):
            md = _extract_plan_md(result_text or d["output"])
            self._append(s, "\n\n[vira] saving the plan…\n")
            res = _finalize_plan(md, d.get("idea_id"), d["id"])
            d["plan"] = res
            self._append(s, (
                f"[vira] plan saved: {_plan_ref(res)}\n" if res.get("plan_id")
                else "[vira] plan could not be saved — see runner.log\n"))
            if res.get("url"):
                self._append(s, f"[vira] plan published: {res['url']}\n")
        d["status"] = "done" if ok else "error"
        if d.get("idea_id"):
            _mark_idea(d, ok)
        d["finished"] = time.time()
        joblog.record_finish(d["id"], d["status"], result_text)
        self._emit("status", d["id"], status=d["status"])


sessions = Sessions()
