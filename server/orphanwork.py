"""Orphan-work sweeper — an inventory of work that never landed.

Vira's own branch-first workflow (server/worktree.py, scripts/branch.sh)
puts every dispatched session in its own worktree on its own claude/*
branch. Nothing tore down the ones a session never finished: a stalled
session leaves uncommitted edits sitting in a worktree nobody is looking
at; a session that finished cleanly but was never merged leaves a branch
with commits main never got; `worktree.tidy()` correctly REFUSES to
remove either (see its own docstring — keeping is the safe default), so
neither self-heals. This module is the other half: it finds what tidy
rightly left behind, ages it, and gives the owner one-click Resume / Merge
/ Discard instead of a manual `git worktree list` audit.

Two rungs, the house shape:

  sweep()   — deterministic. Plain git — `git worktree list`, `git branch
              --list "claude/*"`, `git status --porcelain`, `git rev-list`
              — joined against the durable job ledger for the stalled-
              session signal (a ledger row whose branch matches and whose
              status is orphaned/error). No model call, ever.
  refresh() — sweep() -> data/orphan-work.json -> ping the owner on
              genuinely NEW items (the jobboards baseline rule: the first-
              ever sweep never pings, only what appears afterwards does).

Everything here is READ-ONLY except the three explicit actions (resume,
merge, discard), and even those never reimplement branch.sh — they shell
out to it and pass its stderr through verbatim. The sweeper inventories
and pings; nothing merges, discards, or resumes without an owner click.

Store data/orphan-work.json is derived-plus-dismissals, like brief-state
and reconnect.json — NOT in the backup rotation (a lost dismissal is a
row reappearing, not lost work; the actual work lives in the worktrees
and branches themselves, which git already durably holds).

CRITICAL INTEGRATION FACT: the Resume action launches a session with cwd
already pointed at an EXISTING worktree — a leftover, not a fresh
dispatch. session.Sessions.launch() only placed a session into a NEW
worktree; re-entering an existing one left worktree/live_root unset on
the spec, which made runner._disarmed_guard fail-closed refuse to start
the session at all. session.py's branch-first block now forks on
worktree.is_worktree(root) to arm the same guard fields for this path too
— see server/session.py and tests/test_branch_guard_wiring.py's
ReentryIntoExistingWorktree.
"""
import hashlib
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil, jsonstore, joblog, worktree

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "orphan-work.json"

STALE_AFTER_S = 6 * 3600     # a sweep older than this is served stale=true
ACTION_TIMEOUT = 600         # branch.sh merge/discard, same ceiling worktree.py uses
DIRTY_MTIME_CAP = 50         # dirty paths stat'd for the "when was this touched" signal

_actions_lock = threading.Lock()
_actions = {}                 # branch -> {name, status, output, started, finished}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- sweep

def _porcelain_worktrees():
    """[{"path": Path, "branch": "claude/x" or ""}] from `git worktree
    list --porcelain`. A detached-HEAD worktree carries branch=""."""
    out = gitutil.git(ROOT, "worktree", "list", "--porcelain", timeout=20)
    if out.returncode != 0:
        return []
    entries, cur = [], None
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            cur = {"path": Path(line[len("worktree "):].strip()), "branch": ""}
            entries.append(cur)
        elif line.startswith("branch ") and cur is not None:
            ref = line[len("branch "):].strip()
            cur["branch"] = (ref[len("refs/heads/"):]
                             if ref.startswith("refs/heads/") else "")
    return entries


def _dirty_lines(wt):
    """Non-empty `git status --porcelain` lines, or None if the read failed
    — the per-item degrade signal (never raises the whole sweep out)."""
    out = gitutil.git(wt, "status", "--porcelain", timeout=20)
    if out.returncode != 0:
        return None
    return [l for l in (out.stdout or "").splitlines() if l.strip()]


def _ahead_behind(branch):
    """(ahead, behind) of `branch` vs main, or (None, None) on failure.
    `main...branch` left-right count: left = main-only commits (behind),
    right = branch-only commits (ahead) — the same convention update.py
    uses for HEAD...@{upstream}."""
    out = gitutil.git(ROOT, "rev-list", "--left-right", "--count",
                      f"main...{branch}", timeout=20)
    if out.returncode != 0:
        return None, None
    parts = (out.stdout or "").split()
    if len(parts) != 2:
        return None, None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
    return ahead, behind


def _tip_sha(cwd, branch):
    out = gitutil.git(cwd, "rev-parse", branch, timeout=20)
    return (out.stdout or "").strip() if out.returncode == 0 else ""


def _commit_time(branch):
    out = gitutil.git(ROOT, "log", "-1", "--format=%ct", branch, timeout=20)
    try:
        return float((out.stdout or "").strip())
    except (TypeError, ValueError):
        return None


