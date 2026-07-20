"""TC-IL morning picker, surfaced inside Vira (headless submit -> apply).

The 06:00 TC-IL task builds a keyframe contact sheet ("morning picker") for
new subscription videos and records the batch as `pending` in TC-IL's state
file. This router removes the old copy-paste seam (open the :8778 picker,
Copy selection, paste JSON into a session, run /subs-visuals-apply) by:

- GET  /api/subs-visuals/status       -> is a batch pending? (reads TC-IL's
                                         youtube-subs-visuals-state.json in
                                         place — single source of truth)
- GET  /api/subs-visuals/files/{path} -> serves the pending batch dir
                                         (picker.html + frame jpgs) through
                                         Vira's always-on origin, so the
                                         picker loads on the phone over
                                         Tailscale. picker.html gets a
                                         "Submit to Vira" toolbar injected at
                                         serve time — zero change to the
                                         TC-IL generator, and the standalone
                                         :8778 / file:// picker is untouched.
- POST /api/subs-visuals/apply        -> validates the submitted picks map
                                         against pending.videos, writes
                                         <batch_dir>/picks.json, and
                                         dispatches a headless claude job
                                         (bypassPermissions, cwd=TC-IL) that
                                         runs /subs-visuals-apply from step 3
                                         (extract -> caption -> Sage gate ->
                                         apply -> commit -> push), streamed
                                         into the existing job panel.

The apply pipeline itself is NOT reimplemented here — its captioning and
Sage-review steps are LLM work; Vira just directs Claude to run the existing
TC-IL command.
"""
import json
import mimetypes
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

TCIL_ROOT = Path("~/TC-IL").expanduser()
STATE_FILE = TCIL_ROOT / "scripts" / "youtube-subs-visuals-state.json"

router = APIRouter(prefix="/api/subs-visuals")

_jobs = None                 # actions.Jobs, injected by main.py at import time
_apply_jobs = {}             # batch_dir(str) -> job id (this process's dispatches)
_apply_lock = threading.Lock()


def configure(jobs):
    """Hand the shared Jobs runner in from main.py (avoids a circular import)."""
    global _jobs
    _jobs = jobs


def _state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _pending():
    """The pending batch record, or None. Guards against a stale record whose
    batch dir has already been reclaimed."""
    p = _state().get("pending")
    if not p or not p.get("batch_dir"):
        return None
    return p


def _job_for(batch_dir):
    jid = _apply_jobs.get(batch_dir)
    j = _jobs.get(jid) if (jid and _jobs) else None
    if not j:
        return None
    return {"id": j["id"], "status": j["status"],
            "started": j["started"], "finished": j["finished"]}


@router.get("/status")
def status():
    p = _pending()
    if not p:
        return {"pending": None, "job": None}
    batch = Path(p["batch_dir"])
    return {
        "pending": {
            "batch_dir": p["batch_dir"],
            "built": p.get("built", ""),
            "videos": [{k: v.get(k, "") for k in
                        ("slug", "title", "channel", "url", "video_id")}
                       for v in p.get("videos", [])],
            "picker_ready": (batch / "picker.html").is_file(),
        },
        "job": _job_for(p["batch_dir"]),
    }


# ---------- serve the pending batch (picker + frames), same-origin ----------

