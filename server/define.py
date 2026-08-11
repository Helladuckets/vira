"""On-demand term definitions — a ladder that erases its own rungs.

Right-click any selected text in Vira and a definition card opens.  The card
is composed by the cheapest rung that can answer:

    rung 0  glossary   an existing term note in the vault      ~1ms, no model
    rung 1  atlas      the curated AI-terminology atlas        ~1ms, no model
    rung 2  vault      a note whose title IS the term          ~5ms, no model
    rung 3  model      one suggest.complete over gathered context   2-4s
    rung 4  sourced    a live session that browses and cites   on demand only

THE POINT OF THE WHOLE MODULE is that rung 3 and rung 4 write their result
back into the vault as an ordinary `type: concept` page.  So the SECOND
lookup of a term answers from rung 0, the wiki gains a page, Find's index
picks it up within a scan, and the term joins the graph through `## Related`
wikilinks in both directions.  A climb is paid once.

Why the note is the source of truth and `data/glossary.json` is only an
index: the note is the artifact the owner actually wants and can edit in
Obsidian, and a stored copy of its prose would go stale the moment he did.
The index carries provenance and a pointer, and the card is re-read from the
note — the readinglist soft-pointer rule.

What rung 3 may and may not produce is the load-bearing distinction.  A model
reliably knows what a term MEANS, what it is distinct from, and roughly where
it came from; that is stable knowledge and it is what a dictionary is.  It
does not reliably know a URL, a date, or who said it first.  So rung 3 emits
prose and NO links, and `_validate` strips any it invents.  Only rung 4,
which actually browses, may write the `links` list.
"""
import json
import os
import re
from datetime import date
from pathlib import Path

from . import atlasterms, filelock, jsonstore, settings, suggest, vault

STORE = Path(__file__).resolve().parent.parent / "data" / "glossary.json"
LOCK = Path(__file__).resolve().parent.parent / "data" / "glossary.build"

WIKI_SUBDIR = "wiki"
NOTE_TYPE = "concept"
MAX_TERM = 120
MAX_VALUE = 1200
MAX_RELATED = 12
MAX_CONTEXT_NOTES = 5
MAX_CONTEXT_CHARS = 1800
# A selection long enough to be a sentence is not a term. Defining it would
# be answering a question nobody asked, slowly.
MAX_SELECTION_WORDS = 8

# The spine every card carries, whatever the domain. Domain-specific rows
# (cohort tilt, hype scores, a Vira example) ride along only where they mean
# something — a legal term has no hype cycle, and inventing a number for one
# is the failure this schema exists to avoid.
SPINE = [
    ("plain_definition", "Plain definition"),
    ("technical_definition", "Technical definition"),
    ("distinctions", "Synonyms and distinctions"),
    ("lineage", "Etymology and lineage"),
    ("current_usage", "Current usage"),
    ("trajectory", "Trajectory"),
    ("confusion_risk", "Confusion risk"),
    ("verdict", "Vocabulary verdict"),
]
SPINE_KEYS = {k for k, _ in SPINE}
LABELS = dict(SPINE)
REQUIRED = ("plain_definition",)

RELATED_LABEL = "Related"
LINKS_LABEL = "Read further"

URL_RE = re.compile(r"https?://\S+")
_WS = re.compile(r"\s+")


class DefineError(RuntimeError):
    pass


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def slugify(text, cap=72):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(s) > cap:
        s = s[:cap].rsplit("-", 1)[0] or s[:cap]
    return s.strip("-") or "term"


def clean_term(raw):
    """A selection reduced to a term, or '' when it is not one."""
    t = _WS.sub(" ", (raw or "").strip().strip("\"'“”‘’.,;:!?()[]{}"))
    if not t or len(t) > MAX_TERM:
        return ""
    if len(t.split()) > MAX_SELECTION_WORDS:
        return ""
    return t


# ------------------------------------------------------------------- store

def _blank():
    return {"version": 1, "terms": {}}


def index():
    return jsonstore.read(STORE, _blank())


def entry(term):
    return (index().get("terms") or {}).get(_norm(term))


