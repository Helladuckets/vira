"""Circuits — multi-model agent pipelines as executable DAGs.

The breadboard lab's design-to-orchestration compiler, running on Vira's
own session engine. A circuit is a set of STAGES; each stage is one agent
session (model + mode + prompt template); `needs` edges are the wires. A
stage with no needs is an entry point; `{{input}}` substitutes the run's
input and `{{stage.<id>.output}}` threads an upstream stage's final text
into a downstream prompt — the out->in handoff, verbatim from the
breadboard export semantics.

This is how "Fable writes the plan, Sonnet executes it" happens: stage
`plan` runs read-only on `fable`, stage `build` (needs: plan) runs
bypassPermissions on sonnet with the plan wired into its prompt, and stage
`judge` (mode: judge) spawns a FRESH session that grades the build — with
an optional GRADE GATE: verdict below min_grade relaunches the target
stage with the judge's findings appended, up to max_retries times (the
grader-gated loop).

Execution facts:
- Every stage run is a normal detached durable job (session registry) —
  it gets a terminal window, a ledger row, restart survival.
- All run state lives in data/circuit-runs.json (fcntl-locked writes);
  the driver thread is stateless between ticks, so a server restart
  resumes every running circuit exactly where it was.
- The live-session cap applies naturally: a stage that can't launch yet
  (cap reached) just stays ready and is retried next tick.

Stores:
  data/circuits.json      — definitions (seeded with builtin templates)
  data/circuit-runs.json  — runs; stages_def frozen per run at start
"""
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import jobfiles, joblog, judge, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
DEFS = ROOT / "data" / "circuits.json"
RUNS = ROOT / "data" / "circuit-runs.json"

TICK_S = 2.0
OUTPUT_INJECT_CAP = 24_000
RUNS_KEEP = 120
# The session permission ladder (session.MODES — kept literal here because
# `session` is imported lazily throughout this module to dodge a circular
# import) plus "judge", the one stage kind that is a role rather than a
# rung: read-only, grading the stages it names.
MODES = ("manual", "acceptEdits", "bypassPermissions", "judge",
         "approval", "logic", "output", "native")
LOCAL_MODES = ("approval", "logic", "output", "native")

# Stage definitions are DATA, stored in data/circuits.json and in owner-saved
# retunes, so they hold whatever rung name was current when they were saved.
# Normalizing on read is what lets the 2026-07-29 rename land without
# migrating that store — the same reason session.norm_mode exists.
_LEGACY_STAGE_MODES = {"interactive": "manual", "acceptedits": "acceptEdits",
                       "autopilot": "bypassPermissions"}


def norm_stage_mode(m, default="manual"):
    """Canonical stage mode, accepting retired rung spellings. "judge" is
    passed through untouched — it is a role, not a rung."""
    s = str(m or "").strip()
    if not s:
        return default
    if s in MODES:
        return s
    return _LEGACY_STAGE_MODES.get(s.lower(), s)


EXTRA_CAP = 4_000        # per-stage owner instructions (tray) length cap
MAX_RETRIES = 5          # ceiling a tray-set grade gate may ask for

# What an Output part can be. "plan" is the odd one and deliberately so: the
# other four only SHAPE the text a stage already produced, while a plan is
# also a thing Vira makes — a vault note plus the rendered HTML dossier. It
# lives here rather than as a permission mode because a plan is a shape, not
# a rung: see plans.SHAPE and _launch_stage below.
OUTPUT_DESTINATIONS = ("record", "decision_brief", "artifact", "notification",
                       "plan")
OUTPUT_LABELS = {
    "record": "a durable Vira record",
    "decision_brief": ("a decision brief with the answer first, evidence, "
                       "tradeoffs, and recommendation"),
    "artifact": "a finished artifact ready to use",
    "notification": ("a concise notification with the decision or action up "
                     "front"),
    "plan": "a plan",
}

_dlock = threading.Lock()
_rlock = threading.Lock()

# ---------- builtin templates ----------

