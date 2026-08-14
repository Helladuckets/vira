"""The employer's own page in the vault, resolved and measured for one
application.

A cover letter's whole job is to connect this career to THIS company — what
they do, what they say they are for, and why the owner wants to be there.
Until now the only company context an Apply dispatch carried was Anthropic's:
the skill named `canon/targets/anthropic.md` by hand and `research.py`'s graph
catalog holds exactly one row, so a letter for any of the other ten companies
in the catalog was written against the job description alone.

The material already exists. The vault holds 124 `type: entity` pages, and the
catalog's employers are among them. This module RESOLVES a company name to
that page and MEASURES it.

It never writes. Expanding a thin page is research and prose, done in the
vault under the vault's own conventions — the fullingest split, where staging
is deterministic and synthesis is a model's job. Vira's job here is to name
the path and the gap honestly, so a session cannot mistake "no page" for
"nothing to say".

THE RESOLUTION RUNG IS ALWAYS REPORTED. A page about Microsoft is not a page
about Microsoft AI, and presenting one as the other is the wikilink-resolution
failure in a different costume: a plausible wrong answer is worse than an
honest miss, because the letter it produces reads as tailored while being
about the wrong company.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import atlasvault, vault

WIKI_SUBDIR = "wiki"

# Read as a prompt, never as a verdict — the payload states every number so a
# session can disagree with them against the role in front of it.
#
# Size is the weakest of the three and it is deliberately not alone. Measured
# across the catalog's own eleven employers, COVERAGE of the sections a letter
# draws on is what actually separates a company page from a vault-navigation
# page: every strong page (anthropic, openai, xai, google-deepmind) answers
# exactly 3 of the 4, and every weak one answers 1 or 0. Nothing sits at 2, so
# MIN_COVERS is the corpus's own natural break rather than a chosen floor.
# It is what catches palantir.md — 9,785 bytes over 5 sections, comfortably
# past any size floor, and made of "Vault holdings" and "Cross-references",
# which cannot ground a single sentence about why the owner wants to be there.
THIN_BYTES = 6000
MIN_SECTIONS = 4
MIN_COVERS = 3

# The house shape, derived from the vault's own strong company pages
# (anthropic, openai, google-deepmind, xai) rather than invented here. An
# expansion should reach this shape so every company page answers the same
# questions in the same order.
SKELETON = (
    "What they do",
    "Mission and stated values",
    "Key products / initiatives",
    "Leadership",
    "Positions and claims",
    "Recent developments",
    "Competitive context",
    "Controversies / contradictions",
    "Related",
)

# The subset a cover letter actually draws on. "Positions and claims" is where
# the vault keeps leadership's stated views, which is the conviction beat's
# real source — values stated as history, not generic praise.
LETTER_SECTIONS = (
    "What they do",
    "Mission and stated values",
    "Positions and claims",
    "Recent developments",
)

# Stripped only at the LAST rung, after exact and slug matching have failed —
# "Scale AI" must resolve to scale-ai.md, never to a page about scales.
_PARENT_SUFFIXES = (" ai", " labs", " research", " inc", " inc.", " llc")

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
_UPDATED_RE = re.compile(r'^updated:\s*"?([0-9]{4}-[0-9]{2}-[0-9]{2})', re.M)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _norm(text):
    return " ".join(str(text or "").lower().split())


def slugify(text):
    return _SLUG_RE.sub("-", _norm(text)).strip("-")


def _sections(body):
    """The `## ` headings, deduped, in order."""
    out, seen = [], set()
    for head in _HEADING_RE.findall(body):
        key = _norm(head)
        if key and key not in seen:
            seen.add(key)
            out.append(head)
    return out


# An explicit alias table, not fuzzy matching. The vault's strong pages spell
# the same section several ways — "What they do" / "What it is" / "What it
# does", "Recent developments" / "Notable events" — and a letter does not care
# which. Every alias here was read off a real page; a clever substring rule
# instead matched "What the vault holds" as though it described the company.
_ALIASES = {
    "What they do": ("what they do", "what it is", "what it does",
                     "what they are", "overview"),
    "Mission and stated values": ("mission", "mission / positioning",
                                  "mission and values", "stated values",
                                  "strategic positioning", "positioning"),
    "Positions and claims": ("positions and claims", "positions", "claims",
                             "notable events / claims", "views"),
    "Recent developments": ("recent developments", "notable events",
                            "notable events covered in wiki", "developments",
                            "corporate history", "news"),
}


def _covers(sections):
    """Which LETTER_SECTIONS this page actually answers."""
    have = {_norm(s) for s in sections}
    hits = []
    for want in LETTER_SECTIONS:
        names = {_norm(want)} | {_norm(a) for a in _ALIASES.get(want, ())}
        if any(h == n or h.startswith(n + " ") or h.startswith(n + " (")
               for h in have for n in names):
            hits.append(want)
    return hits


def _find(company, entities):
    """(slug, rung) for the best entity page, or (None, "none").

    Rungs run cheapest and most certain first. The rung is returned, never
    swallowed, because rung 3 substitutes a DIFFERENT subject.
    """
    wanted = _norm(company)
    if not wanted:
        return None, "none"
    slug = slugify(company)
    for key, rec in entities.items():
        if _norm(rec.get("title")) == wanted:
            return key, "exact"
    if slug in entities:
        return slug, "exact"
    for suffix in _PARENT_SUFFIXES:
        if wanted.endswith(suffix):
            stem = wanted[: -len(suffix)].strip()
            if not stem:
                continue
            stem_slug = slugify(stem)
            if stem_slug in entities:
                return stem_slug, "parent"
            for key, rec in entities.items():
                if _norm(rec.get("title")) == stem:
                    return key, "parent"
    return None, "none"


def resolve(company, root=None):
    """Everything a dispatch needs to know about this employer's vault page.

    Read-only. Dormant (available False) when no vault is connected, with the
    reason named rather than an empty result that reads like an absent page.
    """
    out = {
        "company": str(company or ""),
        "available": False,
        "reason": "",
        "path": "",
        "ref": "",
        "title": "",
        "match": "none",
        "exact": False,
        "bytes": 0,
        "updated": "",
        "sections": [],
        "covers": [],
        "missing_sections": list(LETTER_SECTIONS),
        "verdict": "missing",
        "why": "",
        "skeleton": list(SKELETON),
        "thresholds": {"thin_bytes": THIN_BYTES, "min_sections": MIN_SECTIONS,
                       "min_covers": MIN_COVERS},
        "suggested_path": "",
    }
    if not out["company"]:
        out["reason"] = "no company on the role"
        return out
    try:
        root = Path(root or vault.vault_root()).expanduser()
    except Exception:  # noqa: BLE001 -- a dispatch is still usable without it
        root = None
    wiki = (root / WIKI_SUBDIR) if root else None
    if wiki is None or not wiki.is_dir():
        out["reason"] = ("no vault is connected, so there is no company page "
                         "to read or expand")
        return out
    out["available"] = True
    out["suggested_path"] = str(wiki / f"{slugify(company)}.md")

    entities = (atlasvault.scan(root) or {}).get("entities") or {}
    slug, rung = _find(company, entities)
    if slug is None:
        out["why"] = (f"no `type: entity` page in the vault answers to "
                      f"{out['company']}")
        return out

    rec = entities[slug]
    path = wiki / f"{slug}.md"
    out.update(path=str(path), ref=f"{WIKI_SUBDIR}/{path.name}",
               title=rec.get("title") or slug, match=rung,
               exact=(rung == "exact"))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        out["why"] = f"the page exists but could not be read ({exc})"
        return out
    out["bytes"] = len(text.encode("utf-8"))
    stamp = _UPDATED_RE.search(text)
    if stamp:
        out["updated"] = stamp.group(1)
    body = text.split("\n---", 1)[-1] if text.startswith("---") else text
    out["sections"] = _sections(body)
    out["covers"] = _covers(out["sections"])
    out["missing_sections"] = [s for s in LETTER_SECTIONS
                               if s not in out["covers"]]

    if rung == "parent":
        # Never "usable": the page is about the parent, and the letter is
        # being written to the subsidiary. It is a starting point and the
        # payload has to say so, or the substitution disappears.
        out["verdict"] = "thin"
        out["why"] = (
            f"the closest page is {out['ref']} ({out['title']}), the PARENT "
            f"organisation — not {out['company']} itself. Use it as "
            f"background, and write the page for {out['company']} at "
            f"{out['suggested_path']}")
        return out
    out["suggested_path"] = str(path)
    if (out["bytes"] < THIN_BYTES or len(out["sections"]) < MIN_SECTIONS
            or len(out["covers"]) < MIN_COVERS):
        out["verdict"] = "thin"
        out["why"] = (
            f"{out['ref']} is {out['bytes']:,} bytes across "
            f"{len(out['sections'])} sections and answers "
            f"{len(out['covers'])} of the {len(LETTER_SECTIONS)} things a "
            f"letter draws on, under the {THIN_BYTES:,}-byte / "
            f"{MIN_SECTIONS}-section / {MIN_COVERS}-covered floor this "
            "dispatch treats as enough to ground a specific paragraph")
        return out
    out["verdict"] = "usable"
    out["why"] = (f"{out['ref']} is {out['bytes']:,} bytes across "
                  f"{len(out['sections'])} sections"
                  + (f", last updated {out['updated']}" if out["updated"]
                     else ""))
    return out


def prompt_block(company, root=None):
    """The COMPANY RESEARCH lines an Apply dispatch carries, or []."""
    info = resolve(company, root=root)
    name = info["company"] or "this employer"
    if not info["available"]:
        if not info["reason"]:
            return []
        return ["", "COMPANY RESEARCH:",
                f"- {info['reason']}. Research {name} from the posting and "
                "the open web before drafting the letter, and say in the fit "
                "brief that no vault page backed it."]
    lines = [
        "",
        f"COMPANY RESEARCH — {name}:",
        "- The cover letter's job is to connect this career to THIS "
        "employer: what they do, what they say they are for, and why the "
        "owner wants to work there. A letter that would read the same with "
        "another company's name in it has failed, however well written.",
    ]
    if info["verdict"] == "missing":
        lines += [
            f"- {info['why']}.",
            f"- WRITE ONE at {info['suggested_path']} BEFORE drafting the "
            "letter, from the posting, the company's own site and current "
            "reporting. Follow the vault's house shape for a company page: "
            + "; ".join(SKELETON) + ".",
        ]
    else:
        lines += [
            f"- READ {info['path']} FIRST. {info['why']}.",
        ]
        if info["match"] == "parent":
            lines += ["- That page is the PARENT organisation, not this "
                      "employer. Do not let the letter describe the parent "
                      "as though it were the team hiring."]
        if info["verdict"] == "thin":
            lines += [
                "- EXPAND IT BEFORE DRAFTING, in the vault, at "
                f"{info['suggested_path']} — this is part of the job, not "
                "optional. It is missing "
                + "; ".join(info["missing_sections"])
                + ". Reach the house shape: " + "; ".join(SKELETON) + ".",
            ]
        else:
            lines += [
                "- If it still cannot ground a SPECIFIC observation for this "
                "role — the team's actual work, a stated position the owner "
                "genuinely shares, something they shipped — expand the page "
                "in the vault before drafting rather than falling back on "
                "generic praise.",
            ]
        if info["sections"]:
            lines += ["- Sections it carries: "
                      + "; ".join(info["sections"][:14])]
    lines += [
        "- Expansion rules: the vault's conventions bind (frontmatter with "
        "`type: entity`, `[[wikilinks]]`, `updated:` stamped, sources "
        "listed). Company research describes the EMPLOYER, never the owner — "
        "every sentence about the owner still needs a Master History endnote "
        "anchor. Do not fabricate: a fact you could not source stays out.",
        "- The conviction beat is drawn from this page. Values stated as "
        "history, specific and in the owner's voice; never generic praise, "
        "and never omitted.",
    ]
    return lines