# Appended to picker.html at serve time. Builds the Submit toolbar with DOM
# calls (no fragile string surgery on the generated header) and reuses the
# picker's own selection markup (.c.sel + data-slug/data-sec/data-panel) to
# assemble the exact {slug: [seconds|"panel-id"]} map Copy selection emits.
_SUBMIT_SNIPPET = """
<script>
(function () {
  var VIRA_BATCH_DIR = __BATCH_DIR__;
  if (!document.querySelector('meta[name="viewport"]')) {
    var mv = document.createElement('meta');
    mv.name = 'viewport';
    mv.content = 'width=device-width, initial-scale=1';
    document.head.appendChild(mv);
  }
  var btn = document.createElement('button');
  btn.id = 'vira-submit';
  btn.textContent = 'Submit to Vira';
  btn.style.cssText = 'background:#d9a441;color:#141312;font-weight:700';
  var note = document.createElement('span');
  note.className = 'muted';
  var hdr = document.querySelector('header');
  if (hdr) { hdr.appendChild(btn); hdr.appendChild(note); }
  else {
    btn.style.cssText += ';position:fixed;right:14px;top:10px;z-index:99';
    document.body.appendChild(btn);
    document.body.appendChild(note);
  }
  function picksMap() {
    var m = {};
    document.querySelectorAll('.c.sel').forEach(function (c) {
      var k = c.dataset.slug;
      (m[k] = m[k] || []).push(c.dataset.panel ? c.dataset.panel
                                               : parseFloat(c.dataset.sec));
    });
    Object.keys(m).forEach(function (k) {
      m[k].sort(function (a, b) {
        var an = typeof a === 'number', bn = typeof b === 'number';
        if (an && bn) return a - b;
        if (an) return -1;
        if (bn) return 1;
        return String(a).localeCompare(String(b));
      });
    });
    return m;
  }
  btn.addEventListener('click', function () {
    var m = picksMap();
    var frames = document.querySelectorAll('.c.sel').length;
    var msg = frames
      ? 'Submit ' + frames + ' pick(s) across ' + Object.keys(m).length
        + ' video(s)? Vira runs the full apply: extract, caption, Sage '
        + 'review, wiki commit + push.'
      : 'Nothing is selected. Submit an EMPTY selection? The whole batch is '
        + 'marked reviewed with no visuals.';
    if (!confirm(msg)) return;
    btn.disabled = true;
    btn.textContent = 'Submitting\\u2026';
    fetch('/api/subs-visuals/apply', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ picks: m, batch_dir: VIRA_BATCH_DIR }),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) throw new Error(body.detail || ('HTTP ' + r.status));
        btn.textContent = 'Submitted';
        note.textContent = ' apply job ' + String(body.job_id || '').slice(0, 8)
          + ' dispatched \\u2014 watch it in Vira';
        try {
          parent.postMessage({ type: 'subs-visuals-submitted',
                               job_id: body.job_id }, location.origin);
        } catch (e) {}
      });
    }).catch(function (e) {
      btn.disabled = false;
      btn.textContent = 'Submit to Vira';
      alert('Submit failed: ' + e.message);
    });
  });
})();
</script>
"""


def _inject(html, batch_dir):
    viewport = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    if '<meta charset="utf-8">' in html:
        html = html.replace('<meta charset="utf-8">',
                            '<meta charset="utf-8">' + viewport, 1)
    return html + _SUBMIT_SNIPPET.replace("__BATCH_DIR__", json.dumps(batch_dir))


@router.get("/files/{path:path}")
def files(path: str):
    p = _pending()
    if not p:
        raise HTTPException(404, "no pending batch")
    batch = Path(p["batch_dir"]).resolve()
    if not batch.is_dir():
        raise HTTPException(404, "pending batch dir is gone")
    target = (batch / (path or "picker.html")).resolve()
    if not (target == batch or target.is_relative_to(batch)):
        raise HTTPException(404, "not found")
    if target.is_dir():
        target = target / "picker.html"
    if not target.is_file():
        raise HTTPException(404, "not found")
    if target.name == "picker.html":
        html = _inject(target.read_text(encoding="utf-8"), str(batch))
        return HTMLResponse(html, headers={"cache-control": "no-store"})
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    # Frames are immutable once built; a rebuilt --force batch lands in a new
    # stamped dir, so per-URL caching for a day is safe and spares the phone
    # re-downloading hundreds of jpgs on every reopen.
    return FileResponse(target, media_type=mime,
                        headers={"cache-control": "private, max-age=86400"})


# ---------- submit -> write picks.json -> dispatch the headless apply ----------

class ApplyReq(BaseModel):
    picks: dict[str, list]
    batch_dir: str | None = None   # staleness check against the live pending