TEMPLATES = [
    {
        "id": "plan",
        "name": "Plan it",
        "description": "One agent studies the ask and writes the plan; Vira "
                       "saves it to your vault as an editable note and "
                       "renders the HTML dossier with diagrams. Nothing is "
                       "built — the plan is the deliverable.",
        "builtin": True,
        "stages": [
            {"id": "plan", "name": "Plan", "model": "", "mode": "manual",
             "read_only": True, "needs": [],
             "prompt": "Write the plan for the following. Study the working "
                       "directory and anything else you need first, so the "
                       "plan is grounded in what is actually there rather "
                       "than in assumptions.\n\n{{input}}\n\nName exact "
                       "files, the change in each, the order of work, how to "
                       "verify it, and what NOT to touch. Say what you are "
                       "unsure of rather than papering over it."},
            {"id": "dossier", "name": "Plan dossier", "mode": "output",
             "needs": ["plan"], "output": {"destination": "plan"}},
        ],
    },
    {
        "id": "plan-build-judge",
        "name": "Plan, build, judge",
        "description": "Fable 5 writes the implementation plan (read-only), "
                       "Sonnet builds it (bypass permissions), and a fresh judge "
                       "grades the result — below a B, the build re-runs "
                       "once with the judge's findings.",
        "builtin": True,
        "stages": [
            {"id": "plan", "name": "Plan (Fable)", "model": "fable",
             "mode": "manual", "read_only": True, "needs": [],
             "prompt": "You are the PLANNING stage of a pipeline. Another "
                       "agent will implement your plan without talking to "
                       "you, so it must stand alone.\n\nWrite a concrete, "
                       "step-by-step implementation plan for:\n\n{{input}}"
                       "\n\nExplore the code read-only as needed. The plan "
                       "must name exact files, the changes in each, the "
                       "order of work, how to verify, and what NOT to touch."
                       " Do not modify anything. Output only the plan."},
            {"id": "build", "name": "Build (Sonnet)", "model": "sonnet",
             "mode": "bypassPermissions", "needs": ["plan"],
             "prompt": "You are the BUILD stage of a pipeline. Implement "
                       "the plan below completely. Run tests where they "
                       "exist. Do NOT commit or push — changes stay in the "
                       "working tree. Original ask: {{input}}\n\n"
                       "THE PLAN:\n{{stage.plan.output}}\n\nFinish with a "
                       "clear report of what you changed and how you "
                       "verified it."},
            {"id": "dossier", "name": "Plan dossier", "mode": "output",
             "needs": ["plan"], "output": {"destination": "plan"}},
            {"id": "judge", "name": "Judge (fresh eyes)", "model": "",
             "mode": "judge", "needs": ["build"],
             "judge": {"of": ["build"], "retry_stage": "build",
                       "min_grade": "B", "max_retries": 1}},
        ],
    },
    {
        "id": "watch-build",
        "name": "Watch, then build",
        "description": "Vira watches an explainer video with the watch "
                       "toolkit — timestamped frames plus transcript — and "
                       "writes the definitive breakdown of what it "
                       "proposes; Fable 5 turns that into a standalone "
                       "implementation plan for the target repo, Sonnet "
                       "builds it (bypass permissions), and a fresh judge grades "
                       "the result — below a B, the build re-runs once "
                       "with the judge's findings.",
        "builtin": True,
        "stages": [
            {"id": "watch", "name": "Watch (Sonnet)", "model": "sonnet",
             "mode": "bypassPermissions", "needs": [],
             "prompt": "You are the WATCH stage of a pipeline. The input "
                       "below holds a video URL (and possibly extra notes "
                       "from the owner). Watch the video for real and "
                       "write the definitive breakdown of what it "
                       "proposes — downstream stages never see the video, "
                       "only your text, so it must stand alone.\n\n"
                       "{{input}}\n\n"
                       "How to watch: run the watch toolkit —\n"
                       "python3 ~/.claude/skills/watch/scripts/watch.py "
                       "\"<url>\"\n"
                       "It downloads the video, extracts timestamped "
                       "frames, and pulls the transcript (captions first, "
                       "Whisper fallback). Read every frame path it "
                       "prints — they render as images — alongside the "
                       "transcript. For videos over ~10 minutes do a "
                       "sparse full pass first, then re-run focused "
                       "(--start/--end, --resolution 1024) on the "
                       "sections that show code, commands, architecture "
                       "diagrams, or UI that must be read exactly. If the "
                       "toolkit is missing, fall back to yt-dlp + ffmpeg "
                       "directly.\n\n"
                       "Then write the breakdown:\n"
                       "1. THE PITCH — what is proposed or demonstrated, "
                       "in two sentences.\n"
                       "2. HOW IT WORKS — the architecture as presented: "
                       "components, data flow, every tool, service, "
                       "model, or library named, with timestamps.\n"
                       "3. THE BUILD RECIPE — every concrete step, "
                       "command, code snippet, config, or prompt shown "
                       "on screen or spoken, in order, transcribed "
                       "exactly.\n"
                       "4. GAPS — what the video hand-waves, skips, or "
                       "gets wrong; the decisions an implementer must "
                       "make.\n"
                       "5. VERDICT — worth building as shown, and what "
                       "to change.\n\n"
                       "Keep the breakdown under 20,000 characters. "
                       "Delete the working directory when done. Output "
                       "only the breakdown."},
            {"id": "plan", "name": "Plan (Fable)", "model": "fable",
             "mode": "manual", "read_only": True, "needs": ["watch"],
             "prompt": "You are the PLANNING stage of a pipeline. An "
                       "agent watched a video and wrote the breakdown "
                       "below. Turn it into a concrete, standalone "
                       "implementation plan for the working directory "
                       "you are in — another agent will implement it "
                       "without seeing the video or talking to you.\n\n"
                       "THE ASK:\n{{input}}\n\n"
                       "THE VIDEO BREAKDOWN:\n{{stage.watch.output}}\n\n"
                       "Explore the working directory read-only. Decide "
                       "what to adopt as shown and what to adapt to this "
                       "machine and codebase — name each deviation and "
                       "why. The plan must name exact files, the changes "
                       "in each, dependencies to install, the order of "
                       "work, how to verify, and what NOT to touch. Do "
                       "not modify anything. Output only the plan."},
            {"id": "build", "name": "Build (Sonnet)", "model": "sonnet",
             "mode": "bypassPermissions", "needs": ["plan"],
             "prompt": "You are the BUILD stage of a pipeline. Implement "
                       "the plan below completely. Run tests where they "
                       "exist. Do NOT commit or push — changes stay in "
                       "the working tree. Original ask: {{input}}\n\n"
                       "THE PLAN:\n{{stage.plan.output}}\n\nFinish with "
                       "a clear report of what you changed and how you "
                       "verified it."},
            {"id": "dossier", "name": "Plan dossier", "mode": "output",
             "needs": ["plan"], "output": {"destination": "plan"}},
            {"id": "judge", "name": "Judge (fresh eyes)", "model": "",
             "mode": "judge", "needs": ["build"],
             "judge": {"of": ["build"], "retry_stage": "build",
                       "min_grade": "B", "max_retries": 1}},
        ],
    },
    {
        "id": "council",
        "name": "The Council",
        "description": "One question, three independent minds — Sonnet, "
                       "Opus, and Haiku answer in parallel with no "
                       "knowledge of each other; Fable 5 synthesizes where "
                       "they agree, where they split, and what to trust.",
        "builtin": True,
        "stages": [
            {"id": "sonnet", "name": "Sonnet's take", "model": "sonnet",
             "mode": "manual", "read_only": True, "needs": [],
             "prompt": "Answer this question thoroughly and directly, on "
                       "your own judgment:\n\n{{input}}"},
            {"id": "opus", "name": "Opus's take", "model": "opus",
             "mode": "manual", "read_only": True, "needs": [],
             "prompt": "Answer this question thoroughly and directly, on "
                       "your own judgment:\n\n{{input}}"},
            {"id": "haiku", "name": "Haiku's take", "model": "haiku",
             "mode": "manual", "read_only": True, "needs": [],
             "prompt": "Answer this question thoroughly and directly, on "
                       "your own judgment:\n\n{{input}}"},
            {"id": "synth", "name": "Synthesis (Fable)", "model": "fable",
             "mode": "manual", "read_only": True,
             "needs": ["sonnet", "opus", "haiku"],
             "prompt": "Three independent advisors answered the same "
                       "question without seeing each other's work. "
                       "Synthesize a final answer: where they agree "
                       "(high confidence), where they disagree (name the "
                       "disagreement and adjudicate it), and anything "
                       "exactly one of them caught.\n\nTHE QUESTION:\n"
                       "{{input}}\n\nADVISOR 1 (Sonnet):\n"
                       "{{stage.sonnet.output}}\n\nADVISOR 2 (Opus):\n"
                       "{{stage.opus.output}}\n\nADVISOR 3 (Haiku):\n"
                       "{{stage.haiku.output}}"},
        ],
    },
    {
        "id": "research-brief",
        "name": "Research, then brief",
        "description": "Sonnet researches across Vira's data plane — the "
                       "vault, CRM, mail, calendar — read-only; Fable 5 "
                       "turns the findings into a tight decision brief.",
        "builtin": True,
        "stages": [
            {"id": "research", "name": "Research (Sonnet)",
             "model": "sonnet", "mode": "manual", "read_only": True,
             "needs": [],
             "prompt": "You are the RESEARCH stage. Investigate this "
                       "question using the mcp__vira__* native tools — "
                       "vault_search / vault_note (the owner's knowledge "
                       "vault), crm_lookup, mail_search, calendar, "
                       "daily_brief — plus web search when useful:\n\n"
                       "{{input}}\n\nReturn organized findings with "
                       "sources named, contradictions surfaced, and open "
                       "questions listed. Findings only — no "
                       "recommendations yet."},
            {"id": "brief", "name": "Brief (Fable)", "model": "fable",
             "mode": "manual", "read_only": True,
             "needs": ["research"],
             "prompt": "Turn these research findings into a decision "
                       "brief for the owner: the answer up front, the "
                       "evidence, the tradeoffs, and a recommendation. "
                       "Tight and frank.\n\nTHE QUESTION:\n{{input}}\n\n"
                       "FINDINGS:\n{{stage.research.output}}"},
        ],
    },
]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- definitions store ----------