# -------------------------------------------------------------- note shape

def wiki_dir():
    return Path(vault.vault_root()) / WIKI_SUBDIR


def _yaml_str(v):
    return json.dumps(str(v), ensure_ascii=False)


def _existing_stems(root):
    """Every note filename in the vault, mapped to its vault-relative path.

    Obsidian resolves [[link]] by FILENAME across directories, so a new slug
    must never shadow one — and for the same reason the value here is a path,
    not just the file: 5,635 stems in this vault are owned by more than one
    note, so writing a link by stem alone leaves the reader to arbitrate.
    """
    stems = {}
    root = Path(root)
    try:
        for p in root.rglob("*.md"):
            try:
                stems.setdefault(p.stem, p.relative_to(root))
            except ValueError:
                continue
    except OSError:
        pass
    return stems


def _link(term, stems):
    """`[[wiki/slug|Term]]` when the note exists, plain text when it does not.

    A wikilink that resolves nowhere reads as a page you have and leads to a
    blank — worse than plain text, which is honest about being a loose end.
    The path is carried so the link names ONE file; a bare slug is arbitrated
    differently by Obsidian (linking note's folder) than by this app
    (DIR_RANK), which is the whole reason links are written qualified now.
    """
    s = slugify(term)
    hit = stems.get(s)
    if hit is None:
        return term
    p = Path(hit).as_posix()
    p = p[:-3] if p.lower().endswith(".md") else p
    return f"[[{p}|{term}]]"


def note_text(card, stems, created=None):
    """Render one term note as a TC-IL `type: concept` page.

    Pure, so the writer can diff it against disk before touching the file.
    """
    term = card["term"]
    today = date.today().isoformat()
    tags = ["glossary", "vira-definition", slugify(term, cap=64)]
    if card.get("family"):
        tags.append(slugify(card["family"], cap=64))
    seen, uniq = set(), []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)

    fm = [
        "---",
        f"title: {_yaml_str(term)}",
        f"type: {NOTE_TYPE}",
        f"tags: [{', '.join(uniq)}]",
        f"created: {created or today}",
        f"updated: {today}",
        # The exact machine join key. Slugs collide and titles get edited;
        # this is what `_index_by_term` reads back, the room_item_id pattern.
        f"vira_term: {_yaml_str(term)}",
        f"vira_rung: {card.get('rung', 'model')}",
        f"vira_sourced: {'true' if card.get('sourced') else 'false'}",
    ]
    if card.get("source"):
        fm.append(f"vira_source: {_yaml_str(card['source'])}")
    fm.append("sources: []")
    fm.append("source_count: 0")
    fm.append("---")

    body = ["", f"# {term}", ""]
    for row in card.get("rows") or []:
        if not row.get("value"):
            continue
        body += [f"## {row.get('label') or LABELS.get(row['key'], row['key'])}",
                 "", row["value"], ""]

    related = card.get("related") or []
    if related:
        body += [f"## {RELATED_LABEL}", ""]
        body += [f"- {_link(r, stems)}" for r in related]
        body.append("")

    links = card.get("links") or []
    if links:
        body += [f"## {LINKS_LABEL}", ""]
        body += [f"- [{l['label']}]({l['url']})" for l in links
                 if l.get("url")]
        body.append("")
    return "\n".join(fm + body).rstrip() + "\n"


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _frontmatter(text):
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "-", "\t")):
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return fm, text[m.end():]


