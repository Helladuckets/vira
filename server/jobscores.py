"""Per-role job scores — one score per role, written by Vira.

WHAT THIS REPLACES. A role's judgment (fit, why_fit, tier, lane, verdict)
used to be APPENDED into whichever batch file the agent session that scored
it happened to name that day. No code ever wrote one: `jobboards.score_prompt`
composed an English instruction saying "append a score entry to
2026-08-12-raw-scores.json" and a session wrote the JSON by hand. So the
filename was a suggestion, and 111 files accumulated under 1,254 entries —
`v2`, `d6`, forty `swarm-<ts>-batch-NNN`, eleven dates, and fifty more
carrying invented `b`/`c`/`d` suffixes minted by sessions that found the
day's file already there.

Reading them merged every file in SORTED FILENAME ORDER, later file wins.
That is the trap this module exists to close: digits sort before letters, so
a file named `<today>-raw-scores.json` — exactly what score_prompt generates
— loses every uid collision against `swarm-*` and `v2-*`. It has never bitten
(measured 2026-08-12: zero uids appear in more than one file, because the
pipeline only ever scored roles that had NO score). It would bite the moment
anything RE-scores a role: the new entry would land on disk, look correct,
and be silently overridden by the July pass it was meant to replace.

THE FIX IS A STORE, NOT A SORT RULE. One file per role at
`<universe>/scores/<uid>.json`, so there is no merge and nothing for
precedence to get wrong; a rescore overwrites one file. Legacy batch files
stay on disk and are still read for any uid with no per-role file, so the
migration is additive and reversible.

THE SERVER STAMPS PROVENANCE, NEVER THE MODEL. `scored_at` and `canon` are
written here, because a rescore that could claim to be newer than it is
would defeat the staleness reporting those fields exist to feed. The write
goes through `write()` behind `validate()` — the same proposes/validates/
applies discipline as `update_module_map`, `create_reading_room` and
`configure_applications`.

STALENESS IS DERIVED, NEVER STORED. `canon_at()` is the newest mtime across
the owner's MASTER_HISTORY and their standing adjudication; a score written
before that moment is stale by definition. Move the canon and every earlier
score reads stale on the next request, with no sweep to run and no flag to
maintain.

Entry shape (everything beyond the named fields is preserved verbatim —
the corpus carries `served`, `_fulluid`, `_title`, `verify_note` and more,
and a store that dropped them would lose data on migration):

  {"uid", "fit", "screen", "tier", "final_tier", "lane", "why_fit",
   "lead_with", "caveat", "comp_note", "verdict",
   "scored_at", "canon", "source_file"?, "prev"?}
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import jsonstore
from .filelock import locked

SCORES_SUBDIR = "scores"
LEGACY_GLOB = "*-raw-scores.json"

# The vocabulary the real corpus uses, measured rather than invented:
# tier is "1"/"2"/"3"/"pass" across all 1,254 entries and final_tier adds
# "cut". applications.TIER_RANK reads these exact spellings.
TIERS = {"1", "2", "3", "pass", "cut", ""}
VERDICTS = {"confirm", "demote", "flag", ""}

REQUIRED = ("uid", "fit", "tier", "why_fit")
TEXT_FIELDS = ("lane", "why_fit", "lead_with", "caveat", "comp_note",
               "verdict", "served")

# Caps apply to what Vira WRITES, never to what it migrates. 454 legacy
# entries already exceed 1,000 characters and the longest why_fit is 7,992;
# truncating those on migration would destroy the owner's own analysis to
# satisfy a rule invented afterwards. The UI clamps long ones instead.
MAX_WHY = 1200
MAX_TEXT = 600
MAX_FIELDS = 48

# uid doubles as the filename, so it is constrained to what is safe as one.
UID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class ScoreError(ValueError):
    """A proposed score the server refuses.

    The message is written FOR THE MODEL: it names the field, what was
    wrong, and what a correct value looks like, so a session can fix one
    entry and retry rather than losing a whole batch to a silent drop.
    """


# ------------------------------------------------------------------ paths

def _udir(udir=None):
    if udir is not None:
        return Path(udir)
    from . import applications
    return applications.universe_dir()


def scores_dir(udir=None):
    return _udir(udir) / SCORES_SUBDIR


def dir_mtime(udir=None):
    """Newest mtime under the scores dir, for cache invalidation.

    applications._universe_key folds this in. Without it a rescore changes
    nothing the catalog cache can see, and the module keeps serving the old
    why_fit until some unrelated file happens to change — the same seam the
    availability work hit with jobboards.state_mtime.
    """
    d = scores_dir(udir)
    newest = 0.0
    try:
        for p in d.glob("*.json"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


# ------------------------------------------------------------ the canon

def _stamp(ts):
    """One shape for every timestamp this module writes or compares.

    Staleness is a string comparison, which is only chronological while both
    sides are UTC ISO at the same precision — so nothing here formats a time
    any other way.
    """
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def now_stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canon_paths(udir=None):
    """The files whose contents a score is written AGAINST."""
    from . import applications
    return [applications.self_record() / "canon" / "MASTER_HISTORY.md",
            _udir(udir) / "owner-adjudication.json"]


def canon_at(udir=None):
    """When the owner's canon last moved, as an ISO stamp — or "" when none
    of it exists on this install (a fresh clone), in which case nothing can
    be stale and `is_stale` says so rather than calling everything stale."""
    newest = 0.0
    for p in canon_paths(udir):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return _stamp(newest) if newest else ""


def is_stale(entry, canon=None, udir=None):
    """True when this score was written before the canon last changed.

    An entry with NO stamp reads stale: every one of those predates stamping
    entirely, so it was written against a canon nobody recorded.
    """
    if canon is None:
        canon = canon_at(udir)
    if not canon:
        return False
    at = str((entry or {}).get("scored_at") or "")
    return at < canon if at else True


# ------------------------------------------------------------ validation

def _clean_uid(raw):
    uid = str(raw or "").strip()
    if not uid:
        raise ScoreError("uid is required — the board uid of the role being "
                         "scored, e.g. 'a-4012345' or 'as-openai-<uuid>'.")
    if not UID_RE.match(uid):
        raise ScoreError(
            f"uid {uid!r} is not a valid role uid: letters, digits, dot, dash "
            "and underscore only, up to 120 characters.")
    return uid


def _clean_int(name, raw, lo, hi, *, required):
    if raw is None or raw == "":
        if required:
            raise ScoreError(f"{name} is required — a whole number {lo}-{hi}.")
        return None
    try:
        val = int(round(float(raw)))
    except (TypeError, ValueError):
        raise ScoreError(f"{name} must be a number {lo}-{hi}, got {raw!r}.")
    if not lo <= val <= hi:
        raise ScoreError(f"{name} must be between {lo} and {hi}, got {val}.")
    return val


def _clean_tier(name, raw, *, required):
    """Accepts the corpus spellings plus the T-prefixed form.

    A model reaching for "T1" is not making a mistake worth a retry round
    trip — it is the way tiers are written everywhere else in this module's
    own UI — so the prefix is stripped rather than refused.
    """
    val = str(raw if raw is not None else "").strip()
    if val[:1] in ("T", "t") and val[1:] in ("1", "2", "3"):
        val = val[1:]
    val = val.lower() if val.lower() in ("pass", "cut") else val
    if val not in TIERS:
        if required or val:
            allowed = ", ".join(sorted(t for t in TIERS if t))
            raise ScoreError(
                f"{name} must be one of: {allowed} (got {raw!r}).")
    if required and not val:
        raise ScoreError(f"{name} is required — one of: 1, 2, 3, pass, cut.")
    return val


def _clean_text(name, raw, cap):
    val = str(raw if raw is not None else "").strip()
    if len(val) > cap:
        raise ScoreError(
            f"{name} is {len(val)} characters; keep it under {cap}. Say what "
            "the fit IS, not everything about the role.")
    return val


def validate(entry, *, known=None, cap_text=True):
    """Check a proposed score and return the cleaned record.

    `known`, when given, is the set of uids that exist on this install —
    a score for a role nothing has ever seen is a typo, not data, and
    storing it would leave a file no reader will ever join.

    `cap_text=False` is the migration path ONLY: legacy entries are
    preserved verbatim, caps or no caps.
    """
    if not isinstance(entry, dict):
        raise ScoreError("each score must be a JSON object.")
    if len(entry) > MAX_FIELDS:
        raise ScoreError(f"too many fields ({len(entry)}); the maximum is "
                         f"{MAX_FIELDS}.")

    uid = _clean_uid(entry.get("uid"))
    if known is not None and uid not in known:
        raise ScoreError(
            f"uid {uid!r} matches no role in the candidate universe or the "
            "boards snapshot. Score only roles listed in the prompt.")

    for field in REQUIRED:
        if field not in entry:
            raise ScoreError(f"{field} is required on every score entry.")

    out = dict(entry)
    out["uid"] = uid
    out["fit"] = _clean_int("fit", entry.get("fit"), 0, 100, required=True)
    screen = _clean_int("screen", entry.get("screen"), 0, 100, required=False)
    if screen is None:
        out.pop("screen", None)
    else:
        out["screen"] = screen
    out["tier"] = _clean_tier("tier", entry.get("tier"), required=True)
    if "final_tier" in entry:
        out["final_tier"] = _clean_tier(
            "final_tier", entry.get("final_tier"), required=False)
    if "verdict" in entry:
        verdict = str(entry.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            allowed = ", ".join(sorted(v for v in VERDICTS if v))
            raise ScoreError(f"verdict must be one of: {allowed} "
                             f"(got {entry.get('verdict')!r}).")
        out["verdict"] = verdict

    for field in TEXT_FIELDS:
        if field == "verdict" or field not in entry:
            continue
        cap = MAX_WHY if field == "why_fit" else MAX_TEXT
        out[field] = (_clean_text(field, entry.get(field), cap) if cap_text
                      else str(entry.get(field) or ""))

    if not out.get("why_fit"):
        raise ScoreError("why_fit is required — one or two sentences on why "
                         "this role fits, grounded in the owner's record.")

    # provenance belongs to the server, never to the proposal
    for field in ("scored_at", "canon", "prev"):
        out.pop(field, None)
    return out


# ------------------------------------------------------------------ read

def _read_one(path):
    """A corrupt or wrong-encoding score file is skipped, never fatal.

    UnicodeDecodeError is named explicitly: it is neither an OSError nor a
    JSONDecodeError, so an install carrying cp1252 bytes from before the
    encoding was pinned would otherwise raise into every caller.
    """
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) and obj.get("uid") else None


def per_role(udir=None):
    """{uid: entry} from the per-role store, double-indexed by `_fulluid`
    exactly as the legacy reader is — some entries carry a truncated uid
    with the full board uuid alongside, and both spellings must join."""
    out = {}
    d = scores_dir(udir)
    try:
        paths = sorted(d.glob("*.json"))
    except OSError:
        return out
    for p in paths:
        entry = _read_one(p)
        if entry is None:
            continue
        for key in (entry.get("uid"), entry.get("_fulluid")):
            if key:
                out[key] = entry
    return out


def _legacy_sourced(udir=None):
    """{key: (entry, source filename)} from the batch files, in the historical
    sorted-name order. Kept only so migration can record where an entry came
    from and so a uid with no per-role file still resolves."""
    out = {}
    d = _udir(udir)
    try:
        files = sorted(d.glob(LEGACY_GLOB))
    except OSError:
        return out
    for sf in files:
        try:
            rows = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            for key in (entry.get("uid"), entry.get("_fulluid")):
                if key:
                    out[key] = (entry, sf.name)
    return out


def legacy(udir=None):
    return {k: v[0] for k, v in _legacy_sourced(udir).items()}


def load(udir=None):
    """The one {uid: entry} map every reader gets.

    PRECEDENCE IS STATED, NOT INFERRED: the per-role store wins, and the
    legacy batch files answer only for uids it does not hold. Filename sort
    order decides nothing.
    """
    scores = legacy(udir)
    scores.update(per_role(udir))
    return scores


# ----------------------------------------------------------------- write

def _refuse_if_passive():
    if os.environ.get("VIRA_PASSIVE"):
        raise PermissionError(
            "passive instance: scores write the owner's real self-record")


def known_uids(udir=None):
    """Every uid this install could legitimately score: the curated universe
    role files plus whatever the boards snapshot is currently serving."""
    uids = set()
    d = _udir(udir)
    role_dir = d / "candidate-universe" / "role"
    try:
        uids |= {p.stem for p in role_dir.glob("*.json")}
    except OSError:
        pass
    try:
        from . import jobboards
        snap = jobboards.snapshot() or {}
        uids |= set((snap.get("roles") or {}).keys())
    except Exception:  # noqa: BLE001 — a broken snapshot never blocks a write
        pass
    return uids


def write(entry, *, udir=None, known=None, source_file=None,
          cap_text=True, keep_prev=True):
    """Validate and store one score. Returns the record as written.

    The previous score is kept one deep under `prev` (the
    save_profile_refresh precedent), so a bad rescore is visibly
    recoverable rather than silently destructive.

    REFUSED ON A PASSIVE INSTANCE. Scores live in the CRM self-record,
    which is OUTSIDE the cloned data/ a branch instance gets — so a test
    copy writing here would edit the owner's real job-search analysis.
    Same boundary plans.py and vaultpeople.create_stub hold.
    """
    _refuse_if_passive()
    clean = validate(entry, known=known, cap_text=cap_text)
    d = scores_dir(udir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{clean['uid']}.json"
    with locked(path):
        prior = _read_one(path)
        if prior and keep_prev:
            prior = dict(prior)
            prior.pop("prev", None)
            clean["prev"] = prior
        clean["scored_at"] = now_stamp()
        clean["canon"] = canon_at(udir)
        if source_file:
            clean["source_file"] = source_file
        jsonstore.write_atomic(path, clean, indent=1, ensure_ascii=False)
    return clean


# ------------------------------------------------------------- migration

def migrate(udir=None, *, force=False):
    """One-time: batch files -> per-role files. Additive and reversible.

    Every legacy file stays exactly where it is; this only writes the
    per-role store the reader now prefers. `scored_at` comes from the source
    file's mtime — the closest honest answer to when that judgment was made —
    and `source_file` records which pass it came from.

    Safe because the merge it reads is unambiguous: measured 2026-08-12, no
    uid appears in more than one batch file, so the winner is the only
    candidate. That stops being true after anything rescores, which is why
    this runs before the write path is live.
    """
    _refuse_if_passive()
    sourced = _legacy_sourced(udir)
    seen, report = set(), {"written": 0, "existing": 0, "skipped": [],
                           "entries": 0}
    d = scores_dir(udir)
    for entry, src in sourced.values():
        marker = id(entry)
        if marker in seen:
            continue
        seen.add(marker)
        report["entries"] += 1
        uid = str(entry.get("uid") or "").strip()
        if not UID_RE.match(uid):
            report["skipped"].append(uid or "<blank uid>")
            continue
        if not force and (d / f"{uid}.json").exists():
            report["existing"] += 1
            continue
        try:
            stamp = _stamp((_udir(udir) / src).stat().st_mtime)
        except OSError:
            stamp = ""
        try:
            clean = validate(entry, cap_text=False)
        except ScoreError:
            report["skipped"].append(uid)
            continue
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{uid}.json"
        with locked(path):
            clean["scored_at"] = stamp
            clean["canon"] = ""       # unknowable for a pre-stamping pass
            clean["source_file"] = src
            jsonstore.write_atomic(path, clean, indent=1, ensure_ascii=False)
        report["written"] += 1
    return report


# ---------------------------------------------------------------- status

def status(udir=None):
    """What the module can honestly say about its own coverage.

    THE ONE implementation of the current/stale/undated split — the boards
    status line reads it rather than counting again, because two counters
    over one question are two chances to disagree about what "stale" means.

    Counts run over `load()`, not the per-role store alone: before the
    migration every score is a legacy entry, and a freshness line that
    ignored those would report zero of everything on an install holding a
    thousand of them. `undated` is split OUT of stale deliberately — an
    entry with no stamp predates stamping, which is a different fact from
    one written against a superseded canon, and folding them would let a
    migration that failed to stamp read as a canon that had moved.
    """
    canon = canon_at(udir)
    own = per_role(udir)
    scores = load(udir)
    uniq = list({id(e): e for e in scores.values()}.values())
    own_uniq = {id(e) for e in own.values()}
    stale = current = undated = 0
    for e in uniq:
        if not (e or {}).get("scored_at"):
            undated += 1
        elif is_stale(e, canon):
            stale += 1
        else:
            current += 1
    return {
        "scores_dir": str(scores_dir(udir)),
        "canon_at": canon,
        "scored_total": len(uniq),
        "per_role": len(own_uniq),
        "legacy_only": len(uniq) - len(own_uniq),
        "scores_stale": stale,
        "scores_current": current,
        "scores_unstamped": undated,
    }


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "migrate":
        print(json.dumps(migrate(force="--force" in sys.argv), indent=1))
    else:
        print(json.dumps(status(), indent=1))