def _dirty_mtime(wt, lines):
    """Newest mtime among up to DIRTY_MTIME_CAP dirty paths, or None. A
    deleted-file status line stats nothing and is skipped, not fatal."""
    best = None
    for line in lines[:DIRTY_MTIME_CAP]:
        rel = line[3:].strip()          # porcelain short format: "XY path"
        if " -> " in rel:               # a rename: "old -> new"
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip('"')
        if not rel:
            continue
        try:
            m = (Path(wt) / rel).stat().st_mtime
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def _job_for_branch(branch, ledger_by_branch):
    row = ledger_by_branch.get(branch)
    if not row:
        return None
    return {"id": row.get("id"), "title": joblog.name(row),
            "status": row.get("status"), "finished": row.get("finished")}


def _make_item(branch, wt, dirty_lines, ledger_by_branch):
    """One inventory row, or None when there is nothing unlanded (clean
    working tree AND fully merged into main — worktree.tidy() owns
    removing that case, the sweeper never does)."""
    ahead, behind = _ahead_behind(branch)
    if ahead is None:
        return None                     # could not read — degrade away
    dirty = len(dirty_lines) if dirty_lines is not None else 0
    if dirty == 0 and ahead == 0:
        return None
    tip = _tip_sha(wt or ROOT, branch)
    ts = _commit_time(branch) or time.time()
    if dirty and wt:
        m = _dirty_mtime(wt, dirty_lines)
        if m:
            ts = max(ts, m)
    job = _job_for_branch(branch, ledger_by_branch)
    if job and job.get("status") == "running":
        # A live session owns this tree: its dirt is work IN PROGRESS, not
        # orphan work, and a row here would carry a Resume button that
        # drops a second bypassPermissions agent into a tree another agent
        # is writing (the judge's high finding). The row appears on the
        # first sweep after the session finishes, errors, or the
        # supervisor reaps it as orphaned. A parked reply-window session
        # keeps status "running" on purpose and is equally excluded — it
        # is the owner's to answer, and the decision layer surfaces it.
        return None
    return {
        "key": f"{branch}:{(tip or '')[:12]}:{dirty}",
        "branch": branch,
        "worktree": str(wt) if wt else "",
        "dirty": dirty, "ahead": ahead, "behind": behind,
        "kind": "dirty" if dirty else "unmerged",
        "instance_running": bool(wt and worktree._instance_alive(wt)),
        "stalled": bool(job and job.get("status") in ("orphaned", "error")),
        "job": job,
        "last_activity": ts,
        "last_activity_iso": datetime.fromtimestamp(
            ts, tz=timezone.utc).isoformat(timespec="seconds"),
        "age_days": round((time.time() - ts) / 86400, 1),
    }


def sweep():
    """Deterministic inventory: every worktree/branch carrying unlanded
    work, plus one row for unpushed commits on main. Read-only git the
    whole way; a failed sub-command degrades that one item away rather
    than raising the sweep out."""
    from . import update
    ledger_by_branch = {}
    for r in joblog.list_records():         # ascending -> last write wins == newest
        b = r.get("branch")
        if b:
            ledger_by_branch[b] = r

    items, seen = [], set()
    for wt_entry in _porcelain_worktrees():
        wt, branch = wt_entry["path"], wt_entry["branch"]
        if not worktree.is_worktree(wt):
            continue                        # the primary checkout itself
        if not branch:
            continue                        # detached HEAD — nothing to act on
        it = _make_item(branch, wt, _dirty_lines(wt), ledger_by_branch)
        if it:
            items.append(it)
        seen.add(branch)

    out = gitutil.git(ROOT, "branch", "--list", "claude/*",
                      "--format=%(refname:short)", timeout=20)
    if out.returncode == 0:
        for branch in (out.stdout or "").splitlines():
            branch = branch.strip()
            if not branch or branch in seen:
                continue
            it = _make_item(branch, None, None, ledger_by_branch)
            if it:
                items.append(it)

    st = update.status(fetch=False)
    if st.get("git") and st.get("remote") and (st.get("ahead") or 0) > 0:
        items.append({
            "key": f"unpushed-main:{st.get('sha') or ''}",
            "branch": "main", "worktree": "", "dirty": 0,
            "ahead": st["ahead"], "behind": st.get("behind", 0),
            "kind": "unpushed", "instance_running": False, "stalled": False,
            "job": None, "last_activity": time.time(),
            "last_activity_iso": _now_iso(), "age_days": 0.0,
        })
    return items


# ---------------------------------------------------------------- store

def _blank():
    return {"last_sweep": None, "items": [], "dismissed": {}, "notified": {},
            "baseline_done": False}


def _read():
    s = jsonstore.read(STORE, _blank())
    if not isinstance(s, dict):
        s = _blank()
    for k, v in _blank().items():
        s.setdefault(k, v)
    return s


