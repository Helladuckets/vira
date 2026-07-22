"""Fresh-eyes judge — a clean session grades finished work.

The breadboard lab's verification methodology, productized: after a build
(or any job), a FRESH session with no shared context reads the original
ask, the produced output, and — when the job ran in a git repo — the
actual diff, then returns a structured verdict: a letter grade, per-axis
scores, findings, and a ship/fix/redo recommendation.

Judge sessions run read-only (spec read_only=True: write tools disallowed
at the SDK level AND gate-denied instantly, no hanging cards) in
interactive mode, so the only reachable tools are the auto-allowed
read set + the native vira tools. The evidence (diff, transcript tail)
is computed server-side and embedded in the prompt — the judge never
needs Bash.

Verdicts are parsed from the judge's final JSON block and written back to
the judged job's ledger record (joblog.record_judge) and, when the job
was idea-linked, the idea's note. Circuits parse verdicts through the
same helpers for judge stages and grade gates.
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import jobfiles, joblog, settings

GRADES = ["F", "D-", "D", "D+", "C-", "C", "C+",
          "B-", "B", "B+", "A-", "A", "A+"]
DIFF_CAP = 30_000
OUTPUT_CAP = 20_000
POLL_S = 3.0
JUDGE_TIMEOUT = 1800

VERDICT_CONTRACT = """Return your verdict as the FINAL thing in your reply,
as a single JSON object in a ```json code fence:

{"grade": "<A+|A|A-|B+|B|B-|C+|C|C-|D+|D|D-|F>",
 "score": <0-100>,
 "summary": "<two-sentence overall assessment>",
 "findings": [{"severity": "high|medium|low", "note": "<specific issue>"}],
 "recommendation": "ship|fix|redo"}

Grade honestly. An A means you would ship it untouched; a C means it works
but a careful reviewer would push back; an F means it does not do what was
asked. Findings must be specific and actionable, not generic advice."""


def grade_value(grade):
    """Letter grade -> ordinal (F=0 .. A+=12); None for unknown."""
    try:
        return GRADES.index((grade or "").strip().upper()
                            .replace("PLUS", "+").replace("MINUS", "-"))
    except ValueError:
        return None


def meets(grade, min_grade):
    gv, mv = grade_value(grade), grade_value(min_grade)
    if gv is None or mv is None:
        return False
    return gv >= mv


def parse_verdict(text):
    """The last JSON object containing a "grade" key anywhere in the text,
    fenced or bare. None when the judge failed to follow the contract."""
    if not text:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates += re.findall(r"(\{[^{}]*\"grade\"[^{}]*\})", text, re.S)
    for raw in reversed(candidates):
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "grade" in obj:
            obj["grade"] = str(obj["grade"]).strip().upper()
            if grade_value(obj["grade"]) is None:
                continue
            return obj
    return None


def _git_diff(cwd):
    """Working-tree diff + status for a repo cwd, capped. Empty string when
    cwd is not a git repo or git is unhappy — evidence, not a hard dep."""
    if not cwd or not (Path(cwd).expanduser() / ".git").exists():
        return ""
    try:
        status = subprocess.run(
            ["git", "status", "--short"], cwd=cwd, capture_output=True,
            text=True, timeout=20).stdout
        diff = subprocess.run(
            ["git", "diff"], cwd=cwd, capture_output=True,
            text=True, timeout=30).stdout
        untracked = [ln[3:] for ln in status.splitlines()
                     if ln.startswith("?? ")]
        extra = []
        root = Path(cwd).expanduser().resolve()
        for f in untracked[:12]:
            p = Path(cwd).expanduser() / f
            # Containment: never follow an untracked symlink, and never
            # read a path that resolves outside the repo root — this text
            # lands verbatim in a model prompt (audit P1-4).
            if p.is_symlink():
                continue
            try:
                if not p.resolve().is_relative_to(root):
                    continue
            except OSError:
                continue
            if p.is_file() and p.stat().st_size < 40_000:
                try:
                    extra.append(f"--- new file: {f}\n"
                                 + p.read_text(errors="replace")[:4000])
                except OSError:
                    pass
        return (f"git status --short:\n{status}\n\ngit diff:\n{diff}\n\n"
                + "\n\n".join(extra))[:DIFF_CAP]
    except Exception:  # noqa: BLE001 — evidence gathering is best-effort
        return ""


def build_prompt(ask, output, cwd=None, transcript_tail="", context=""):
    """The judge brief: original ask + evidence. Deterministic — the judge
    session itself needs no shell access."""
    diff = _git_diff(cwd)
    parts = [
        "You are a JUDGE — a fresh, independent reviewer with no stake in "
        "the work. Another agent was given a task; your job is to grade "
        "what it produced. Be rigorous and specific: check the work "
        "against what was actually asked, not against effort.",
        f"THE ORIGINAL ASK:\n{(ask or '').strip()[:8000]}",
    ]
    if context:
        parts.append(f"CONTEXT:\n{context[:4000]}")
    if output:
        parts.append(f"THE WORKER'S FINAL REPORT:\n{output[:OUTPUT_CAP]}")
    if diff:
        parts.append("THE ACTUAL CHANGES ON DISK (git working tree at "
                     f"{cwd}):\n{diff}")
        parts.append("Judge the DIFF above as the primary evidence — the "
                     "report is the worker's claim, the diff is the truth. "
                     "You may Read files in the repo to verify claims.")
    elif transcript_tail:
        parts.append(f"SESSION TRANSCRIPT (tail):\n"
                     f"{transcript_tail[-OUTPUT_CAP:]}")
    parts.append(VERDICT_CONTRACT)
    return "\n\n".join(parts)


def prompt_for_job(jid):
    """Judge brief for a finished ledger job."""
    rec = joblog.get_record(jid)
    if not rec:
        raise KeyError(jid)
    output = rec.get("result") or ""
    tail = jobfiles.tail_output(jobfiles.job_dir(jid), OUTPUT_CAP)
    return build_prompt(rec.get("prompt"), output, cwd=rec.get("cwd"),
                        transcript_tail=tail)


def judge_model():
    return settings.get("judge_model") or "opus"


def record_and_close(target_jid, verdict, judge_jid=None, idea_id=None):
    """The shared judge epilogue: stamp the verdict with the judge's job
    id, write it to the judged job's ledger record, and — when the judged
    job was idea-linked — append the outcome note to the idea
    (best-effort). Both judge paths end here: the ad-hoc /api/judge
    watcher below and circuits' judge stages (whose gate/retry/cascade
    logic stays in circuits.py). Returns the verdict as recorded."""
    from . import ideas
    v = dict(verdict)
    if judge_jid:
        v["judge_job"] = judge_jid
    joblog.record_judge(target_jid, v)
    if idea_id:
        try:
            ideas.stamp_note(idea_id,
                             f"judged {v['grade']} "
                             f"(job {(judge_jid or '')[:8]})",
                             append=True)
        except Exception:  # noqa: BLE001 — write-back is best-effort
            pass
    return v


def launch_judge(jid, model=None):
    """Spawn a fresh judge session over a finished job; returns the judge's
    job id. A watcher thread parses the verdict when it lands and writes it
    back to the judged job's ledger record (+ the linked idea's note)."""
    from . import session
    rec = joblog.get_record(jid)
    if not rec:
        raise KeyError(jid)
    if rec.get("status") == "running":
        raise ValueError("job is still running — judge it when it finishes")
    prompt = prompt_for_job(jid)
    judge_jid = session.sessions.launch(
        prompt, cwd=rec.get("cwd"), model=model or judge_model(),
        mode="interactive", read_only=True,
        meta={"judge_of": jid})
    threading.Thread(target=_watch_judge, args=(jid, judge_jid),
                     daemon=True, name=f"vira-judge-{judge_jid}").start()
    return judge_jid


def _watch_judge(jid, judge_jid):
    from . import session
    deadline = time.time() + JUDGE_TIMEOUT
    while time.time() < deadline:
        snap = session.sessions.get(judge_jid) or joblog.get_record(judge_jid)
        status = (snap or {}).get("status", "running")
        if status != "running":
            break
        time.sleep(POLL_S)
    rec = joblog.get_record(judge_jid) or {}
    snap = session.sessions.get(judge_jid) or {}
    verdict = parse_verdict(snap.get("result_text") or rec.get("result"))
    if verdict is None:
        verdict = {"grade": "?", "score": None,
                   "summary": "judge finished without a parseable verdict",
                   "findings": [], "recommendation": ""}
    judged = joblog.get_record(jid) or {}
    record_and_close(jid, verdict, judge_jid=judge_jid,
                     idea_id=judged.get("idea_id"))
