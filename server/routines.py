"""Routines — Vira's standing agent loops.

The "different types of loops" engine: a routine is a recurring agent
dispatch — a prompt (or a whole circuit) on a cadence. Kinds:

  muse    — the idea generator. Composed server-side at dispatch: reads
            the current backlog + radar, searches the vault, and PROPOSES
            new ideas via the propose_idea native tool. Proposals land in
            the Ideas queue as status "proposed" — nothing runs without
            the owner's approval. The permissioned-autonomy loop.
  watch   — a watcher: run a prompt, notify when something needs eyes.
  digest  — a synthesizer: periodic summary/analysis runs.
  circuit — dispatch a whole circuits.py pipeline on a cadence.
  custom  — any prompt, any cadence.

Cadence: every_hours (float) OR daily_at "HH:MM" (local). The Scheduler
thread (60s tick) dispatches due routines as normal durable jobs / circuit
runs — terminals, ledger rows, restart survival all apply — records
last_run/last_job/last_status, skips while the previous run is still
live, and iMessages the owner on completion when notify is set.

Store: data/routines.json (atomic writes; server-only writer).
"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "routines.json"

KINDS = ("muse", "watch", "digest", "circuit", "custom")
TICK_S = 60

_lock = threading.Lock()

SEEDS = [
    {
        "id": "muse",
        "name": "Muse — propose ideas",
        "kind": "muse",
        "daily_at": "07:30",
        "enabled": True,
        "notify": True,
        "model": "",
        "description": "Every morning Vira studies the backlog, the radar, "
                       "and the vault, then proposes 1-3 new ideas for the "
                       "owner to approve or decline.",
    },
    {
        # id kept from the introductions era: _load() never prunes store
        # rows, so a new id would orphan the live one (see RESEEDS below)
        "id": "intro-scout",
        "name": "Grouping scout — refresh groupings",
        "kind": "custom",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__refresh_groupings__",  # internal dispatch, no session
        "description": "Weekly refresh of the Radar window's groupings — "
                       "who to convene around what, and the conversation "
                       "markers riding on what people just shared.",
    },
    {
        "id": "atlas-refresh",
        "name": "Atlas — rebuild the contact graph",
        "kind": "digest",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "prompt": "__refresh_atlas__",    # internal dispatch, no session
        "description": "Weekly rebuild of the Contact Atlas materialized "
                       "graph (edges, degrees, clusters, edge narration).",
    },
    {
        "id": "system-map",
        "name": "System map — refresh from the change log",
        "kind": "digest",
        "every_hours": 168,
        "enabled": True,
        "notify": False,
        "model": "",
        "cwd": str(ROOT),
        "prompt": "__module_map__",       # composed at dispatch (modulemap)
        "description": "Weekly pass over the Vira change log: bring the "
                       "system-map module registry up to date with what "
                       "actually shipped, via the validated "
                       "update_module_map tool.",
    },
]


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A seeded routine that gets renamed in a release needs its stored row
# brought along — seeds only apply to ids the store has never seen, so an
# instance that already has the row would keep the superseded name
# forever. id -> the exact old name that may be overwritten from SEEDS.
RESEEDS = {"intro-scout": "Intro scout — refresh introductions"}


def _load():
    try:
        s = json.loads(STORE.read_text())
    except (OSError, json.JSONDecodeError):
        s = {}
    if not isinstance(s, dict) or "routines" not in s:
        s = {"routines": []}
    have = {r["id"] for r in s["routines"]}
    changed = False
    for seed in SEEDS:
        if seed["id"] not in have:
            r = dict(seed)
            r.setdefault("created", _now_iso())
            r.setdefault("last_run", None)
            r.setdefault("last_job", None)
            r.setdefault("last_run_id", None)
            r.setdefault("last_status", None)
            s["routines"].append(r)
            changed = True
            continue
        stale = RESEEDS.get(seed["id"])
        if not stale:
            continue
        for r in s["routines"]:
            # only an untouched row is reseeded — a name the owner edited
            # is theirs and stays
            if r["id"] == seed["id"] and r.get("name") == stale:
                r["name"] = seed["name"]
                r["description"] = seed["description"]
                r["prompt"] = seed["prompt"]
                changed = True
    if changed:
        _save(s)
    return s


def _save(s):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_name(STORE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=1, ensure_ascii=False))
    tmp.replace(STORE)


def list_routines():
    with _lock, locked(STORE):
        return _load()["routines"]


def get_routine(rid):
    return next((r for r in list_routines() if r["id"] == rid), None)


def save_routine(data, rid=None):
    """Create (rid=None) or update a routine from UI fields."""
    with _lock, locked(STORE):
        s = _load()
        if rid:
            r = next((r for r in s["routines"] if r["id"] == rid), None)
            if not r:
                raise KeyError(rid)
        else:
            r = {"id": "rt_" + uuid.uuid4().hex[:8], "created": _now_iso(),
                 "last_run": None, "last_job": None, "last_run_id": None,
                 "last_status": None}
            s["routines"].append(r)
        for k in ("name", "kind", "prompt", "circuit_id", "model", "mode",
                  "cwd", "description", "daily_at"):
            if k in data and data[k] is not None:
                r[k] = str(data[k]).strip()
        if "every_hours" in data and data["every_hours"] is not None:
            try:
                r["every_hours"] = max(0.25, float(data["every_hours"]))
                r.pop("daily_at", None)
            except (TypeError, ValueError):
                pass
        if r.get("daily_at"):
            r.pop("every_hours", None)
        for k in ("enabled", "notify"):
            if k in data and data[k] is not None:
                r[k] = bool(data[k])
        if r.get("kind") not in KINDS:
            r["kind"] = "custom"
        if not (r.get("name") or "").strip():
            raise ValueError("a routine needs a name")
        if not r.get("daily_at") and not r.get("every_hours"):
            raise ValueError("a routine needs a cadence "
                             "(every_hours or daily_at)")
        r["updated"] = _now_iso()
        _save(s)
        return r


def delete_routine(rid):
    with _lock, locked(STORE):
        s = _load()
        before = len(s["routines"])
        s["routines"] = [r for r in s["routines"] if r["id"] != rid]
        if len(s["routines"]) == before:
            raise KeyError(rid)
        _save(s)


def _stamp(rid, **fields):
    with _lock, locked(STORE):
        s = _load()
        r = next((r for r in s["routines"] if r["id"] == rid), None)
        if r:
            r.update(fields)
            _save(s)


# ---------- due logic ----------

def is_due(r, now=None):
    if not r.get("enabled"):
        return False
    now = now or datetime.now().astimezone()
    last = r.get("last_run")
    if r.get("daily_at"):
        try:
            hh, mm = str(r["daily_at"]).split(":")
            gate = now.replace(hour=int(hh), minute=int(mm),
                               second=0, microsecond=0)
        except (ValueError, AttributeError):
            return False
        if now < gate:
            return False
        if last:
            try:
                last_dt = datetime.fromisoformat(last).astimezone()
                if last_dt.date() >= now.date():
                    return False
            except ValueError:
                pass
        return True
    hours = float(r.get("every_hours") or 0)
    if hours <= 0:
        return False
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last).astimezone()
    except ValueError:
        return True
    return (now - last_dt).total_seconds() >= hours * 3600


# ---------- dispatch ----------

def _muse_prompt():
    from . import ideas, settings
    owner = settings.get("owner_name") or "the owner"
    current = [i for i in ideas.list_items()
               if i["status"] in ("open", "on-hold", "proposed")]
    backlog = "\n".join(
        f"- [{i['status']}] ({i.get('project', '?')}) {i['text'][:160]}"
        for i in current[:60]) or "(backlog is empty)"
    return (
        f"You are Vira's MUSE — the idea engine of {owner}'s AI chief of "
        "staff. Your job this morning: propose 1-3 genuinely NEW, "
        "buildable ideas that would make Vira (or another of the owner's "
        "projects) more valuable.\n\n"
        "How to work:\n"
        "1. Study the current backlog below — do NOT duplicate or lightly "
        "rephrase anything on it.\n"
        "2. Use mcp__vira__daily_brief and mcp__vira__vault_search to "
        "ground yourself in what is actually going on — recent themes in "
        f"the vault, who {owner} is talking to, open loops, friction "
        "points.\n"
        "3. For each idea, call mcp__vira__propose_idea with a crisp "
        "one-to-two-sentence text (what + why it matters) and the project "
        "it belongs to. Quality over quantity — one sharp idea beats "
        "three vague ones.\n"
        "4. Finish with a short note on why you chose these.\n\n"
        "Ideas you propose are STAGED for the owner's approval — nothing "
        "builds until approved, so be ambitious but concrete.\n\n"
        f"CURRENT BACKLOG:\n{backlog}")


def dispatch(r):
    """Dispatch one routine now. Returns {job_id} or {run_id}."""
    from . import circuits
    from . import session
    if r["kind"] == "circuit":
        cid = r.get("circuit_id") or ""
        run = circuits.start_run(cid, r.get("prompt") or r["name"],
                                 cwd=r.get("cwd") or None,
                                 notify=bool(r.get("notify")),
                                 source=f"routine:{r['id']}")
        _stamp(r["id"], last_run=_now_iso(), last_run_id=run["id"],
               last_job=None, last_status="running")
        return {"run_id": run["id"]}
    # __refresh_intros__ is the pre-groupings token; a store row minted
    # before the reframe still carries it, so both dispatch the same way
    if (r.get("prompt") or "") in ("__refresh_groupings__",
                                   "__refresh_intros__"):
        from . import radar
        threading.Thread(target=radar.refresh_groupings, daemon=True,
                         name="vira-grouping-scout").start()
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "refresh_groupings"}
    if (r.get("prompt") or "") == "__refresh_atlas__":
        from . import atlas
        atlas.refresh(narrate=True)      # already runs in its own thread
        _stamp(r["id"], last_run=_now_iso(), last_job=None,
               last_run_id=None, last_status="done")
        return {"internal": "refresh_atlas"}
    prompt = _muse_prompt() if r["kind"] == "muse" else (r.get("prompt") or "")
    if prompt == "__module_map__":       # composed fresh per run, like muse
        from . import modulemap
        prompt = modulemap.refresh_prompt()
    if not prompt.strip():
        raise ValueError("routine has no prompt")
    jid = session.sessions.launch(prompt, cwd=r.get("cwd") or None,
                                  model=r.get("model") or None,
                                  mode=r.get("mode") or "interactive",
                                  meta={"routine_id": r["id"],
                                        "kind": r["kind"]})
    _stamp(r["id"], last_run=_now_iso(), last_job=jid,
           last_run_id=None, last_status="running")
    return {"job_id": jid}


def _previous_live(r):
    from . import circuits, joblog, session
    if r.get("last_job"):
        snap = (session.sessions.get(r["last_job"])
                or joblog.get_record(r["last_job"]))
        if snap and snap.get("status") == "running":
            return True
    if r.get("last_run_id"):
        run = circuits.get_run(r["last_run_id"])
        if run and run.get("status") == "running":
            return True
    return False


class Scheduler(threading.Thread):
    """60s tick: dispatch due routines; settle finished ones (stamp
    last_status, fire the completion ping)."""

    def __init__(self):
        super().__init__(daemon=True, name="vira-routines")
        self._stop = threading.Event()

    def run(self):
        time.sleep(10)                   # let the rest of the app boot
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the scheduler never dies
                pass
            self._stop.wait(TICK_S)

    def stop(self):
        self._stop.set()

    def tick(self):
        for r in list_routines():
            try:
                self._settle(r)
                if is_due(r) and not _previous_live(r):
                    dispatch(r)
            except Exception:  # noqa: BLE001 — one routine never kills the rest
                pass

    def _settle(self, r):
        """When a dispatched job/run has finished since we last looked,
        record the outcome and notify once."""
        from . import circuits, joblog, session
        if r.get("last_status") != "running":
            return
        status = None
        detail = ""
        if r.get("last_job"):
            snap = (session.sessions.get(r["last_job"])
                    or joblog.get_record(r["last_job"]) or {})
            status = snap.get("status")
            if r.get("kind") == "muse" and status == "done":
                from . import ideas
                fresh = [i for i in ideas.list_items()
                         if i["status"] == "proposed"]
                detail = f" — {len(fresh)} proposal(s) waiting"
        elif r.get("last_run_id"):
            run = circuits.get_run(r["last_run_id"]) or {}
            status = run.get("status")
        if not status or status == "running":
            return
        _stamp(r["id"], last_status=status)
        if r.get("notify") and not r.get("last_run_id"):
            # circuit runs ping from the driver's finalize; jobs ping here
            try:
                from . import notify
                notify.agent_ping(
                    f"Vira: routine '{r['name']}' {status}{detail}",
                    key=f"routine:{r['id']}:{r.get('last_run')}")
            except Exception:  # noqa: BLE001
                pass


scheduler = Scheduler()