def _validate_picks(picks, pending):
    ok_slugs = {v.get("slug") for v in pending.get("videos", [])}
    bad = sorted(set(picks) - ok_slugs)
    if bad:
        raise HTTPException(400, "unknown slug(s) not in the pending batch: "
                            + ", ".join(bad))
    for slug, vals in picks.items():
        if not isinstance(vals, list):
            raise HTTPException(400, f"picks for {slug} must be a list")
        for v in vals:
            if isinstance(v, bool):
                raise HTTPException(400, f"bad pick value for {slug}: {v!r}")
            if isinstance(v, (int, float)):
                if not (0 <= float(v) < 86400):
                    raise HTTPException(400, f"bad timestamp for {slug}: {v!r}")
            elif isinstance(v, str):
                if not (0 < len(v) <= 40 and
                        all(c.isalnum() or c in "_-." for c in v)):
                    raise HTTPException(400, f"bad panel id for {slug}: {v!r}")
            else:
                raise HTTPException(400, f"bad pick value for {slug}: {v!r}")


def _apply_prompt(batch_dir):
    return "\n".join([
        "You are Vira's subs-visuals apply agent, running headless (no",
        "interactive prompts available) inside the TC-IL repository at",
        str(TCIL_ROOT) + ".",
        "",
        "The owner has just picked keeper frames for the pending",
        "subscription-visuals batch in Vira's morning picker. His picks are",
        "ALREADY WRITTEN to " + batch_dir + "/picks.json — do not ask for",
        "pasted JSON and do not rewrite that file.",
        "",
        "Read .claude/commands/subs-visuals-apply.md and follow it exactly,",
        "starting from step 3 (steps 1-2 — locating the batch and saving the",
        "picks — are already done). That means: extract, caption via",
        "subagents, run the MANDATORY Sage review gate, apply the visuals to",
        "the wiki, mark the batch reviewed, refresh the diagram index, and",
        "finish with the command's scoped commit and push. If picks.json is",
        "an empty map {}, follow the command's empty-selection path: mark the",
        "batch reviewed only, no commit.",
        "",
        "- CRITICAL: you are running INSIDE the Vira server as a child",
        "  process. Never restart, stop, or kill the Vira server, its",
        "  launchd service, or its process (no launchctl kickstart/bootout,",
        "  no pkill of uvicorn or python) — restarting it kills you",
        "  mid-task.",
        "- Stay inside the TC-IL repository. The only git operations allowed",
        "  are the scoped commit and push the command itself specifies.",
        "- No emojis anywhere.",
        "",
        "End with the command's delivery-state report: videos done, frames",
        "shipped, Sage catches, commit sha, pushed (or 'batch cleared, no",
        "commit' on the empty path).",
    ])


@router.post("/apply")
def apply(req: ApplyReq):
    if _jobs is None:
        raise HTTPException(503, "job runner not configured")
    p = _pending()
    if not p:
        raise HTTPException(409, "no pending batch — nothing to apply")
    batch_dir = p["batch_dir"]
    if req.batch_dir and req.batch_dir != batch_dir:
        raise HTTPException(409, "stale picker: the pending batch has changed "
                            "— reload the picker")
    batch = Path(batch_dir)
    if not batch.is_dir():
        raise HTTPException(409, "pending batch dir is gone")
    _validate_picks(req.picks, p)
    with _apply_lock:
        running = _job_for(batch_dir)
        if running and running["status"] == "running":
            raise HTTPException(409, "an apply job is already running for "
                                f"this batch (job {running['id']})")
        # atomic write, same tmp+rename pattern as the other Vira stores
        tmp = batch / "picks.json.tmp"
        tmp.write_text(json.dumps(req.picks, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(batch / "picks.json")
        jid = _jobs.launch(_apply_prompt(batch_dir), cwd=str(TCIL_ROOT),
                           permission_mode="bypassPermissions")
        _apply_jobs[batch_dir] = jid
    frames = sum(len(v) for v in req.picks.values())
    return {"job_id": jid, "videos": len(req.picks), "picks": frames,
            "empty": not req.picks}
