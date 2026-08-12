"""Shared job-hunt logic: what applications.py (the catalog/universe
module) and jobboards.py (the live board poller) each carried
privately — the uid scheme, the owner-adjudication cut, score-file
loading, and the timestamp helper. It also owns the board HTTP helper
shared by the poller and jobdesc.py's single-posting reader.

Deliberately NOT unified: the two role _norm() shapes. applications
normalizes for the catalog UI (comp_kind/family/apply_url), jobboards
for the boards snapshot (comp/function/apply) — different consumers
read each, so collapsing them is a behavior risk deferred to a
daylight session.
"""
import re
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- http

# Board APIs answer a default python UA with a challenge page often enough
# that the browser UA is load-bearing, not politeness.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HTTP_TIMEOUT = 30


def http_get(url, as_json=True, headers=None, timeout=HTTP_TIMEOUT):
    """The one outbound GET the job-hunt pair makes. `requests` is imported
    HERE rather than at module scope so a machine without it fails on the
    first fetch instead of at server startup."""
    import requests
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json() if as_json else r.text


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

# Frontier teardown specials: the corpora's historical scheme (a-<id>
# Anthropic Greenhouse, o-<uuid> OpenAI Ashby).
#
# CONVERGED 2026-07-29. These used to apply on the URL-derivation side
# ONLY, so a registered anthropic/openai board would have minted
# g-anthropic-<id> against a universe holding a-<id> — the same posting
# under two names, joining to nothing. The stated risk of converging was
# re-minting every role on an already-registered board as "new" (a ping
# storm); it did not apply, because neither board had ever been
# registered. What the split actually cost: the owner's two anchor
# companies could not be polled at all, so 181 universe roles — every one
# of his eight picks among them — had no line to a live board and no way
# to learn a posting had closed. One spelling now, both sides.
FRONTIER = {("greenhouse", "anthropic"): "a", ("ashby", "openai"): "o"}

GH_URL = re.compile(r"greenhouse\.io/([a-z0-9_-]+)/jobs/(\d+)")
ASHBY_URL = re.compile(r"ashbyhq\.com/([a-z0-9_-]+)/([0-9a-f-]{36})")


def board_uid(ats, jid, org=""):
    """Fetcher-side uid for a board role (stable across polls)."""
    special = FRONTIER.get((ats, (org or "").lower()))
    if special:
        return f"{special}-{jid}"
    p = ATS_PREFIX[ats]
    return f"{p}-{org}-{jid}" if org else f"{p}-{jid}"


def uid_prefix(ats, org=""):
    """The uid namespace a board mints into — `a-`, `g-scaleai-`, `ms-`.
    What lets a sweep ask "which known roles does this board own?" and so
    close a posting the snapshot never held (a universe role carried over
    from a frozen teardown corpus, which is most of them)."""
    special = FRONTIER.get((ats, (org or "").lower()))
    if special:
        return f"{special}-"
    p = ATS_PREFIX[ats]
    return f"{p}-{org}-" if org else f"{p}-"


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
    return board_uid(ats, jid, org)


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
    """Every score this install holds, as one {uid: entry} map — the per-role
    store first, the legacy `*-raw-scores.json` batch files only for uids it
    does not carry.

    THE MERGE MOVED, THE NAME DID NOT. This used to be the whole reader: it
    globbed the batch files and merged them in sorted-filename order, later
    file winning. That order is a trap for anything that re-scores a role
    (digits sort before letters, so a freshly dated file loses to `v2-*` and
    `swarm-*`), so precedence now lives in server/jobscores.py where it is
    stated rather than inferred. Roughly a dozen callers say `load_scores`
    and mean "every score" — they keep saying it.
    """
    from . import jobscores
    return jobscores.load(udir)
