"""Job descriptions — the posting, readable inside Vira.

The owner's question was "was there a reason why Anthropic job
descriptions couldn't open in a native Vira window?", and the honest
answer is no: the module had the text the whole time and never showed
it. A role's title was an `<a target="_blank">`, so reading what a job
actually said meant leaving Vira for the employer's own page — cookie
banner, trackers, apply widget — while `jobboards.py` had already pulled
the full description into the boards snapshot on its last sweep (up to
JD_CAP characters, for the scoring sessions to deep-read).

And what was stored could not have been rendered anyway. `_strip_html`
unescaped entities AFTER stripping tags, which is exactly backwards for
Greenhouse: its API escapes the description inside the JSON string, so
no tag matched the strip and the markup survived as literal text
(`<div class="content-intro"><h2><strong>About Anthropic</strong>...`).
Every other board fared no better — a real HTML body came out as one
10,000-character line with every paragraph and bullet gone.

So this module is two things:

- **`to_markdown()` — the one converter**, and the reason the panel
  reads like a document. It takes all three shapes the corpus holds
  (entity-escaped HTML, real HTML, already-flattened prose), keeps the
  headings and bullets a job description is MADE of, and hands the rest
  to `mailread.html_to_text`, which is the house structure-preserving
  html->text pass (`mail.strip_html`, its flattening sibling, is
  preview-only for the same reason `_strip_html` was). `jobboards`
  routes through it too, so from the next sweep the snapshot stores
  something a person — or a scoring session — could read.
- **`describe()` — the ladder**, cheapest first: the boards snapshot
  (already on disk: no network, and no visit to the employer's
  analytics), then a cached live fetch, then a live per-posting fetch
  for the ATSes that have a cheap one, then the blurb. Ashby publishes
  no single-posting endpoint (401; its board API is one 12MB document)
  and google/microsoft are search endpoints, so for those the snapshot
  IS the answer — and the payload says how old it is rather than
  implying Vira just checked.

Nothing here writes the world, so a passive instance serves it in full:
reading a public job posting is not acting on anything.
"""
import html as htmllib
import re
from pathlib import Path

from . import jobshared, jsonstore, mailread

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "job-descriptions.json"

TEXT_CAP = 60_000   # a posting far past any real one; a guard, not a budget
MAX_CACHED = 60     # live fetches only — the snapshot is re-read, never cached
TIMEOUT = 20

# The ATSes with a public per-posting endpoint. Ashby is deliberately
# absent (see the module docstring), as are the query-only boards.
LEVER_URL = re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})", re.I)


# ------------------------------------------------------------- conversion

_TAG = re.compile(r"<[A-Za-z/!][^>]*>")
_ESCAPED_TAG = re.compile(r"&lt;\s*/?[A-Za-z]")
_HEAD = re.compile(r"(?is)<h([1-6])[^>]*>(.*?)</h\1\s*>")
_STRONG = re.compile(r"(?is)<(?:strong|b)[^>]*>(.*?)</(?:strong|b)\s*>")
_LI_BLOCK = re.compile(r"(?is)<li[^>]*>(.*?)</li\s*>")
_LI_OPEN = re.compile(r"(?i)<li[^>]*>")
_LIST_END = re.compile(r"(?i)</(?:ul|ol)\s*>")


def _unescape_markup(raw):
    """Recover markup that arrived escaped inside a JSON string.

    Greenhouse sends `&lt;p&gt;`, so one unescape has to happen BEFORE
    any tag can be recognised. Ashby and Lever send real HTML, where the
    guard below sees tags and does nothing. Bounded at two passes: enough
    for the one real double-escape, and short of turning prose that
    merely mentions `&lt;` into markup forever.
    """
    text = raw or ""
    for _ in range(2):
        if _TAG.search(text) or not _ESCAPED_TAG.search(text):
            break
        text = htmllib.unescape(text)
    return text


def _inline(frag):
    """A heading's own text: tags out, entities resolved, one line."""
    return re.sub(r"\s+", " ",
                  htmllib.unescape(re.sub(r"<[^>]+>", " ", frag or ""))).strip()


_BULLET_GAP = re.compile(r"(?m)^(- .*)\n\n+(?=- )")


