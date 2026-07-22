"""Shared job-hunt logic: what applications.py (the catalog/universe
module) and jobboards.py (the live board poller) each carried
privately — the uid scheme, the owner-adjudication cut, score-file
loading, and the timestamp helper.

Deliberately NOT unified: the two role _norm() shapes. applications
normalizes for the catalog UI (comp_kind/family/apply_url), jobboards
for the boards snapshot (comp/function/apply) — different consumers
read each, so collapsing them is a behavior risk deferred to a
daylight session.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------- uid scheme

# ATS -> uid prefix: the one table both sides read. Fetcher-side uids
# are <prefix>-<org>-<id> for slug boards and <prefix>-<id> for query
# boards (microsoft/google carry no org slug).
ATS_PREFIX = {
    "greenhouse": "g",
    "ashby": "as",
    "lever": "lv",
    "microsoft": "ms",
    "google": "gg",
}

# Frontier teardown specials — URL-derivation side ONLY, matching the
# corpora's historical scheme (a-<id> Anthropic Greenhouse, o-<uuid>
# OpenAI Ashby). board_uid deliberately does NOT apply these: fetcher
# uids key the boards snapshot/state, and changing them for a
# registered anthropic/openai board would re-mint every role as "new"
# (ping storm). Converging the two spellings is a daylight decision.
FRONTIER = {("greenhouse", "anthropic"): "a", ("ashby", "openai"): "o"}

GH_URL = re.compile(r"greenhouse\.io/([a-z0-9_-]+)/jobs/(\d+)")
ASHBY_URL = re.compile(r"ashbyhq\.com/([a-z0-9_-]+)/([0-9a-f-]{36})")


def board_uid(ats, jid, org=""):
    """Fetcher-side uid for a board role (stable across polls)."""
    p = ATS_PREFIX[ats]
    return f"{p}-{org}-{jid}" if org else f"{p}-{jid}"


def url_uid(url):
    """Posting URL -> the shared uid scheme (with the frontier
    specials), or None when the URL matches no known ATS."""
    m = GH_URL.search(url or "")
    if m:
        return _org_uid("greenhouse", *m.groups())
    m = ASHBY_URL.search(url or "")
    if m:
        return _org_uid("ashby", *m.groups())
    return None


def _org_uid(ats, org, jid):
    p = FRONTIER.get((ats, org))
    return f"{p}-{jid}" if p else board_uid(ats, jid, org)


# ------------------------------------------------------- adjudication cut

def cut_reason(comp_kind, title, adj):
    """The owner-adjudication cut, one implementation for both sides:
    by comp structure (`ote` = quota comp), then title pattern — NEVER
    the board's function label (it files base-comp deployment roles
    under 'Sales & Go-To-Market'). Returns the reason string, or ""
    when the role survives. adj is applications._load_adjudication's
    shape; falsy adj never cuts."""
    if not adj:
        return ""
    if comp_kind in adj["cut_comp"]:
        return adj["reason_comp"]
    for pat in adj["cut_titles"]:
        if pat.search(title or ""):
            return adj["reason_title"]
    return ""


# ------------------------------------------------------------ score files

def load_scores(udir):
    """Every *-raw-scores.json under the universe dir (v2 + d6 + future
    passes) as one {uid: entry} map, double-indexed by uid AND _fulluid
    (some entries carry a truncated uid with the full board uuid in
    _fulluid, so every keeper matches its role file). Files merge in
    sorted-name order; later files win uid collisions. A corrupt file
    is skipped, never fatal."""
    scores = {}
    for sf in sorted(Path(udir).glob("*-raw-scores.json")):
        try:
            for s in json.loads(sf.read_text()):
                for k in (s.get("uid"), s.get("_fulluid")):
                    if k:
                        scores[k] = s
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return scores
