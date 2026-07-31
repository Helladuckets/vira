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

from . import (agentbackend, ideas, jobfiles, joblog, plans, settings,
               viratools, worktree)
from .suggest import config

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
    # bypassPermissions is the shipped default (owner's call, 2026-07-29).
    # The containment is structural, not per-call: a writing session is
    # PLACED in its own worktree and the gate refuses any write aimed back
    # at the live checkout, so the result is always either merged or thrown
    # away with the branch. Clicking Approve on mechanical calls bought
    # nothing — the session that broke the desktop on 2026-07-25 was one
    # the owner was approving call by call.
    "session_default_mode": "bypassPermissions",
    "session_max_live": 4,               # concurrent detached sessions cap
    "session_reply_window_hours": 12,    # safety reap for an idle linger
    # A writing session in a branch-first repo gets its own worktree, and
    # the gate refuses writes to the live checkout. See worktree.py for why
    # this is placement-and-enforcement rather than a line in the preamble.
    "session_branch_first": True,
    "session_ask_timeout": 21600,        # 6h for an owner decision
}

# The permission ladder, safest first. A session's mode is ONE of these —
# it decides what the gate waves through, never whether the owner can talk
# to the session (every mode is steerable; see the runner's reply window).
#   manual             every risky call raises an Approve/Deny card
#   acceptEdits        edits land unasked, commands still raise a card
#   bypassPermissions  everything runs; only the two non-negotiable denials
#                      (read-only, and a write aimed at the live checkout)
#                      still fire — see runner.gate
# Ordered, so a future rung slots in without rewriting the callers.
#
# NAMED TO MATCH CLAUDE CODE's own --permission-mode values (owner's call,
# 2026-07-29), so a rung means the same thing in both places and the two can
# be compared at a glance. The old names were also actively misleading:
# "interactive" implied the other rungs were not, when the steer bar has
# never keyed on mode at all (app.js composeState) — EVERY rung is
# interactive at the owner's discretion.
MODES = ("manual", "acceptEdits", "bypassPermissions")
DEFAULT_MODE = "bypassPermissions"

# Retired spellings -> current. Stored modes outlive a rename: they sit in
# data/jobs/*/job.json, in the joblog ledger rows the Work window renders,
# in circuit stage definitions, in routines.json, and in the browser's
# vira-idea-perm. Normalizing on READ means none of that has to be migrated
# and an old record can never fall through to a stricter-or-looser default.
LEGACY_MODES = {"interactive": "manual", "acceptedits": "acceptEdits",
                "autopilot": "bypassPermissions"}


def norm_mode(m, default=None):
    """Canonical rung name for `m`, accepting retired spellings and any
    casing. Returns `default` when it names no rung."""
    s = str(m or "").strip()
    if s in MODES:
        return s
    low = s.lower()
    if low in LEGACY_MODES:
        return LEGACY_MODES[low]
    for canon in MODES:
        if canon.lower() == low:
            return canon
    return default

# What "edits land unasked" means. The acceptedits rung is enforced in
# Vira's OWN gate (runner.gate), not by handing the SDK its acceptEdits
# permission_mode: the gate stays the single auditable place where policy
# lives, so the rung can never quietly widen because an SDK release
# changed what it short-circuits.
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Tools the READ-ONLY policy strips even when auto-allowed (audit P1-4):
# Task spawns subagents that answer to their own gate, and WebSearch is
# network egress not a read. The vira native server's write tools come
# from viratools.WRITE_TOOLS so a new one is excluded the day it ships
# rather than the day someone notices. Read-only means reads.
READ_ONLY_EXCLUDE = {"Task", "WebSearch"} | viratools.WRITE_TOOLS

# The two halves of the launch-dict -> job.json derivation (_spawn_runner).
#
# RUNNER_OWNED is the ONLY reason a launch input does not reach the runner:
# these are live-state fields the runner itself owns and republishes in
# state.json, so shipping them in the immutable spec would just be a stale
# copy. Everything else in the launch dict is a launch INPUT and rides along
# automatically — that default is the fix for the four-time key drop.
RUNNER_OWNED = {"status", "output", "finished", "session_id", "awaiting",
                "live"}