def refresh():
    """Regenerate the store from a fresh sweep and ping on genuinely new
    orphans. The FIRST sweep ever (baseline_done False) stamps every key
    into `notified` without pinging — announcing the whole standing
    backlog the first time this ships would bury the one ping that
    matters, the same rule jobboards.py uses for a newly-registered
    board. Safe to call from any thread."""
    items = sweep()
    now = _now_iso()
    keys = {it["key"] for it in items}
    ping = {}

    def fn(s):
        s["items"] = items
        s["last_sweep"] = now
        notified = dict(s.get("notified") or {})
        dismissed = dict(s.get("dismissed") or {})
        new_keys = sorted(k for k in keys if k not in notified)
        if not s.get("baseline_done"):
            for k in keys:
                notified[k] = now
            s["baseline_done"] = True
        elif new_keys:
            actionable = [k for k in new_keys if k not in dismissed]
            if actionable:
                oldest = min((it for it in items if it["key"] in actionable),
                            key=lambda it: it.get("last_activity") or 0.0)
                n = len(actionable)
                ping["text"] = (
                    f"Vira: {n} piece{'s' if n != 1 else ''} of unlanded "
                    f"work — oldest {oldest['branch']}, "
                    f"{oldest.get('age_days', 0):.0f}d old")
                ping["key"] = "orphanwork:" + hashlib.sha1(
                    ",".join(sorted(actionable)).encode()).hexdigest()[:16]
            for k in new_keys:
                notified[k] = now
        jsonstore.prune_oldest(notified, 200)
        jsonstore.prune_oldest(dismissed, 200)
        s["notified"] = notified
        s["dismissed"] = dismissed
        return s

    jsonstore.mutate(STORE, fn, _blank(), indent=1, ensure_ascii=False)
    if ping.get("text"):
        try:
            from . import notify
            notify.agent_ping(ping["text"], key=ping["key"])
        except Exception:  # noqa: BLE001 — a ping must never break the sweep
            pass
    return items


def compose():
    """Every non-dismissed item, stalest-first, with any in-flight action
    state folded in. `unpushed-main` is pinned last — it has no age worth
    sorting on and it is not a worktree to resume."""
    s = _read()
    dismissed = set(s.get("dismissed") or {})
    items = [dict(it) for it in (s.get("items") or [])
             if it.get("key") not in dismissed]
    with _actions_lock:
        for it in items:
            a = _actions.get(it.get("branch") or "")
            if a:
                it["action"] = {"name": a["name"], "status": a["status"],
                                "output": a["output"]}

    def sort_key(it):
        if it.get("kind") == "unpushed":
            return (1, 0.0)
        return (0, it.get("last_activity") or 0.0)
    items.sort(key=sort_key)

    last_sweep = s.get("last_sweep")
    dt = _parse_iso(last_sweep) if last_sweep else None
    stale = dt is None or (
        (datetime.now(timezone.utc) - dt).total_seconds() > STALE_AFTER_S)
    return {"items": items, "last_sweep": last_sweep, "stale": stale}


