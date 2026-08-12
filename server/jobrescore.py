"""Rescoring — a role's judgment, re-read against today's canon.

WHAT THIS CLOSES. `jobscores.py` (2026-08-12) landed the store and the
reporting: one score per role, provenance stamped by the server, staleness
derived from the canon's own mtime. It answered the owner's question —
1,238 of 1,254 scores predate the Aug 12 canon — and could do nothing
about it. Auto-scoring only ever touches roles with NO score, so a
`why_fit` written in July against a MASTER_HISTORY that has since moved
stayed on screen forever, correct-looking and out of date.

Two depths, the `profilerefresh.py` shape (this module is deliberately its
sibling — read that one first):

  current — ONE `suggest.complete` over the stored posting, the prior
      score, and the passages of the owner's record that actually govern
      a fit judgment for THIS role. Seconds, no session.
  refetch — the posting is refetched first (`jobdesc.describe(refresh=
      True)`), then the same pass. A posting's own text can have changed
      since the sweep stored it, and a rescore against a stale copy would
      re-judge a job that no longer exists in that form.

Plus the QUEUE and the batch prompt behind the scheduled drain: which
roles are stale enough to be worth a session, in the order worth spending
on. `jobboards.maybe_auto_score` is the scheduler — every guard it
already carries (one dispatch in flight, the AI-ready probe, the
20-minute floor) applies unchanged, so this adds a phase rather than a
second scheduler.

THE CANON CANNOT BE INLINED. `MASTER_HISTORY.md` is ~200KB and
`suggest.complete` is a one-shot with no tools, so unlike `score_prompt`
— which tells an AGENT SESSION to go read the file — the one-pass has to
bring the record to the model. It does that through `resumeview._corpus`
/ `_anchors_for`: IDF-weighted coverage retrieval over the record's own
passages, cached on the record's mtime, with the approved-wording
endnotes given reserved slots. Never inline a head excerpt instead — the
head of a career record is an arbitrary slice and usually the wrong one.

WHAT THE SERVER STILL OWNS. Every write goes through
`jobscores.write`, which validates the entry, stamps `scored_at` and
`canon`, and keeps the previous score one deep under `prev`. A rescore is
the exact case that trap was built for: a model that could claim its own
provenance could claim to be newer than it is, and the whole staleness
report reads off those two fields.
"""
import json

from . import jobscores, settings, suggest

# The fields a rescore may rewrite — `jobscores`' own vocabulary, minus
# `uid` (the server decides which role was rescored; a model naming a
# different one is answering about something else) and minus the
# provenance the store stamps.
MODEL_FIELDS = ("fit", "screen", "tier", "final_tier", "lane", "why_fit",
                "lead_with", "caveat", "comp_note", "verdict")

# Provenance from an EARLIER pass that must not ride forward onto a fresh
# judgment. Everything else on the prior entry does — the corpus carries
# `_fulluid`, `_title`, `served`, `verify_note` and more, and `_fulluid`
# in particular is what joins a truncated uid to its board uuid, so a
# rescore that rebuilt the record from scratch would silently unjoin it.
DROP_ON_REWRITE = ("source_file", "scored_at", "canon", "prev")

MODES = ("current", "refetch")

# A rescore that comes back with "Strong fit." is worse than no rescore:
# it overwrites a considered paragraph with nothing and stamps it current.
MIN_WHY = 60

JD_IN_PROMPT = 9000     # the posting, enough to re-judge without the boilerplate
PRIOR_WHY_IN_PROMPT = 900
ANCHOR_TEXT = 900

# The query the record is retrieved against. A whole 24k posting would
# make almost every passage of the record share SOME rare token, which
# ranks by passage length rather than by relevance; the title, team and
# the opening of the description are what actually say what the job is.
QUERY_JD_CHARS = 3000


class RescoreError(RuntimeError):
    """A rescore that could not honestly produce a score."""


# ----------------------------------------------------------------- context

def _anchors(role, jd_text):
    """The passages of the owner's own record that govern a fit judgment
    for this posting. Deterministic and inspectable — `resumeview` owns
    the retrieval and its reasoning; this is one caller of it."""
    try:
        from . import resumeview
        query = " ".join(x for x in (
            role.get("title"), role.get("team"), role.get("family"),
            (jd_text or "")[:QUERY_JD_CHARS]) if x)
        return resumeview._anchors_for(query)
    except Exception:  # noqa: BLE001 — a record Vira cannot read is a
        return []      # thinner prompt, never a failed rescore