def _tidy(text):
    """Drop the markers that survived an empty element, close the gaps
    between bullets, and cap the blank runs.

    A bare '-' or '##' renders as a bullet or heading with nothing in it,
    which reads as a broken document rather than an empty one. The bullet
    gap matters more than it looks: the renderer starts a list at the
    first '- ' line and ends it at the first line that is not one, so a
    blank line between two items is EIGHT separate one-item lists rather
    than one list of eight.
    """
    lines = [ln for ln in text.split("\n")
             if ln.strip() not in ("-", "*", "#", "##", "###")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return _BULLET_GAP.sub(r"\1\n", text).strip()


def to_markdown(raw):
    """A posting's description -> the small markdown subset the panel
    renders (`## heading`, `- bullet`, blank-line paragraphs).

    Headings and list items are turned into their markers BEFORE the
    generic pass, because that pass can only see tags as line breaks —
    it has no way to know a `</li>` was a bullet and an `</h2>` was a
    section title, and a job description is almost entirely those two.
    """
    text = _unescape_markup(raw)
    text = _HEAD.sub(lambda m: "\n\n## " + _inline(m.group(2)) + "\n\n", text)
    # A list item is flattened to ONE line by design. Boards wrap items in
    # their own <p>, and the generic pass turns every </p> into a blank
    # line — which splits what the employer wrote as one list into a
    # separate list per bullet. The bare-open sub behind it catches the
    # unclosed <li> that sloppier boards emit.
    text = _LI_BLOCK.sub(lambda m: "\n- " + _inline(m.group(1)) + "\n", text)
    text = _LI_OPEN.sub("\n- ", text)
    text = _LIST_END.sub("\n\n", text)
    # Not decoration: several boards (Ashby among them) mark their section
    # titles with a bold paragraph rather than an <h> tag, so without this
    # every section of an OpenAI posting reads as undifferentiated prose.
    # Collapsed to one line because the renderer resolves emphasis per line.
    text = _STRONG.sub(
        lambda m: f"**{_inline(m.group(1))}**" if _inline(m.group(1)) else "",
        text)
    return _tidy(mailread.html_to_text(text))[:TEXT_CAP]


# ------------------------------------------------------------ live fetch

def live_target(url):
    """(ats, org, posting id) for a URL Vira can fetch a single posting
    from, else None. Reads the URL rather than the role's board record,
    so a role carried over from a frozen teardown corpus — one whose
    company was never registered as a board — is fetchable too."""
    m = jobshared.GH_URL.search(url or "")
    if m:
        return ("greenhouse",) + m.groups()
    m = LEVER_URL.search(url or "")
    if m:
        return ("lever",) + m.groups()
    return None


def fetch_live(url):
    """The description as the board serves it right now, or "" when this
    posting has no cheap single fetch. Raises on a network/HTTP failure —
    the caller decides whether that is fatal or just the end of a rung."""
    target = live_target(url)
    if not target:
        return ""
    ats, org, jid = target
    if ats == "greenhouse":
        data = jobshared.http_get(
            f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{jid}",
            timeout=TIMEOUT)
        return to_markdown(data.get("content") or "")
    data = jobshared.http_get(
        f"https://api.lever.co/v0/postings/{org}/{jid}?mode=json",
        timeout=TIMEOUT)
    parts = [data.get("description") or ""]
    parts += [f"<h3>{c.get('text') or ''}</h3>{c.get('content') or ''}"
              for c in (data.get("lists") or [])]
    parts.append(data.get("additional") or "")
    return to_markdown("".join(p for p in parts if p))


# ----------------------------------------------------------------- cache

def _cache_read():
    store = jsonstore.read(STORE, {})
    items = store.get("items") if isinstance(store, dict) else None
    return items if isinstance(items, dict) else {}


def _cache_put(uid, entry):
    def apply(s):
        items = s.setdefault("items", {})
        items[uid] = entry
        if len(items) > MAX_CACHED:
            # prune_oldest sorts by VALUE, and these values are dicts, so
            # the cap is local (the lessonwatch precedent)
            for k in sorted(items, key=lambda k: items[k].get("fetched") or "")[
                    :len(items) - MAX_CACHED]:
                items.pop(k, None)
    jsonstore.mutate(STORE, apply, {"items": {}}, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- ladder

def _snapshot(uid):
    """(description, sweep stamp) from the boards snapshot."""
    from . import jobboards
    rec, fetched = jobboards.snapshot_role(uid)
    return ((rec or {}).get("jd") or "", fetched)


def _live_rung(uid, url, out):
    """Fetch, cache and shape the live rung — or record why it failed and
    let the caller fall through to a cheaper one. A posting Vira cannot
    reach is a reason to show the sweep's copy, never to show nothing."""
    try:
        text = fetch_live(url)
    except Exception as e:  # noqa: BLE001 — any network/HTTP failure
        out["reason"] = f"could not reach the posting ({e.__class__.__name__})"
        return None
    if not text:
        return None
    entry = {"text": text, "fetched": jobshared.now_iso(), "url": url}
    _cache_put(uid, entry)
    return {**out, "text": text, "source": "live",
            "as_of": entry["fetched"], "reason": ""}


def describe(role, refresh=False):
    """Everything the description panel needs for one role.

    `source` names the rung that answered so the panel can SAY it: a
    sweep's text carries the date of that sweep, a live fetch says it is
    current, and a blurb says outright that it is an opening excerpt and
    not the posting. `live` reports whether a refresh could do better, so
    the panel never offers a button that would only re-run the rung it
    already has.
    """
    uid = role.get("uid") or ""
    url = role.get("url") or role.get("apply_url") or ""
    out = {"uid": uid, "url": url, "title": role.get("title") or "",
           "company": role.get("company") or "", "text": "", "source": "",
           "as_of": "", "partial": False, "reason": "",
           "live": bool(live_target(url))}

    if refresh and out["live"]:
        hit = _live_rung(uid, url, out)
        if hit:
            return hit

    stored, swept = _snapshot(uid)
    text = to_markdown(stored)
    if text:
        return {**out, "text": text, "source": "snapshot", "as_of": swept}

    cached = _cache_read().get(uid) or {}
    if cached.get("text"):
        return {**out, "text": cached["text"], "source": "live",
                "as_of": cached.get("fetched") or ""}

    if out["live"] and not refresh:
        hit = _live_rung(uid, url, out)
        if hit:
            return hit

    blurb = to_markdown(role.get("blurb") or "")
    if blurb:
        return {**out, "text": blurb, "source": "blurb", "partial": True}

    if not out["reason"]:
        out["reason"] = ("no sweep has stored this posting's description, and "
                         "its board publishes no single-posting API"
                         if not out["live"] else
                         "no description could be fetched for this posting")
    return out