# Fields _spawn_runner computes at spawn time rather than reading from the
# launch dict. Named here so the structural test can state the whole contract
# as one set equation instead of a hand-maintained list that drifts.
SPAWN_COMPUTED = {"provider", "model_resolved", "auto_allow",
                  "permission_timeout", "reply_window"}

# UI/circuit model keywords -> ids the CLI actually accepts.
#
# STILL EMPTY, and it must stay that way. This table used to widen `fable`
# to `claude-fable-5` back when the alias was too young for the CLI to know
# it — which quietly turned a tier keyword into a GENERATION PIN: every
# circuit stage and routine that says "fable" would still be running Fable
# 5 the week Fable 6 shipped, with nothing on screen to say so. A widening
# entry here is a stale name by another name — see models.py MODEL SOURCES,
# and test_no_shipped_model_id_names_a_generation, which fails the build.
MODEL_ALIASES = {}

# The escape hatch for when an alias is WRONG on this machine, which is not
# hypothetical: measured 2026-07-29 on Claude Code 2.1.207, `--model opus`
# resolves to claude-opus-4-8 while `--model claude-opus-5` answers fine.
# So every session had been running a generation behind whatever the picker
# said, because the picker sends the bare alias and the alias is stale.
#
# Config key `model_alias_overrides`, default {} — OWNER DATA, never a
# shipped literal, so the no-hardcoded-id rule is intact and the override
# lives somewhere visible and editable rather than buried in code. It
# applies at resolve_model, which is the single funnel every anthropic
# launch already passes through (idea dispatches, circuit stages, routines,
# judges, the Applications apply), so one entry fixes all of them at once.
#
# Re-measure after any CLI upgrade and DELETE the entry once the alias is
# correct again — an override left in place is exactly the generation pin
# the table above exists to prevent:
#   claude --print --output-format json --model opus 'Say OK.'   # read modelUsage
ALIAS_OVERRIDE_KEY = "model_alias_overrides"


def alias_overrides():
    v = config().get(ALIAS_OVERRIDE_KEY)
    return v if isinstance(v, dict) else {}