def parse_note(text):
    """A term note read back into a card. The inverse of `note_text`."""
    fm, body = _frontmatter(text)
    term = fm.get("vira_term") or fm.get("title") or ""
    rows, related, links = [], [], []
    by_label = {lb: k for k, lb in SPINE}
    for chunk in re.split(r"^##\s+", body, flags=re.M)[1:]:
        head, _, rest = chunk.partition("\n")
        label = head.strip()
        value = rest.strip()
        if label == RELATED_LABEL:
            for line in value.splitlines():
                m = re.match(r"-\s*(?:\[\[([^\]|]+)(?:\|([^\]]+))?\]\]|(.+))",
                             line.strip())
                if m:
                    related.append((m.group(2) or m.group(1)
                                    or m.group(3) or "").strip())
        elif label == LINKS_LABEL:
            for line in value.splitlines():
                m = re.match(r"-\s*\[([^\]]*)\]\((\S+)\)", line.strip())
                if m:
                    links.append({"label": m.group(1), "url": m.group(2)})
        elif value:
            rows.append({"key": by_label.get(label, slugify(label, cap=40)),
                         "label": label, "value": value})
    return {
        "term": term,
        "rung": fm.get("vira_rung") or "vault",
        "source": fm.get("vira_source") or "",
        "sourced": (fm.get("vira_sourced") or "").lower() == "true",
        "rows": rows,
        "related": [r for r in related if r],
        "links": links,
    }


def _index_by_term(root):
    """{normalized term -> path} for every term note already in the vault."""
    found = {}
    try:
        paths = sorted(Path(root).rglob("*.md"))
    except OSError:
        return found
    for p in paths:
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:1200]
        except OSError:
            continue
        m = re.search(r"^vira_term:\s*(.+?)\s*$", head, re.M)
        if m:
            found.setdefault(_norm(m.group(1).strip().strip('"')), p)
    return found


# ------------------------------------------------------------------- rungs