def _load_defs():
    try:
        s = json.loads(DEFS.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict) or "circuits" not in s:
        s = {"circuits": []}
    have = {c["id"] for c in s["circuits"]}
    changed = False
    for t in TEMPLATES:
        if t["id"] not in have:
            s["circuits"].append(json.loads(json.dumps(t)))
            changed = True
    changed = _seed_plan_outputs(s) or changed
    if changed:
        _save_defs(s)
    return s


def _seed_plan_outputs(s):
    """ONE-TIME: give the shipped planning starters their Plan-dossier
    output part.

    Seeding is by id, so a starter already on disk never picks up a stage
    added to its template later — which would have left every existing
    install with planning workflows that could not produce the dossier
    the feature exists for. This reconciles them once and records that it
    ran, so a part the owner then DELETES stays deleted: an additive
    migration that re-ran on every load would be a stage he cannot get
    rid of.
    """
    if s.get("plan_outputs_seeded"):
        return False
    s["plan_outputs_seeded"] = True
    by_id = {c["id"]: c for c in s["circuits"]}
    for t in TEMPLATES:
        extra = [st for st in t.get("stages") or []
                 if st.get("id") == "dossier"]
        rec = by_id.get(t["id"])
        if not extra or not rec or not rec.get("builtin"):
            continue
        ids = {st.get("id") for st in rec.get("stages") or []}
        if "dossier" in ids or not {"plan"} <= ids:
            continue
        rec["stages"].append(json.loads(json.dumps(extra[0])))
    return True