def resolve_model(m):
    """The id to actually launch for a UI/circuit model keyword.

    Owner overrides win: they exist to correct an alias the CLI resolves
    WRONG on this machine, so they must outrank the alias itself.
    """
    m = (m or "").strip()
    if not m:
        return None
    over = alias_overrides().get(m.lower())
    if isinstance(over, str) and over.strip():
        return over.strip()
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
    # vira_plan_id tells the repointed hook to SKIP its vault-markdown write —
    # _finalize_plan saves the markdown via plans.py, and a second copy under
    # a different stem would just collide with that registry's naming.
    payload = json.dumps({"tool_name": "ExitPlanMode",
                          "tool_input": {"plan": md, "vira_plan_id": True}})
    try:
        res = subprocess.run(["python3", str(PLAN_HOOK)], input=payload,
                             capture_output=True, text=True, timeout=300,
                             env=settings.strip_env())
    except Exception:  # noqa: BLE001 — publish is best-effort
        return None
    # Hook URLs are Vira-served now (http over the tailnet, /docs/plans/);
    # the old https lab form still matches for a machine running the pre-
    # repoint hook.
    m = re.search(r"https?://\S+?/plans/\S+?\.html", res.stdout)
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
            ideas.stamp_note(job["idea_id"],
                             f"action interrupted {stamp} (job {jid}) — see terminal")
        elif not ok:
            ideas.stamp_note(job["idea_id"],
                             f"action failed {stamp} (job {jid}) — see terminal")
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
            ideas.stamp_note(job["idea_id"], note)
        else:
            ideas.stamp_note(job["idea_id"],
                             f"implemented by Vira {stamp} (job {jid})",
                             status="done")
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

    def working(self):
        """Running AND actually consuming a turn. A session parked in its
        reply window has finished its work and is only holding the door
        open for the owner, so it must not block a new launch against the
        live-session cap — otherwise a handful of unanswered questions
        would wedge the cockpit shut."""
        st = self.last_state or {}
        return (st.get("status", "running") == "running"
                and st.get("awaiting") not in ("reply", "paused"))


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
               read_only=False, meta=None, provider=None):
        """Start a run; returns the job id. `mode` is one of MODES — the
        permission ladder (manual / acceptEdits / bypassPermissions), and
        retired spellings still resolve via norm_mode; when absent it
        derives from the legacy permission_mode param, else the config
        default. read_only=True disallows write tools at the SDK level and
        the gate denies everything outside the auto-allow set instantly
        (judge sessions, circuit read stages). `meta` is a small dict
        recorded on the ledger row (circuit_run/stage/judge_of/routine_id).
        Raises ValueError when the live-session cap is hit."""
        mode = norm_mode(mode)
        if mode is None:
            mode = ("bypassPermissions"
                    if permission_mode == "bypassPermissions"
                    else norm_mode(_scfg("session_default_mode")))
            if mode is None:
                mode = DEFAULT_MODE
        jid = uuid.uuid4().hex[:12]
        if cwd:
            cwd = str(Path(cwd).expanduser())
            if not Path(cwd).is_dir():
                cwd = None
        # Which engine drives this session: an explicit provider wins, else
        # the model names it, else the gated default (anthropic). A
        # CLI-exec provider runs the detached runner even without the SDK.
        prov = agentbackend.session_provider(model=model, provider=provider)
        if not agentbackend.sessions_quality(prov):
            raise ValueError(
                f"{prov} cannot host live agent sessions yet — pick an "
                "Anthropic or OpenAI model")
        live = SDK_AVAILABLE or agentbackend.uses_cli_exec({"provider": prov})
        # Branch-first placement, decided HERE rather than asked of the model.
        # A session that can write lands in its own worktree; the gate then
        # refuses any write aimed back at the live checkout. Read-only and
        # plan sessions are skipped — they cannot damage anything, and a
        # worktree per plan run would litter the repo with empty branches.
        branch_slug = wt_path = live_root = None
        branch_note = ""
        if (cwd and not read_only and not publish_plan
                and bool(_scfg("session_branch_first"))):
            root = worktree.repo_root(cwd)
            if root and worktree.is_branch_first(root) and worktree.is_worktree(root):
                # RE-ENTRY: cwd is already a linked worktree (the orphan-work
                # sweeper's Resume action lands here — a leftover worktree
                # from a stalled session, not a fresh dispatch). Arm the same
                # guard fields a fresh placement would set, derived from the
                # worktree itself rather than minted. Without this the spec
                # ships with worktree/live_root empty and
                # runner._disarmed_guard fail-closed refuses to start the
                # session at all.
                prim = worktree.primary_root(root)
                br = worktree.current_branch(root)
                if prim and br.startswith("claude/"):
                    wt_path = root
                    live_root = prim
                    branch_slug = br.split("/", 1)[1]
                    branch_note = (
                        f"[vira] branch-first: resuming in existing worktree "
                        f"{root} on {br}; the live checkout at {prim} is "
                        "read-only for this session\n")
                # else: leave everything unset. A worktree whose branch or
                # primary root can't be derived is exactly the case the
                # disarmed-guard refusal exists for — loud is correct here.
            elif root and worktree.is_branch_first(root):
                # Name it after what was asked, suffixed with the job id so
                # two dispatches of the same idea never collide on a branch.
                #
                # The FIRST LINE of the prompt is not what was asked. Every
                # machine-composed dispatch opens with its own preamble — an
                # Implement job leads with "You are Vira's coding agent,
                # working in the git repository at ~/workspace/vira." and the
                # idea text sits ten lines down — so six dispatches on
                # 2026-07-29 all slugged to `you-are-vira-s-coding-agent-work`
                # and only the job id told them apart. joblog.command() already
                # solves this for the ledger, the terminal title bar and the
                # change log ("Implement — <the idea>", "Routine — muse",
                # "Circuit step — build"), so the branch reads the same as
                # every other surface naming the same job rather than carrying
                # a second, worse implementation of the same idea.
                try:
                    head = joblog.command({
                        "prompt": prompt, "idea_id": idea_id,
                        "publish_plan": publish_plan, "meta": meta or {}})
                except Exception:  # noqa: BLE001 — a name must never block a run
                    head = (prompt or "").strip().splitlines()[:1]
                    head = " ".join(head)
                branch_slug = worktree.slugify(head[:60], fallback="session")
                branch_slug = f"{branch_slug}-{jid[:6]}"[:40].strip("-")
                wt_path, made, detail = worktree.ensure(root, branch_slug)
                if wt_path:
                    # capture the live root BEFORE cwd moves — afterwards
                    # repo_root(cwd) answers with the worktree instead
                    live_root = root
                    cwd = str(wt_path)
                    branch_note = (
                        f"[vira] branch-first: working in {cwd} on "
                        f"claude/{branch_slug} ({detail}); the live checkout "
                        f"at {root} is read-only for this session\n")
                else:
                    branch_slug = None
                    branch_note = (
                        f"[vira] branch-first: could NOT create a worktree "
                        f"({detail}) — running in {cwd}. Nothing enforces the "
                        f"live tree here; commit nothing.\n")
        data = {"id": jid, "prompt": prompt, "cwd": cwd or str(Path.home()),
                "status": "running", "output": "", "started": time.time(),
                "finished": None,
                "permission_mode": ("bypassPermissions"
                                    if mode == "bypassPermissions"
                                    else permission_mode),
                "model": (resolve_model(model) if prov == "anthropic"
                          else (model or "").strip() or None),
                "provider": prov,
                "publish_plan": publish_plan,
                "idea_id": idea_id, "session_id": "",
                "mode": mode, "awaiting": None, "live": live,
                "read_only": bool(read_only), "meta": meta or {},
                # the guard's two halves: where writes ARE allowed, and the
                # tree they must stay out of
                "worktree": str(wt_path) if wt_path else "",
                "branch": f"claude/{branch_slug}" if branch_slug else "",
                "live_root": str(live_root) if live_root else "",
                # emitted by the runner at startup — a placement decided
                # silently is one nobody can check, and "which tree was that
                # session editing?" is exactly the question the 2026-07-25
                # incident left unanswerable
                "branch_note": branch_note,
                "ask_timeout": float(_scfg("session_ask_timeout"))}
        with self.lock:
            if live:
                running = sum(
                    1 for x in self.sessions.values()
                    if x.kind == "detached" and x.working())
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
            if mode == "manual":
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
        prov = data.get("provider") or "anthropic"
        # DERIVED, never retyped. This used to be a hand-written dict literal
        # naming every key it wanted, and that seam has silently dropped a
        # launch input FOUR times: read_only/meta, then reply_window, then
        # provider, then — the expensive one — the whole branch-first guard.
        # `worktree`/`live_root` never reached job.json, so runner.gate's
        # denial could not fire, the model never learned it was in a worktree,
        # and no transcript ever said which tree it edited (2026-07-25 ->
        # diagnosed 2026-07-29). A new launch input now rides along by
        # default; dropping one takes an explicit entry below.
        spec = {k: v for k, v in data.items() if k not in RUNNER_OWNED}
        spec.update({
            "provider": prov,
            # A launch that names no model runs the PROVIDER's configured
            # default, not anthropic's.
            "model_resolved": (data["model"]
                               or (resolve_model(config()["cli_model"])
                                   if prov == "anthropic"
                                   else agentbackend.default_model(prov))),
            "auto_allow": list(_scfg("session_auto_allow")),
            "permission_timeout": float(_scfg("session_permission_timeout")),
            "reply_window": float(_scfg("session_reply_window_hours")) * 3600,
        })
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
            # What the CLI resolved the request to — app.js prefers this
            # everywhere it names a model, so an alias that is not tracking
            # the newest generation shows as itself instead of hiding.
            "model_used": st.get("model_used", ""),
            "provider": spec.get("provider", "anthropic"),
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

    def pending_all(self):
        """Every unanswered decision card across every live session, oldest
        first — the whole "waiting on you" set in one read.

        This is what makes a decision reachable from anywhere in the app
        instead of only from inside its own terminal. A card that nobody is
        looking at is a session quietly stalled, which is the failure the
        ask_owner card was built to end (2026-07-25); a card only rendered
        in one window is the same failure one layer up.

        Scoped to the REGISTRY, never a scan of data/jobs: only a session
        this server supervises can be answered (permission/answer both go
        through _require_live), and a card that cannot be actioned is worse
        than no card at all.

        state.json is read FRESH rather than off `last_state` — the cached
        copy is refreshed by the supervisor, which does not run on a passive
        instance, and a decision list that silently stops updating is the
        one thing this surface must not do. The read is deliberately
        side-effect free (it does NOT touch `last_state`): the supervisor
        detects status transitions by comparing its cached status against a
        fresh read, so refreshing the cache from here could swallow the
        transition event it is watching for.
        """
        with self.lock:
            handles = [x for x in self.sessions.values()
                       if x.kind == "detached"]
        rows = []
        for h in handles:
            st = jobfiles.read_json(h.dir / "state.json") or {}
            if st.get("status") != "running":
                continue
            for card in st.get("pending") or []:
                if not isinstance(card, dict) or not card.get("req_id"):
                    continue
                rows.append({
                    "job_id": h.id,
                    "mode": h.spec.get("mode"),
                    "provider": h.spec.get("provider", "anthropic"),
                    "cwd": h.spec.get("cwd", ""),
                    "card": card,
                })
        rows.sort(key=lambda r: r["card"].get("created", 0))
        return rows

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

    def answer(self, jid, req_id, text):
        """Answer a pending decision card. Same shape as permission() — the
        card the session is blocked on is addressed by req_id, and an answer
        for a card that is no longer pending is a 404 rather than a silent
        no-op, so a double-tap on a phone cannot look like it worked."""
        h = self._require_live(jid)
        st = h.read_state() or {}
        if not any(p.get("req_id") == req_id and p.get("kind") == "ask"
                   for p in st.get("pending") or []):
            raise KeyError(req_id)
        answer = str(text or "").strip()
        if not answer:
            raise ValueError("an answer is required")
        jobfiles.append_control(h.dir, {
            "op": "answer", "req_id": req_id, "answer": answer})

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

    def _tidy_worktree(self, h):
        """A finished session's worktree goes away if it held nothing.

        Called on every path out of `running` — done, error, orphaned, a
        spawn that never started. NOT on the reply-window park, which keeps
        status `running` on purpose: the session is still the owner's to
        answer, and its tree is still theirs to look at.

        The outcome is appended to the transcript either way. A directory
        that silently appears and silently vanishes is how the owner ended
        up asking where these came from; a kept one has a reason, and that
        reason is the thing worth reading."""
        spec = h.spec or {}
        wt, root, branch = (spec.get("worktree"), spec.get("live_root"),
                            spec.get("branch"))
        if not (wt and root and branch):
            return
        try:
            removed, detail = worktree.tidy(root, wt, branch)
        except Exception as e:  # noqa: BLE001 — the supervisor never dies
            removed, detail = False, f"tidy failed: {e}"
        try:
            with (h.dir / "output.log").open("a", encoding="utf-8") as f:
                f.write(f"\n[vira] worktree: {detail}\n" if removed else
                        f"\n[vira] worktree kept at {wt} — {detail}\n")
        except OSError:
            pass

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
                    self._tidy_worktree(h)
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
                    self._tidy_worktree(h)
                    self._emit("status", h.id, status="error")
                continue
            prev_status = (h.last_state or {}).get("status")
            h._state_mtime = st_m
            h._out_size = out_sz
            st = h.read_state() or {}
            if st.get("status") != prev_status and st.get("status") != "running":
                # The transition out of `running` is the one moment the
                # supervisor sees a session end. Tidy BEFORE the emit so the
                # client's refetch already carries the transcript's verdict.
                self._tidy_worktree(h)
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
            proc = subprocess.Popen(cmd, cwd=d["cwd"], env=settings.strip_env(),
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
        # Same cleanup as the detached path — placement happens in launch()
        # for every session, so the legacy --print fallback leaves worktrees
        # behind too. Its transcript lives in memory, not output.log, so the
        # note goes through _append rather than _tidy_worktree.
        if d.get("worktree") and d.get("live_root") and d.get("branch"):
            try:
                removed, detail = worktree.tidy(
                    d["live_root"], d["worktree"], d["branch"])
            except Exception as e:  # noqa: BLE001 — never fail a finish
                removed, detail = False, f"tidy failed: {e}"
            self._append(s, f"\n[vira] worktree: {detail}\n" if removed else
                         f"\n[vira] worktree kept at {d['worktree']} — "
                         f"{detail}\n")
        self._emit("status", d["id"], status=d["status"])


sessions = Sessions()