def dismiss(key, restore=False):
    def fn(s):
        d = dict(s.get("dismissed") or {})
        if restore:
            d.pop(key, None)
        else:
            d[key] = _now_iso()
        s["dismissed"] = d
        return s
    jsonstore.mutate(STORE, fn, _blank(), indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- actions

def _run_action(slug, argv, name, post_ok=None):
    """Run one `scripts/branch.sh <argv>` action for `slug` in a daemon
    thread, guarded so only one action runs per branch at a time. branch.sh
    is the sole authority for preflight/refusals — its combined
    stdout+stderr is kept verbatim, never second-guessed. `post_ok(text)`
    runs after a successful call and may return extra text to append (the
    post-merge push). Returns (started: bool, detail: str) immediately;
    the outcome lands on the item via compose()'s `action` field."""
    branch = f"claude/{slug}"
    with _actions_lock:
        cur = _actions.get(branch)
        if cur and cur.get("status") == "running":
            return False, "an action is already running for this branch"
        _actions[branch] = {"name": name, "status": "running", "output": "",
                            "started": _now_iso(), "finished": None}

    def run():
        text, ok = "", False
        try:
            out = subprocess.run(
                [str(ROOT / "scripts" / "branch.sh"), *argv], cwd=str(ROOT),
                capture_output=True, text=True, timeout=ACTION_TIMEOUT,
                check=False)
            text = ((out.stdout or "") + (out.stderr or "")).strip()
            ok = out.returncode == 0
            if ok and post_ok:
                extra = post_ok(text)
                if extra:
                    text = text + "\n" + extra
        except (OSError, subprocess.SubprocessError) as e:
            text = f"{name} failed to run: {e}"
        try:
            refresh()             # re-sweep BEFORE the status flips off "running" —
        except Exception:  # noqa: BLE001 — a re-sweep must never crash the thread
            pass           # else a caller polling for completion could act on a
        with _actions_lock:      # list that has not caught up with this action yet
            _actions[branch] = {
                "name": name, "status": "ok" if ok else "failed",
                "output": text[-4000:],
                "started": _actions.get(branch, {}).get("started") or _now_iso(),
                "finished": _now_iso()}

    threading.Thread(target=run, daemon=True,
                     name=f"vira-orphan-{name}-{slug}"[:60]).start()
    return True, "started"


def merge(slug):
    """branch.sh merge <slug>, then push (the standing push-by-default
    rule) and, if server/ changed, name the restart the owner must run —
    the server never restarts itself."""
    def post_ok(merge_text):
        push = gitutil.git(ROOT, "push", timeout=60)
        lines = [("push: " + ((push.stdout or "") + (push.stderr or "")).strip())
                 if push.returncode == 0 else
                 ("push FAILED: "
                  + ((push.stderr or "") + (push.stdout or "")).strip())]
        if "server/" in merge_text:
            lines.append(
                "server code changed — restart is the owner's: "
                "launchctl kickstart -k gui/501/nyc.durham.vira")
        return "\n".join(lines)
    return _run_action(slug, ["merge", slug], "merge", post_ok=post_ok)


def discard(slug, force=False):
    argv = ["discard", slug] + (["--force"] if force else [])
    return _run_action(slug, argv, "discard")


RESUME_PROMPT = """You are resuming stalled work in a branch-first repository.

Worktree: {worktree}
Branch: {branch}
Live checkout (read-only for you — do not edit it): {live_root}

An earlier session started this work and never finished it — it sat \
uncommitted, unmerged, or the session that started it stalled. Below is \
the worktree's current state.
{job_block}
Uncommitted changes (git status --porcelain):
{status}

Commits on this branch not yet on main (git log --oneline main..branch):
{log}

Finish the work: read what is already there, understand what was being \
built, and carry it through to completion. Run the test suite. Do NOT \
merge and do NOT push — the owner decides that. When you are done (or if \
you get stuck and need the owner's judgment), end with the usual decision \
menu: merge it, spin up a test instance, or discard it.
"""


def resume_prompt(item):
    """The composed resume prompt — also servable read-only for a passive
    instance to copy into another session (the apply-prompt pattern)."""
    wt = item.get("worktree") or ""
    branch = item.get("branch") or ""
    status_out, log_out = "(worktree not available)", "(worktree not available)"
    if wt:
        st = gitutil.git(Path(wt), "status", "--porcelain", timeout=20)
        status_out = (st.stdout or "").strip() or "(clean)"
        lg = gitutil.git(Path(wt), "log", "--oneline", f"main..{branch}",
                         "-n", "30", timeout=20)
        log_out = (lg.stdout or "").strip() or "(no unmerged commits)"
    job = item.get("job")
    job_block = (f"\nThe originating job was: \"{job.get('title')}\" "
                f"(final status: {job.get('status')})\n") if job else ""
    return RESUME_PROMPT.format(
        worktree=wt, branch=branch, live_root=str(ROOT), job_block=job_block,
        status=status_out[:4000], log=log_out[:3000])


def resume(item):
    """Dispatch a session to resume stalled work in ITEM's worktree.
    Synchronous — the launch call itself is cheap, the session runs
    detached like any other. Raises ValueError when there is no worktree
    to resume into (a branch this sweeper found with no linked worktree —
    recreating one automatically is out of scope; the owner reruns
    `branch.sh start`)."""
    wt = item.get("worktree")
    if not wt:
        raise ValueError(
            f"no worktree for {item.get('branch')} — recreate it with "
            "scripts/branch.sh before resuming")
    branch = item.get("branch") or ""
    # Refuse while anything live owns the branch — checked FRESH at click
    # time, never off the possibly day-old item row (the judge's high
    # finding: sweep-time state must not authorize a second agent into a
    # tree a session is writing).
    with _actions_lock:
        a = _actions.get(branch)
        if a and a.get("status") == "running":
            raise ValueError(
                f"an action ({a.get('name')}) is already running for "
                f"{branch} — wait for it to finish")
    newest = {}
    for r in joblog.list_records():     # ascending -> last write wins
        b = r.get("branch")
        if b:
            newest[b] = r
    row = newest.get(branch)
    if row and row.get("status") == "running":
        raise ValueError(
            f"a session is already live on {branch} "
            f"(job {row.get('id')}) — steer, answer, or finish it "
            "instead of resuming over it")
    from . import session
    prompt = resume_prompt(item)
    return session.sessions.launch(
        prompt, cwd=wt, meta={"kind": "orphan-resume", "branch": item.get("branch")})