def _save_defs(s):
    DEFS.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEFS.with_name(DEFS.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(DEFS)


def list_circuits():
    with _dlock, locked(DEFS):
        return _load_defs()["circuits"]


def get_circuit(cid):
    return next((c for c in list_circuits() if c["id"] == cid), None)


def validate_stages(stages):
    """Shape + DAG checks; raises ValueError. Returns topo order ids."""
    if not stages:
        raise ValueError("a circuit needs at least one stage")
    ids = [st.get("id") for st in stages]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError("stage ids must be unique and non-empty")
    known = set(ids)
    for st in stages:
        if norm_stage_mode(st.get("mode")) not in MODES:
            raise ValueError(f"stage {st['id']}: bad mode")
        for n in st.get("needs") or []:
            if n not in known:
                raise ValueError(f"stage {st['id']}: unknown need {n!r}")
        if norm_stage_mode(st.get("mode")) == "judge":
            j = st.get("judge") or {}
            for ref in list(j.get("of") or []) + (
                    [j["retry_stage"]] if j.get("retry_stage") else []):
                if ref not in known:
                    raise ValueError(
                        f"judge stage {st['id']}: unknown stage {ref!r}")
        elif norm_stage_mode(st.get("mode")) not in LOCAL_MODES and not (
                st.get("prompt") or "").strip():
            raise ValueError(f"stage {st['id']}: empty prompt")
    return topo_order(stages)


def topo_order(stages):
    """Kahn's — raises ValueError on a cycle."""
    needs = {st["id"]: set(st.get("needs") or []) for st in stages}
    order = []
    ready = sorted(sid for sid, n in needs.items() if not n)
    needs = {sid: n for sid, n in needs.items() if n}
    while ready:
        sid = ready.pop(0)
        order.append(sid)
        for other, n in list(needs.items()):
            n.discard(sid)
            if not n:
                del needs[other]
                ready.append(other)
        ready.sort()
    if needs:
        raise ValueError("circuit has a cycle: " + ", ".join(sorted(needs)))
    return order


def apply_overrides(stages, overrides):
    """Merge the Run tray's per-stage edits into `stages`, in place.

    A circuit is a template, not a contract: the model a step runs on and
    the instructions it carries are exactly the knobs an owner wants to
    turn for ONE run — "same pipeline, but build on Opus, and stay off the
    migrations". So the tray's edits ride the run request and land on the
    run's frozen stages_def; the definition is only touched when they are
    explicitly saved (update_stages).

    Deliberately narrow: a run may retune a stage, never rewire the
    circuit. Ids, needs and judge targets are the graph and stay put —
    everything the driver relies on to be a DAG. Raises ValueError on an
    unknown stage or an uneditable field, so a typo fails the run rather
    than silently running the unedited pipeline."""
    if not overrides:
        return stages
    by_id = {st["id"]: st for st in stages}
    for sid, upd in overrides.items():
        st = by_id.get(sid)
        if st is None:
            raise ValueError(f"unknown stage {sid!r}")
        if not isinstance(upd, dict):
            raise ValueError(f"stage {sid}: overrides must be an object")
        is_judge = norm_stage_mode(st.get("mode")) == "judge"
        is_local = norm_stage_mode(st.get("mode")) in LOCAL_MODES
        if is_local and upd:
            raise ValueError(f"stage {sid}: local graph parts are edited in the Forge")
        for key, val in upd.items():
            if key in ("min_grade", "max_retries"):
                if not is_judge:
                    raise ValueError(f"stage {sid}: {key} is a judge setting")
                j = dict(st.get("judge") or {})
                if key == "min_grade":
                    grade = str(val or "").strip().upper()
                    if grade and judge.grade_value(grade) is None:
                        raise ValueError(f"stage {sid}: unknown grade {val!r}")
                    j["min_grade"] = grade          # "" = run the gate off
                else:
                    j["max_retries"] = max(0, min(int(val or 0), MAX_RETRIES))
                st["judge"] = j
            elif key == "extra":
                st["extra"] = str(val or "").strip()[:EXTRA_CAP]
            elif key == "read_only":
                if is_judge:                        # judges are read-only, full stop
                    raise ValueError(f"stage {sid}: a judge is always read-only")
                st["read_only"] = bool(val)
            elif key == "mode":
                mode = str(val or "").strip()
                if is_judge or mode == "judge" or mode in LOCAL_MODES:
                    raise ValueError(f"stage {sid}: a stage cannot change "
                                     f"into or out of being a judge")
                if mode:
                    st["mode"] = mode               # validate_stages checks it
            elif key == "model":
                st["model"] = str(val or "").strip()
            else:
                raise ValueError(f"stage {sid}: {key!r} is not editable")
    return stages


def save_circuit(circ):
    """Create or update a definition (builtins can be updated too — they
    reseed only when absent)."""
    stages = circ.get("stages") or []
    validate_stages(stages)
    cid = (circ.get("id") or "").strip() or "cir_" + uuid.uuid4().hex[:8]
    with _dlock, locked(DEFS):
        s = _load_defs()
        existing = next((c for c in s["circuits"] if c["id"] == cid), None)
        rec = {
            "id": cid, "name": (circ.get("name") or cid).strip(),
            "description": (circ.get("description") or "").strip(),
            "builtin": bool(existing and existing.get("builtin")),
            "stages": stages,
            "created": existing.get("created") if existing else _now(),
            "updated": _now(),
        }
        s["circuits"] = [c for c in s["circuits"] if c["id"] != cid]
        s["circuits"].append(rec)
        _save_defs(s)
    return rec


def update_stages(cid, overrides):
    """Bake tray edits into the definition — the tray's "save as default".
    Same merge a run uses, so what gets saved is exactly what was running."""
    circ = get_circuit(cid)
    if not circ:
        raise KeyError(cid)
    stages = apply_overrides(json.loads(json.dumps(circ["stages"])), overrides)
    return save_circuit({**circ, "stages": stages})


def delete_circuit(cid):
    with _dlock, locked(DEFS):
        s = _load_defs()
        before = len(s["circuits"])
        s["circuits"] = [c for c in s["circuits"] if c["id"] != cid]
        if len(s["circuits"]) == before:
            raise KeyError(cid)
        _save_defs(s)


# ---------- runs store ----------

def _load_runs():
    try:
        s = json.loads(RUNS.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict) or "runs" not in s:
        s = {"runs": []}
    return s


def _save_runs(s):
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNS.with_name(RUNS.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(RUNS)


def _mutate_runs(fn):
    with _rlock, locked(RUNS):
        s = _load_runs()
        if fn(s):
            _save_runs(s)


def list_runs(limit=40):
    with _rlock, locked(RUNS):
        return list(reversed(_load_runs()["runs"]))[:max(1, min(limit, 200))]


def get_run(run_id):
    with _rlock, locked(RUNS):
        return next((r for r in _load_runs()["runs"]
                     if r["id"] == run_id), None)


def start_run(cid, input_text, cwd=None, notify=False, source="manual",
              idea_id=None, overrides=None, flow_options=None):
    circ = get_circuit(cid)
    if not circ:
        raise KeyError(cid)
    # The run gets its OWN copy of the stages, tray edits merged in and
    # then validated — so a bad override fails here, before any stage
    # launches, and the definition on disk is untouched either way.
    stages = apply_overrides(json.loads(json.dumps(circ["stages"])), overrides)
    flow_options = dict(flow_options or {})
    destination = str(flow_options.get("output") or "").strip()
    if destination:
        if destination not in OUTPUT_DESTINATIONS:
            raise ValueError("unknown Flow output destination")
        for stage in stages:
            if norm_stage_mode(stage.get("mode")) == "output":
                stage["output"] = {**(stage.get("output") or {}),
                                   "destination": destination}
    validate_stages(stages)
    input_text = (input_text or "").strip()
    if not input_text:
        raise ValueError("a run needs an input")
    run = {
        "id": "run_" + uuid.uuid4().hex[:10],
        "circuit_id": cid, "circuit_name": circ["name"],
        "input": input_text, "cwd": cwd or None, "idea_id": idea_id,
        "status": "running", "source": source, "notify": bool(notify),
        "launch_options": flow_options,
        "started": _now(), "finished": None, "error": "",
        "stages_def": stages,
        "stages": {st["id"]: {"status": "pending", "job_id": None,
                              "attempts": 0, "grade": None, "score": None,
                              "verdict": None, "feedback": "",
                              "result_text": "", "decision": None}
                   for st in stages},
    }

    def fn(s):
        s["runs"].append(run)
        if len(s["runs"]) > RUNS_KEEP:
            done = [r for r in s["runs"] if r["status"] != "running"]
            for r in done[:len(s["runs"]) - RUNS_KEEP]:
                s["runs"].remove(r)
        return True
    _mutate_runs(fn)
    return run


def cancel_run(run_id):
    from . import session
    run = get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] != "running":
        raise ValueError("run already finished")
    for st in run["stages"].values():
        if st["status"] == "running" and st["job_id"]:
            try:
                session.sessions.close(st["job_id"])
            except (KeyError, ValueError):
                pass

    def fn(s):
        r = next((r for r in s["runs"] if r["id"] == run_id), None)
        if r and r["status"] == "running":
            r["status"] = "canceled"
            r["finished"] = _now()
            for st in r["stages"].values():
                if st["status"] in ("pending", "ready", "waiting"):
                    st["status"] = "skipped"
            return True
        return False
    _mutate_runs(fn)
    return get_run(run_id)


def decide_approval(run_id, stage_id, approved, note=""):
    """Resolve one waiting Approval part; the driver resumes on its next tick."""
    run = get_run(run_id)
    if not run:
        raise KeyError(run_id)
    stage_def = next((stage for stage in run.get("stages_def") or []
                      if stage.get("id") == stage_id), None)
    if not stage_def or norm_stage_mode(stage_def.get("mode")) != "approval":
        raise KeyError(stage_id)
    state = (run.get("stages") or {}).get(stage_id) or {}
    if state.get("status") != "waiting":
        raise ValueError("approval is not waiting for a decision")
    note = str(note or "").strip()[:4000]
    result = "Approved" if approved else "Declined"
    if note:
        result += f": {note}"

    def fn(store):
        row = next((item for item in store["runs"] if item["id"] == run_id), None)
        if not row:
            return False
        stage = row["stages"].get(stage_id)
        if not stage or stage.get("status") != "waiting":
            return False
        stage.update({"status": "done" if approved else "error",
                      "decision": {"approved": bool(approved), "note": note,
                                   "decided": _now()},
                      "result_text": result})
        return True
    _mutate_runs(fn)
    decided = get_run(run_id)
    if ((decided.get("stages") or {}).get(stage_id) or {}).get("status") == "waiting":
        raise ValueError("approval changed before the decision was recorded")
    return decided


# ---------- prompt wiring ----------

def _stage_output(run, sid):
    """Final text of a finished stage: state.json result_text first (rich),
    ledger result as fallback."""
    st = run["stages"].get(sid) or {}
    if st.get("result_text"):
        return str(st["result_text"])[:OUTPUT_INJECT_CAP]
    jid = st.get("job_id")
    if not jid:
        return ""
    state = jobfiles.read_json(jobfiles.job_dir(jid) / "state.json") or {}
    out = state.get("result_text") or ""
    if not out:
        rec = joblog.get_record(jid) or {}
        out = rec.get("result") or ""
    return out[:OUTPUT_INJECT_CAP]


def _destination(item, run=None):
    """The destination an attached Output part resolves to for this run — the
    launchbar's choice overrides what the part was saved with."""
    selected = ((run or {}).get("launch_options") or {}).get("output")
    return str(selected or item.get("destination") or "record")


def attached_outputs(st_def, run=None):
    """Every Output part this stage feeds.

    Two shapes reach here and both are real: the Forge COMPILES a graph's
    downstream output nodes onto the stage as `forge.outputs`, while a
    builtin starter is a plain stage list where the output part is just
    another stage that `needs` this one. Reading only the first would make
    the starters unable to end in anything.
    """
    items = list((st_def.get("forge") or {}).get("outputs") or [])
    seen = {(_destination(i, run), str(i.get("instructions") or ""))
            for i in items}
    sid = st_def.get("id")
    stages = (run or {}).get("stages_def") or []
    has_output_part = False
    feeds_this = False
    terminal = bool(sid)
    for other in stages:
        other_mode = norm_stage_mode(other.get("mode"))
        if other_mode == "output":
            has_output_part = True
        if sid and sid in (other.get("needs") or []):
            terminal = False
            if other_mode == "output":
                feeds_this = True
                cfg = dict(other.get("output") or {})
                key = (_destination(cfg, run),
                       str(cfg.get("instructions") or ""))
                if key not in seen:
                    seen.add(key)
                    items.append({"name": other.get("name") or "Output",
                                  **cfg})
    if items or feeds_this:
        return items
    # The launchbar's own Output choice, on a Flow that carries no Output
    # part. Without this, picking "Plan dossier" there would light up a
    # control that does nothing — the dead-affordance failure — because
    # the destination only ever reached stages an Output part fed. It
    # applies to the Flow's LAST stages only, and never to a judge: a
    # verdict is not a plan.
    if (_destination({}, run) == "plan" and not has_output_part and terminal
            and norm_stage_mode(st_def.get("mode")) not in LOCAL_MODES + ("judge",)):
        return [{"name": "Plan dossier", "destination": "plan"}]
    return items


def writes_a_plan(st_def, run=None):
    """True when this stage's output lands as a plan: an Output part set to
    the plan destination, or the legacy per-stage publish_plan flag.

    This is the whole of what "plan" means to a Flow now. It says nothing
    about how the stage is gated — a planning stage that may read the web,
    spawn a subagent, or even write a scratch file still produces the same
    dossier, because the dossier comes from the SHAPE of what it wrote.
    """
    if st_def.get("publish_plan"):
        return True
    return any(_destination(item, run) == "plan"
               for item in attached_outputs(st_def, run))


def _forge_brief(stage, run=None):
    """Render graph attachments once, at the stage boundary they feed."""
    forge = stage.get("forge") or {}
    blocks = []
    contexts = forge.get("contexts") or []
    if contexts:
        lines = ["CONNECTED CONTEXT"]
        for item in contexts:
            name = item.get("name") or "Context"
            ref = item.get("ref") or ""
            note = item.get("note") or ""
            lines.append(f"- {name}" + (f" [{ref}]" if ref else ""))
            if note:
                lines.append(f"  {note}")
        blocks.append("\n".join(lines))
    tools = forge.get("tools") or []
    if tools:
        lines = ["CONNECTED CAPABILITIES"]
        for item in tools:
            name = item.get("name") or "Capability"
            source = item.get("source_ref") or item.get("source") or ""
            lines.append(f"- {name}" + (f" — {source}" if source else ""))
            if item.get("instructions"):
                lines.append(f"  {item['instructions']}")
        blocks.append("\n".join(lines))
    wire_notes = [str(value).strip() for value in forge.get("wire_instructions") or []
                  if str(value).strip()]
    if wire_notes:
        blocks.append("CONNECTION INSTRUCTIONS\n- " + "\n- ".join(wire_notes))
    outputs = attached_outputs(stage, run)
    if outputs:
        lines = ["OUTPUT CONTRACT"]
        plan = False
        for item in outputs:
            destination = _destination(item, run)
            plan = plan or destination == "plan"
            lines.append("- Shape your final response as "
                         f"{OUTPUT_LABELS.get(destination, destination)}.")
            if item.get("instructions"):
                lines.append(f"  {item['instructions']}")
        if plan:
            # The plan destination is the only one that has to be exact: a
            # renderer reads this structure. plans.SHAPE is that contract.
            from . import plans
            lines.append("")
            lines.append(plans.SHAPE)
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return ("\n\nFORGE GRAPH ATTACHMENTS\n"
            "These are explicit inputs and capabilities connected to this "
            "stage. Use them here; do not silently pass them to unrelated "
            "stages.\n" + "\n\n".join(blocks))


def render_prompt(template, run, stage=None):
    out = template.replace("{{input}}", run["input"])

    def sub(m):
        return _stage_output(run, m.group(1))
    out = re.sub(r"\{\{stage\.([\w-]+)\.output\}\}", sub, out)
    return out + (_forge_brief(stage or {}, run))


# ---------- surfaced run result ----------

RESULT_TEXT_CAP = 12_000


def _built_path(run):
    """The working directory a build run wrote into — a run with a cwd and
    at least one write-capable (non-judge, non-read_only) stage. Pure
    advisory runs (all read_only, e.g. Council) build nothing, so None."""
    cwd = run.get("cwd")
    if not cwd:
        return None
    for st in run.get("stages_def") or []:
        if norm_stage_mode(st.get("mode")) in ("judge",) + LOCAL_MODES:
            continue
        if not st.get("read_only"):
            return cwd
    return None


def run_result(run):
    """The final result to show on a finished run row: the last non-judge
    stage's report text plus any built path. None while the run is still
    running or when there is nothing to surface. Judge verdicts are already
    rendered separately, so the report is the actual work product (the
    build's report, the synthesis, the brief)."""
    if not run or run.get("status") == "running":
        return None
    defs = {st["id"]: st for st in (run.get("stages_def") or [])}
    try:
        order = topo_order(run["stages_def"])
    except (ValueError, KeyError):
        order = list(defs)
    report = None
    for sid in reversed(order):
        d = defs.get(sid, {})
        if d.get("mode") == "judge":
            continue
        stt = (run.get("stages") or {}).get(sid, {})
        if stt.get("status") != "done":
            continue
        txt = _stage_output(run, sid).strip()
        if txt:
            report = {"stage": sid, "name": d.get("name") or sid,
                      "text": txt[:RESULT_TEXT_CAP],
                      "job_id": stt.get("job_id"),
                      "truncated": len(txt) > RESULT_TEXT_CAP}
            break
    built = _built_path(run)
    if not report and not built:
        return None
    return {"report": report, "built_path": built}


# ---------- the driver ----------

class Driver(threading.Thread):
    """Stateless per tick: read running runs from disk, advance each one.
    Restart-safe by construction — stage jobs are detached processes and
    every decision re-derives from the stores."""

    def __init__(self):
        super().__init__(daemon=True, name="vira-circuits")
        self._stop = threading.Event()

    def run(self):
        time.sleep(3)
        while not self._stop.is_set():
            try:
                for run in [r for r in list_runs(200)
                            if r["status"] == "running"]:
                    self._advance(run)
            except Exception:  # noqa: BLE001 — the driver never dies
                pass
            self._stop.wait(TICK_S)

    def stop(self):
        self._stop.set()

    # -- one run, one tick --

    def _advance(self, run):
        from . import session
        changed = {}
        defs = {st["id"]: st for st in run["stages_def"]}
        # 1) refresh running stages from their jobs
        for sid, st in run["stages"].items():
            if st["status"] == "running" and st.get("child_run_id"):
                child = get_run(st["child_run_id"])
                if not child or child.get("status") == "running":
                    continue
                changed[sid] = {
                    "status": "done" if child.get("status") == "done" else "error",
                    "result_text": json.dumps(run_result(child) or {
                        "child_run": child.get("id"), "status": child.get("status")},
                        ensure_ascii=False),
                }
                continue
            if st["status"] != "running" or not st["job_id"]:
                continue
            snap = (session.sessions.get(st["job_id"])
                    or joblog.get_record(st["job_id"]))
            status = (snap or {}).get("status", "running")
            if status == "running":
                continue
            ok = status == "done"
            if defs[sid].get("mode") == "judge":
                self._finish_judge(run, sid, defs[sid], ok, changed)
            else:
                changed[sid] = {"status": "done" if ok else "error"}
        if changed:
            self._apply(run["id"], changed)
            run = get_run(run["id"])
            if run is None or run["status"] != "running":
                return
            changed = {}
        # 2) launch ready stages
        for st_def in run["stages_def"]:
            sid = st_def["id"]
            st = run["stages"][sid]
            if st["status"] != "pending":
                continue
            needs = st_def.get("needs") or []
            if any(run["stages"][n]["status"] != "done" for n in needs):
                if any(run["stages"][n]["status"] in ("error", "skipped")
                       for n in needs):
                    changed[sid] = {"status": "skipped"}
                continue
            mode = norm_stage_mode(st_def.get("mode"))
            if mode in LOCAL_MODES:
                changed[sid] = self._run_local_stage(run, st_def)
                continue
            try:
                jid = self._launch_stage(run, st_def)
            except ValueError:
                continue          # session cap — retry next tick
            except Exception as e:  # noqa: BLE001 — stage launch failed
                changed[sid] = {"status": "error"}
                self._apply(run["id"], changed,
                            error=f"stage {sid} launch failed: {e}")
                return
            changed[sid] = {"status": "running", "job_id": jid,
                            "attempts": st["attempts"] + 1}
        if changed:
            self._apply(run["id"], changed)
            run = get_run(run["id"])
        # 3) finalize
        states = [st["status"] for st in run["stages"].values()]
        if "running" in states or "pending" in states or "waiting" in states:
            return
        final = "done" if all(s == "done" for s in states) else "error"
        self._finalize(run, final)

    def _launch_stage(self, run, st_def):
        from . import session
        sid = st_def["id"]
        # The stage's own instructions from the tray: run-specific steer
        # ("stay off the migrations", "grade the tests hardest") that the
        # template can't know. They go LAST so they win a disagreement.
        extra = (st_def.get("extra") or "").strip()
        if st_def.get("mode") == "judge":
            j = st_def.get("judge") or {}
            of = j.get("of") or (st_def.get("needs") or [])
            evidence = "\n\n".join(
                f"[stage {o} output]\n{_stage_output(run, o)}" for o in of)
            target_cwd = run.get("cwd")
            context = (f"This work was stage(s) {', '.join(of)} of the "
                       f"'{run['circuit_name']}' pipeline.")
            if extra:
                context += ("\n\nThe owner asked you to weigh this in "
                            "particular:\n" + extra)
            prompt = judge.build_prompt(
                run["input"], evidence, cwd=target_cwd, context=context)
            model = st_def.get("model") or judge.judge_model()
            mode, read_only = "manual", True
        else:
            prompt = render_prompt(st_def.get("prompt") or "", run, st_def)
            if extra:
                prompt += ("\n\nADDITIONAL INSTRUCTIONS FROM THE OWNER for "
                           "this run — they take precedence over the brief "
                           "above:\n" + extra)
            fb = run["stages"][sid].get("feedback")
            if fb:
                prompt += ("\n\nA fresh reviewer graded your previous "
                           "attempt below the bar. Address these findings "
                           "specifically:\n" + fb)
            model = st_def.get("model") or None
            mode = norm_stage_mode(st_def.get("mode"))
            read_only = bool(st_def.get("read_only"))
        return session.sessions.launch(
            prompt, cwd=st_def.get("cwd") or run.get("cwd"),
            model=model or None, mode=mode, read_only=read_only,
            publish_plan=writes_a_plan(st_def, run),
            meta={"circuit_run": run["id"], "stage": sid,
                  "circuit": run["circuit_id"]})

    def _run_local_stage(self, run, st_def):
        """Advance a non-model graph part without launching a session."""
        sid = st_def["id"]
        mode = norm_stage_mode(st_def.get("mode"))
        attempts = int(run["stages"][sid].get("attempts") or 0) + 1
        needs = st_def.get("needs") or []
        values = [_stage_output(run, need) for need in needs]
        combined = "\n\n".join(value for value in values if value)
        if mode == "approval":
            request = str((st_def.get("approval") or {}).get("instructions")
                          or st_def.get("name") or "Approval required")
            return {"status": "waiting", "attempts": attempts,
                    "result_text": request}
        if mode == "logic":
            rule = st_def.get("logic") or {}
            operation = str(rule.get("operation") or "always")
            expected = str(rule.get("value") or "")
            haystack = combined.casefold()
            needle = expected.casefold()
            passed = {
                "always": True,
                "contains": needle in haystack,
                "not_contains": needle not in haystack,
                "equals": haystack.strip() == needle.strip(),
                "has_output": bool(combined.strip()),
            }.get(operation, False)
            result = (f"Logic gate {operation}"
                      + (f" {expected!r}" if expected else "")
                      + (": passed" if passed else ": did not pass"))
            return {"status": "done" if passed else "error",
                    "attempts": attempts, "result_text": result}
        if mode == "native":
            from . import routines
            routine_id = str((st_def.get("native") or {}).get("routine_id") or "")
            row = routines.get_routine(routine_id)
            if not row:
                return {"status": "error", "attempts": attempts,
                        "result_text": f"Unknown native system {routine_id}"}
            launched = routines.dispatch(row)
            if launched.get("job_id"):
                return {"status": "running", "attempts": attempts,
                        "job_id": launched["job_id"],
                        "result_text": f"Dispatched native system {routine_id}"}
            if launched.get("run_id"):
                return {"status": "running", "attempts": attempts,
                        "child_run_id": launched["run_id"],
                        "result_text": f"Dispatched nested Flow {launched['run_id']}"}
            return {"status": "done" if not launched.get("error") else "error",
                    "attempts": attempts,
                    "result_text": json.dumps(launched, ensure_ascii=False)}
        output = st_def.get("output") or {}
        destination = output.get("destination") or "record"
        instructions = str(output.get("instructions") or "").strip()
        result = combined or run.get("input") or ""
        if instructions:
            result = f"{result}\n\nOutput instructions: {instructions}".strip()
        return {"status": "done", "attempts": attempts,
                "result_text": result,
                "decision": {"destination": destination}}

    def _finish_judge(self, run, sid, st_def, ok, changed):
        st = run["stages"][sid]
        state = jobfiles.read_json(
            jobfiles.job_dir(st["job_id"]) / "state.json") or {}
        rec = joblog.get_record(st["job_id"]) or {}
        verdict = judge.parse_verdict(
            state.get("result_text") or rec.get("result"))
        j = st_def.get("judge") or {}
        if verdict is None:
            changed[sid] = {"status": "error" if not ok else "done",
                            "grade": "?", "verdict": None}
            return
        upd = {"grade": verdict.get("grade"),
               "score": verdict.get("score"),
               "verdict": {"summary": verdict.get("summary"),
                           "findings": verdict.get("findings"),
                           "recommendation": verdict.get("recommendation")}}
        retry = j.get("retry_stage")
        gate_ok = (not j.get("min_grade")
                   or judge.meets(verdict.get("grade"), j["min_grade"]))
        target = run["stages"].get(retry) if retry else None
        if (not gate_ok and target is not None
                and target["attempts"] <= int(j.get("max_retries") or 0)):
            findings = "\n".join(
                f"- [{f.get('severity', '?')}] {f.get('note', '')}"
                for f in (verdict.get("findings") or []))
            feedback = (f"Grade: {verdict.get('grade')} — "
                        f"{verdict.get('summary', '')}\n{findings}")
            upd["status"] = "pending"       # this judge re-runs afterwards
            changed[sid] = upd
            changed[retry] = {"status": "pending", "job_id": None,
                              "feedback": feedback}
            # downstream of the retried stage (except this judge) re-runs too
            for other in run["stages_def"]:
                if other["id"] in (sid, retry):
                    continue
                if retry in (other.get("needs") or []):
                    changed[other["id"]] = {"status": "pending",
                                            "job_id": None}
            return
        # gate passed, exhausted its retries, or no gate at all -> done
        upd["status"] = "done"
        changed[sid] = upd
        # verdict rides back to the judged jobs' ledger rows (the shared
        # judge epilogue; no idea note here — _finalize owns the close-out)
        for o in (j.get("of") or []):
            ojid = run["stages"].get(o, {}).get("job_id")
            if ojid:
                judge.record_and_close(ojid, verdict,
                                       judge_jid=st["job_id"])

    def _apply(self, run_id, changed, error=None):
        def fn(s):
            r = next((r for r in s["runs"] if r["id"] == run_id), None)
            if not r:
                return False
            for sid, upd in changed.items():
                r["stages"].setdefault(sid, {}).update(upd)
            if error:
                r["error"] = error
                r["status"] = "error"
                r["finished"] = _now()
            return True
        _mutate_runs(fn)

    def _finalize(self, run, final):
        def fn(s):
            r = next((r for r in s["runs"] if r["id"] == run["id"]), None)
            if r and r["status"] == "running":
                r["status"] = final
                r["finished"] = _now()
                return True
            return False
        _mutate_runs(fn)
        grades = [st.get("grade") for st in run["stages"].values()
                  if st.get("grade")]
        if run.get("idea_id"):
            try:
                from . import ideas
                stamp = datetime.now(timezone.utc).date().isoformat()
                g = f", graded {grades[-1]}" if grades else ""
                if final == "done":
                    ideas.stamp_note(run["idea_id"],
                                     f"built by circuit "
                                     f"'{run['circuit_name']}' {stamp}"
                                     f"{g} (run {run['id'][:10]})",
                                     status="done")
                else:
                    ideas.stamp_note(run["idea_id"],
                                     f"circuit run {final} {stamp} "
                                     f"(run {run['id'][:10]}) — see "
                                     f"Circuits window")
            except Exception:  # noqa: BLE001 — closing the loop is best-effort
                pass
        if run.get("notify"):
            tail = f" — graded {', '.join(grades)}" if grades else ""
            try:
                from . import notify
                notify.agent_ping(
                    f"Vira: circuit '{run['circuit_name']}' {final}{tail}",
                    key=f"circuit:{run['id']}")
            except Exception:  # noqa: BLE001 — notification is best-effort
                pass


driver = Driver()