def _from_vault_note(path):
    try:
        return parse_note(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def _title_match(term):
    """A vault note whose own title IS this term — the vault defining it.

    Deliberately narrow. A note that MENTIONS a term forty times still does
    not define it, so a body hit feeds rung 3 as context instead of
    answering here and passing an aside off as a definition.
    """
    root = Path(vault.vault_root())
    if not root.exists():
        return None
    slug = slugify(term)
    for cand in (root / WIKI_SUBDIR / f"{slug}.md", root / f"{slug}.md"):
        if cand.exists():
            card = _from_vault_note(cand)
            if card and any(r.get("value") for r in card.get("rows") or []):
                card["term"] = card.get("term") or term
                card["rung"] = card.get("rung") or "vault"
                card["note"] = str(cand)
                return card
    return None


def _context(term):
    """Vault passages about the term, to seed rung 3. Never the answer."""
    out = []
    try:
        hits = vault.search(term, limit=MAX_CONTEXT_NOTES) or []
    except Exception:
        return out
    for h in hits[:MAX_CONTEXT_NOTES]:
        text = (h.get("text") or h.get("chunk") or "").strip()
        if text:
            out.append({"path": h.get("path") or "",
                        "text": text[:MAX_CONTEXT_CHARS]})
    return out


PROMPT = """You write one dictionary card for a term the owner selected in his \
personal assistant app. Return ONLY a JSON object.

TERM: {term}
{context}
Fields (all strings unless noted):
- plain_definition: one sentence a smart non-specialist understands. REQUIRED.
- technical_definition: what it precisely denotes to a practitioner.
- distinctions: the terms it is confused with, and the actual difference.
- lineage: where the term came from and how its meaning has drifted. If you \
do not know a specific origin, say what is known about its usage instead. \
Never name a specific person, paper, or year you are not sure of.
- current_usage: who uses it now and for what.
- trajectory: where the term is heading.
- confusion_risk: how badly it is misused, and the safer narrower word.
- verdict: one line on when to use it and when to reach for something else.
- related: a JSON array of up to {max_related} closely related term names \
(names only, no descriptions).
- domain: one or two words for the field this term belongs to.

RULES
- NO URLs, citations, dates-as-facts, or "according to" anywhere. You cannot \
verify a link here, and a fabricated one is worse than none. Sourcing is a \
separate step the owner triggers.
- If the term is ambiguous, define the sense the context supports, and say so \
in one clause.
- Omit a field entirely rather than padding it.
- Plain ASCII punctuation. No emojis. No markdown."""


def _extract_json(text):
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"\A```[a-z]*\n|\n```\Z", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise DefineError("the model returned no JSON object")
    return json.loads(raw[start:end + 1])


def _clean_value(v):
    if not isinstance(v, str):
        return ""
    # Rung 3 may not cite. Strip rather than reject: the prose around an
    # invented link is usually fine, and the link is the only unsafe part.
    return URL_RE.sub("", v).strip()[:MAX_VALUE].strip()


def _validate(raw, term):
    rows = []
    for key, label in SPINE:
        v = _clean_value(raw.get(key))
        if v:
            rows.append({"key": key, "label": label, "value": v})
    if not any(r["key"] in REQUIRED for r in rows):
        raise DefineError("the model returned no usable definition")
    related, seen = [], {_norm(term)}
    for r in raw.get("related") or []:
        name = clean_term(r if isinstance(r, str) else "")
        if name and _norm(name) not in seen:
            seen.add(_norm(name))
            related.append(name)
    return {
        "term": term,
        "rung": "model",
        "source": "",
        "sourced": False,
        "family": _clean_value(raw.get("domain"))[:60],
        "rows": rows,
        "related": related[:MAX_RELATED],
        "links": [],
    }


def _compose(term, context):
    block = ""
    if context:
        block = ("\nFROM THE OWNER'S OWN NOTES (use where it sharpens the "
                 "card; ignore where irrelevant):\n"
                 + "\n".join(f"- {c['text']}" for c in context) + "\n")
    raw = suggest.complete(PROMPT.format(term=term, context=block,
                                         max_related=MAX_RELATED))
    return _validate(_extract_json(raw), term)


# ------------------------------------------------------------- write-back

def _passive():
    return bool(os.environ.get("VIRA_PASSIVE"))


def _write_note(path, text):
    """Preserve `created:`, and make an unchanged run a true no-op."""
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^created:\s*(\S+)\s*$", old, re.M)
        if m:
            text = re.sub(r"^created:.*$", f"created: {m.group(1)}",
                          text, count=1, flags=re.M)
        strip = lambda t: re.sub(r"^updated:.*$", "", t, count=1, flags=re.M)
        if strip(old) == strip(text):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def _backlink(path, term, slug, ref=None):
    """Add `[[wiki/slug|term]]` to an existing term note's Related list.

    This is the half that makes the graph grow in BOTH directions: writing
    "context engineering" should make the older "prompt engineering" note
    point at it, not just the other way round. Bounded on purpose — only
    term notes, only the Related section, never the body prose.

    `ref` is the vault-relative path to link by; `slug` remains the bare stem
    so the already-there check still recognises links written before links
    were qualified, and stays idempotent across the change.
    """
    ref = ref or slug
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if f"[[{ref}" in text or f"[[{slug}]" in text or f"[[{slug}|" in text:
        return False
    if not re.search(r"\b" + re.escape(term) + r"\b", text, re.I):
        return False
    line = f"- [[{ref}|{term}]]"
    m = re.search(r"^##\s+" + re.escape(RELATED_LABEL) + r"\s*$", text, re.M)
    if m:
        insert = text.index("\n", m.end()) + 1
        while text[insert:insert + 1] == "\n":
            insert += 1
        text = text[:insert] + line + "\n" + text[insert:]
    else:
        text = text.rstrip() + f"\n\n## {RELATED_LABEL}\n\n{line}\n"
    text = re.sub(r"^updated:.*$", f"updated: {date.today().isoformat()}",
                  text, count=1, flags=re.M)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def save(card):
    """Write the card into the vault and index it. Returns the card, with
    `note` set. This is what makes the next lookup free."""
    if _passive():
        raise DefineError(
            "passive instance: vault_root is outside the cloned data/, so "
            "this would write the live vault. Refusing.")
    root = Path(vault.vault_root())
    if not root.exists():
        raise DefineError(f"vault root does not exist: {root}")
    term = card["term"]
    with filelock.locked(LOCK):
        known = _index_by_term(root)
        path = known.get(_norm(term))
        stems = _existing_stems(root)
        if path is None:
            base = slugify(term)
            cand = base
            n = 0
            # Never shadow an unrelated note that happens to share the slug.
            while cand in stems:
                n += 1
                cand = f"{base}-term" if n == 1 else f"{base}-term-{n}"
            path = wiki_dir() / f"{cand}.md"
        _write_note(path, note_text(card, stems))
        slug = path.stem
        try:
            ref = path.relative_to(root).with_suffix("").as_posix()
        except ValueError:
            ref = slug
        linked = 0
        for other_term, other_path in known.items():
            if other_path != path and _backlink(other_path, term, slug, ref):
                linked += 1
        def _record(st):
            terms = st.setdefault("terms", {})
            prev = terms.get(_norm(term)) or {}
            terms[_norm(term)] = {
                "term": term,
                "slug": slug,
                "path": str(path),
                "rung": card.get("rung", "model"),
                "sourced": bool(card.get("sourced")),
                "updated": date.today().isoformat(),
                "hits": prev.get("hits", 0),
            }

        jsonstore.mutate(STORE, _record, _blank())
    card = dict(card)
    card["note"] = str(path)
    card["slug"] = slug
    card["backlinks"] = linked
    return card


def _bump(term):
    def _hit(st):
        ent = (st.get("terms") or {}).get(_norm(term))
        if ent is not None:
            ent["hits"] = ent.get("hits", 0) + 1

    jsonstore.mutate(STORE, _hit, _blank())


# ------------------------------------------------------------------ ladder

def lookup(term, write=True):
    """The card for `term`, by the cheapest rung that can answer.

    `write` is False on a passive instance and in tests that must not touch
    the vault; the card still comes back, it simply is not banked.
    """
    term = clean_term(term)
    if not term:
        raise DefineError("that selection is not a term")

    # rung 0 — already banked
    ent = entry(term)
    if ent and Path(ent["path"]).exists():
        card = _from_vault_note(Path(ent["path"]))
        if card and card.get("rows"):
            card["term"] = card.get("term") or term
            card["note"] = ent["path"]
            card["slug"] = ent.get("slug", "")
            card["cached"] = True
            _bump(term)
            return card

    # rung 1 — the curated atlas
    hit = atlasterms.lookup(term)
    if hit:
        card = dict(hit)
        if write and not _passive():
            try:
                card = save(card)
            except DefineError:
                pass                          # a card is still worth showing
        return card

    # rung 2 — the vault already has a page titled this
    card = _title_match(term)
    if card:
        return card

    # rung 3 — one model call, seeded with whatever the vault knows
    card = _compose(term, _context(term))
    if write and not _passive():
        try:
            card = save(card)
        except DefineError as e:
            card["write_error"] = str(e)
    return card


SOURCE_PROMPT = """You are upgrading one glossary card in the owner's vault \
from unsourced prose to a sourced reference. The term is:

    {term}

The card currently lives at:

    {path}

Do this:
1. Read that note.
2. Research the term on the web. Find the ACTUAL origin (the paper, post, \
talk, or spec that introduced it or first used it in its current sense) and \
one or two current authoritative uses.
3. Correct any claim in the note that your research contradicts — especially \
in "Etymology and lineage", which was written from memory and is the field \
most likely to be wrong.
4. Rewrite the note, keeping its existing structure exactly: the same \
frontmatter fields, the same `## ` section headings in the same order. \
Set `vira_rung: sourced` and `vira_sourced: true`.
5. Add a `## Read further` section (or replace the existing one) as a \
markdown list of `- [label](url)` links. EVERY url must be one you actually \
fetched and confirmed resolves. Omit a link you could not verify rather than \
guessing; a dead citation is the whole reason this step exists.
6. Leave the `## Related` section's wikilinks alone.

Do not create other notes. Do not restart the Vira server. Finish with a \
two-line report: what you corrected, and how many links you verified."""


def source_prompt(term):
    ent = entry(term) or {}
    return SOURCE_PROMPT.format(
        term=term,
        path=ent.get("path") or f"(not yet written; look under "
                                f"{wiki_dir()} for {slugify(term)}.md)")


def status():
    st = index()
    return {
        "terms": len(st.get("terms") or {}),
        "atlas": atlasterms.status(),
        "vault": str(vault.vault_root()),
        "passive": _passive(),
    }