def _ruling_lines(udir):
    """The owner's standing ruling, stated inline. Small enough to inline
    (a shortlist and two cut rules), unlike the canon — and a rescore that
    could not see it would happily promote a role he has already cut."""
    from . import applications
    adj = applications._load_adjudication(udir)
    if not adj:
        return []
    out = ["THE OWNER'S STANDING RULING (honor it — a pick stays picked, "
           "a cut stays cut):"]
    if adj["shortlist"]:
        out.append(f"- {len(adj['shortlist'])} roles are pinned picks; a "
                   "pick is never demoted to a cut.")
    if adj["cut_comp"]:
        out.append(f"- comp structure {sorted(adj['cut_comp'])} is cut: "
                   + adj["reason_comp"])
    if adj["cut_titles"]:
        out.append("- these title patterns are cut ("
                   + adj["reason_title"] + "): "
                   + ", ".join(p.pattern for p in adj["cut_titles"][:12]))
    out.append("- NEVER cut on the board's own function label: it files "
               "base-comp deployment roles under 'Sales & Go-To-Market'.")
    return out + [""]


def prompt(role, jd, prior, anchors, ruling):
    """The one-pass contract. Strict JSON, one role, `jobscores`' own
    field names, and every claim about the owner grounded in the anchors
    rather than in what someone with this title probably did."""
    lines = [
        "Re-score ONE job posting for the person whose career record is "
        "quoted below. A score for this role already exists; it was "
        "written against an older version of his record, and your job is "
        "to judge the role again as his record reads NOW.",
        "",
        "THE TWO-SCORE DISCIPLINE, and both are required:",
        "- fit (0-100): narrative resonance. Does this role fit the story "
        "his record actually tells?",
        "- screen (0-100): screening probability. Would a recruiter "
        "shortlist him? A hard minimum stated in the posting is a "
        "probability screen, not something to reframe away.",
        "",
    ]
    lines += ruling
    lines += [
        "THE ROLE:",
        json.dumps({k: role.get(k) for k in (
            "uid", "company", "title", "team", "family", "locations",
            "seniority", "salaryMin", "salaryMax", "comp_kind", "url")},
            ensure_ascii=False),
        "",
    ]
    if jd.get("text"):
        rung = {"live": "fetched from the board just now",
                "snapshot": f"from the sweep of {(jd.get('as_of') or '')[:10]}",
                "blurb": "AN OPENING EXCERPT ONLY, not the full posting"}
        lines += [f"THE POSTING ({rung.get(jd.get('source'), jd.get('source'))}"
                  + (", truncated" if len(jd["text"]) > JD_IN_PROMPT else "")
                  + "):",
                  jd["text"][:JD_IN_PROMPT], ""]
    else:
        lines += ["THE POSTING: not available — " + (jd.get("reason") or "")
                  + " Judge from the role fields above and say in why_fit "
                  "that the description could not be read.", ""]

    if prior:
        p = {k: prior.get(k) for k in ("fit", "screen", "tier", "final_tier",
                                       "lane") if prior.get(k) is not None}
        why = str(prior.get("why_fit") or "")
        lines += [
            "THE SCORE YOU ARE REVISING"
            + (f" (written {(prior.get('scored_at') or '')[:10]})"
               if prior.get("scored_at") else " (written before scores were "
               "dated)") + ":",
            json.dumps(p, ensure_ascii=False),
            "Its reasoning"
            + (f" (first {PRIOR_WHY_IN_PROMPT} characters)"
               if len(why) > PRIOR_WHY_IN_PROMPT else "") + ": "
            + why[:PRIOR_WHY_IN_PROMPT],
            "",
            "Keep what still holds. Change what the record no longer "
            "supports, and say what changed. Do not rewrite it merely to "
            "sound different.",
            "",
        ]

    if anchors:
        lines.append(
            "HIS RECORD — the passages that bear on this role. ANSWER ONLY "
            "FROM THESE about him. Anchors marked GATE carry the approved "
            "outward wording and its limits; they outrank narrative context "
            "on any conflict. If they do not establish something, do not "
            "claim it.")
        for i, a in enumerate(anchors, 1):
            head = (" — " + a["heading"]) if a.get("heading") else ""
            tag = "GATE" if a.get("gate") else "RECORD"
            lines.append(f"[{i}] {tag}{head}: {a['text'][:ANCHOR_TEXT]}")
        lines.append("")
    else:
        lines += ["HIS RECORD: no passages could be retrieved. Judge the "
                  "role on its own terms and keep every claim about him to "
                  "what the prior score already established.", ""]

    lines += [
        "Return STRICT JSON only, no prose around it:",
        '{"fit": 0-100, "screen": 0-100, "tier": "1"|"2"|"3"|"pass"|"cut",',
        ' "final_tier": same vocabulary, "lane": "short phrase",',
        ' "why_fit": "2-5 sentences", "lead_with": "one sentence",',
        ' "caveat": "the honest problem, or \\"\\"",',
        ' "comp_note": "one line, or \\"\\"",',
        ' "verdict": "confirm"|"demote"|"flag"}',
        "",
        "verdict is your read on the change: confirm = the prior judgment "
        "still stands, demote = it was too generous, flag = something here "
        "needs the owner's eye. why_fit under 1200 characters, every other "
        "text field under 600. No emojis.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ write

def _clean(raw, uid, prior, mode):
    """Validate the model's proposal and compose the record to store.

    Grounded in the store's own vocabulary: anything outside MODEL_FIELDS
    is ignored rather than written, and the prior entry's own extras ride
    forward so a rescore never costs the corpus a field it was carrying.

    A JUDGMENT FIELD THE RESCORE DID NOT RESTATE IS DROPPED, never carried
    over. A score is one coherent read: a July caveat sitting beside an
    August fit would present as current when nothing re-made it, and the
    prompt asks for every field explicitly, so an omission is the model
    declining to answer rather than asserting "unchanged". Losing a field
    is visible; a stale one dressed as fresh is not.
    """
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        raise RescoreError("the model did not return a score object")

    entry = {k: v for k, v in (prior or {}).items()
             if k not in DROP_ON_REWRITE and k not in MODEL_FIELDS}
    for field in MODEL_FIELDS:
        val = raw.get(field)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        entry[field] = val
    entry["uid"] = uid
    entry["rescored_from"] = mode

    why = str(entry.get("why_fit") or "").strip()
    if len(why) < MIN_WHY:
        raise RescoreError(
            "the rescore came back with no real reasoning — keeping the "
            "score that was already on file")
    return entry


def rescore(uid, mode="current"):
    """Re-judge one role. Returns the stored record plus what it read."""
    if mode not in MODES:
        raise RescoreError(f"mode must be one of: {', '.join(MODES)}")
    if settings.fixture_mode():
        return {"status": "empty", "note": "fixture mode"}
    # Refused BEFORE the model call, not after: scores live in the owner's
    # real self-record, outside the cloned data/ a branch instance gets, so
    # a passive rescore could never land — and spending the call to find
    # that out would be a refusal the owner paid for.
    jobscores._refuse_if_passive()

    from . import applications, jobdesc
    role = applications.find_role(uid)
    if role is None:
        raise KeyError(uid)

    udir = applications.universe_dir()
    prior = jobscores.load(udir).get(uid)
    jd = jobdesc.describe(role, refresh=(mode == "refetch"))
    anchors = _anchors(role, jd.get("text") or "")
    text = suggest.complete(prompt(role, jd, prior, anchors,
                                   _ruling_lines(udir)))
    entry = _clean(suggest._extract_json(text), uid, prior, mode)

    try:
        rec = jobscores.write(entry, udir=udir,
                              known=jobscores.known_uids(udir) or None)
    except jobscores.ScoreError as e:
        raise RescoreError(str(e))
    return {"status": "ok", "score": rec, "mode": mode,
            "jd_source": jd.get("source") or "", "jd_as_of": jd.get("as_of"),
            "anchors": len(anchors),
            "was": {k: (prior or {}).get(k) for k in ("fit", "screen", "tier")}}


# ------------------------------------------------------------- the queue

# A rescore carries the prior judgment INTO the session as well as the
# posting, so each role costs more of a session's context than a first
# score does; the batch is smaller than score_prompt's 40 for that reason
# alone. The scheduler runs one session at a time either way.
BATCH = 25


def queue(udir=None, limit=None):
    """Roles whose score predates the current canon and is still worth
    spending on, in the order worth spending it: tier first, then oldest.

    THE ORDER IS THE WHOLE DESIGN. Measured 2026-08-12 across the 962
    roles that qualify: T1 13 / T2 35 / T3 134 / pass 777. So the 182
    roles the owner would actually act on drain in the first few sessions,
    and the long `pass` tail — already judged not a fit, and exactly what
    a moved canon could flip back into contention — follows behind rather
    than being skipped. No extra knob: the ordering IS the priority.

    Gone postings are excluded (there is nothing to apply to) but
    `unverified` ones are kept: that state means no board has confirmed
    them recently, which is not the same as knowing they are down.
    """
    from . import applications
    udir = udir if udir is not None else applications.universe_dir()
    scores = jobscores.load(udir)
    canon = jobscores.canon_at(udir)
    out = []
    for role in applications.load_universe():
        entry = scores.get(role["uid"])
        if not entry:
            continue                      # unscored: the other queue's job
        if not role.get("eligible") or role.get("cut"):
            continue
        if role.get("availability") == "gone":
            continue
        if not jobscores.is_stale(entry, canon):
            continue
        out.append((applications.TIER_RANK.get(
            str(entry.get("final_tier") or entry.get("tier") or ""), 4),
            str(entry.get("scored_at") or ""), role, entry))
    out.sort(key=lambda r: (r[0], r[1]))
    rows = [(role, entry) for _rank, _at, role, entry in out]
    return rows[:limit] if limit else rows


def batch_prompt(limit=BATCH, udir=None):
    """The prompt behind the scheduled drain: an agent session (cwd = the
    self-record, so it can read the whole canon itself) that re-judges a
    batch and files through the validated write tool.

    It NEVER writes a score file by hand — that is what
    `mcp__vira__record_role_scores` is for, and it is the difference
    between the server stamping provenance and a model claiming it.
    """
    from . import applications
    udir = udir if udir is not None else applications.universe_dir()
    rows = queue(udir, limit)
    if not rows:
        return "", 0

    canon = jobscores.canon_at(udir)
    facts = applications.self_record() / "canon" / "MASTER_HISTORY.md"
    ruling = udir / "owner-adjudication.json"

    lines = [
        "Re-score job roles whose analysis predates the owner's current "
        "record. Each role below already has a score; it was written "
        "against an older version of his canon and you are judging the "
        "role again as the canon reads now.",
        "",
        f"1. Read {facts} — it is the authority on his background, and "
        "every claim you make about him has to pass it. The canon last "
        f"changed {canon[:10] or '(unknown)'}; each role below carries the "
        "date its score was written, so you can see what it could not have "
        "known.",
    ]
    if ruling.exists():
        lines.append(f"2. Read {ruling} and honor it — a pinned pick stays "
                     "picked, a cut stays cut, and never cut on the board's "
                     "own function label.")
    lines += [
        f"{'3' if ruling.exists() else '2'}. Re-read each posting (the url "
        "is on the role) before re-judging it. A posting can have changed "
        "since it was last read.",
        f"{'4' if ruling.exists() else '3'}. Score with the TWO-SCORE "
        "discipline, both required: fit (narrative resonance) and screen "
        "(screening probability, where a hard minimum in the posting is a "
        "screen and not something to reframe away).",
        f"{'5' if ruling.exists() else '4'}. File every re-judged role with "
        "ONE call to mcp__vira__record_role_scores — scores_json is a JSON "
        "array of objects: uid, fit, screen, tier, final_tier, lane, "
        "why_fit, lead_with, caveat, comp_note, verdict. Do NOT write or "
        "edit any score file yourself: the server validates each entry and "
        "stamps when it was written and which canon it was written "
        "against. A refused entry names its reason — fix and re-file only "
        "that one.",
        "",
        "Keep what still holds and change what the record no longer "
        "supports. A rescore that only rewords the prior reasoning is "
        "wasted; say plainly when your judgment is unchanged.",
        "",
        f"ROLES TO RE-SCORE ({len(rows)}):",
    ]
    for role, entry in rows:
        why = str(entry.get("why_fit") or "")
        lines.append(json.dumps({
            "uid": role["uid"],
            "company": role.get("company"),
            "title": role.get("title"),
            "locations": role.get("locations"),
            "url": role.get("url"),
            "comp": role.get("comp_kind"),
            "scored_at": (entry.get("scored_at") or "")[:10] or "undated",
            "prior": {k: entry.get(k) for k in ("fit", "screen", "tier",
                                                "final_tier", "lane")
                      if entry.get(k) is not None},
            "prior_why_fit": why[:PRIOR_WHY_IN_PROMPT]
            + ("…" if len(why) > PRIOR_WHY_IN_PROMPT else ""),
        }, ensure_ascii=False))
    return "\n".join(lines), len(rows)


def auto_rescore_enabled():
    """The owner's call, 2026-08-12: the stale drain runs by default.

    Named rather than assumed, because it is real spend — the queue was
    962 roles at the decision, roughly two dozen sessions. `false` stops
    it without touching the per-role button or the unscored-role pass.
    """
    return settings.raw().get("boards_auto_rescore", True) is not False


def status(udir=None):
    """What the drain can honestly say about itself. One implementation —
    the boards strip reads this rather than counting a second time."""
    try:
        rows = queue(udir)
    except Exception:  # noqa: BLE001 — a status line never raises
        return {"rescore_queue": None, "auto_rescore": auto_rescore_enabled()}
    return {"rescore_queue": len(rows),
            "auto_rescore": auto_rescore_enabled(),
            "rescore_next": [r["uid"] for r, _e in rows[:5]]}


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "prompt":
        text, n = batch_prompt()
        print(text or f"nothing to rescore ({n})")
    elif cmd == "queue":
        for role, entry in queue(limit=40):
            print(f"{entry.get('final_tier') or entry.get('tier') or '?':>4}  "
                  f"{(entry.get('scored_at') or 'undated')[:10]}  "
                  f"{role['uid']:<28} {role.get('company')} — "
                  f"{role.get('title')}")
    else:
        print(json.dumps(status(), indent=1))
