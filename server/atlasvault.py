"""The vault web — people from the wiki joined onto the Visual Network.

The CRM graph is everyone the owner has MET. The vault's wiki also
carries `type: person` pages for people worth knowing ABOUT —
researchers, founders, hosts — each page linking the entities they
belong to and the sources they appear in. This module turns those pages
into an OVERLAY for the atlas: extra nodes (only people the CRM does not
already hold), extra edges (wikilinks between pages, shared sources,
shared orgs), and BRIDGES into the CRM web wherever a vault person's org
matches a contact's employer. The bridges are the point: the path to
someone you have never met runs through the people you already know at
their company, which is what makes the toggle a brainstorming surface.

DERIVED, NEVER STORED (the vaultpeople discipline): rebuilt from the
wiki directory and cached in memory on a directory fingerprint that
includes file mtimes, so an edited page lands without a restart and
nothing here can go stale against the vault. `atlas.compose(vault=True)`
merges the overlay at read time; the materialized atlas-graph.json stays
pristine CRM-only, so every other consumer of compose() — groupchat's
interconnections, the weekly refresh — is untouched.

Dedupe rules, deliberately conservative:
  - a vault page whose name matches a CRM person IN the graph folds onto
    that node (its edges re-point to the pid; the node gains `wiki`);
  - a match to a CRM person BELOW the activity cutoff is dropped — they
    are someone the owner knows, not a "beyond the CRM" candidate;
  - a match to the owner is dropped (the ego is not a node);
  - name matching requires two or more tokens — a single-token page
    ("Claude") must never fold onto a single-token contact — and runs
    exact-normalized first, then first+last token (middle-name variants)
    only where that pair is unique in the CRM.
Org matching is exact on the normalized name (atlas._norm_company); a
fuzzy join would band strangers under employers they do not share.
"""
import os
import re
from collections import defaultdict, deque
from pathlib import Path

from . import vault

WIKI_SUBDIR = "wiki"
HEAD_BYTES = 800                 # enough to see frontmatter `type:`
MAX_VAULT_NODES = 400            # legibility backstop, not a config knob
QUALIFIER_CAP = 160

# per-signal coefficient, the atlas COEF pattern; weight = sum coef * strength
COEF = {
    "wiki_link": 0.9,            # one page deliberately names the other
    "wiki_cosource": 0.7,        # both appear in the same source material
    "wiki_org": 0.8,             # same entity page = the colleague analog
}

_TYPE_RE = re.compile(r"^type:\s*(person|entity)\s*$", re.M)
_TITLE_RE = re.compile(r'^title:\s*"?([^"\n]+?)"?\s*$', re.M)
_TAGS_INLINE_RE = re.compile(r"^tags:\s*\[([^\]]*)\]", re.M)
_TAGS_BLOCK_RE = re.compile(r"^tags:\s*\n((?:\s+-\s+.+\n?)+)", re.M)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#^]+)")
_NAME_NORM_RE = re.compile(r"[^a-z0-9]+")

_cache = {"fp": None, "scan": None}


def _norm_name(name):
    return _NAME_NORM_RE.sub(" ", (name or "").lower()).strip()


def _fingerprint(wiki):
    """Cheap staleness key: entry count + the newest mtime in the dir.
    Unlike a bare dir mtime this moves when a page is EDITED in place."""
    newest, count = 0.0, 0
    try:
        with os.scandir(wiki) as it:
            for entry in it:
                if not entry.name.endswith(".md"):
                    continue
                count += 1
                try:
                    m = entry.stat().st_mtime
                except OSError:
                    continue
                if m > newest:
                    newest = m
    except OSError:
        return None
    return (count, newest)


def _tags(fm):
    m = _TAGS_INLINE_RE.search(fm)
    if m:
        return [t.strip().strip("'\"") for t in m.group(1).split(",")
                if t.strip()]
    m = _TAGS_BLOCK_RE.search(fm)
    if m:
        return [ln.strip().lstrip("-").strip().strip("'\"")
                for ln in m.group(1).splitlines() if ln.strip()]
    return []


def _links(text):
    """Wikilink target slugs, aliases/headings/blocks stripped."""
    out = set()
    for m in _WIKILINK_RE.finditer(text):
        slug = m.group(1).strip().strip('"')
        if "/" in slug:
            slug = slug.rsplit("/", 1)[-1]
        if slug:
            out.add(slug.lower())
    return out


def _parse_page(path):
    """One wiki page -> a person/entity record, or None. Reads the head
    first so 8k source-summaries cost a stat and 800 bytes, not a full
    read."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    tm = _TYPE_RE.search(head)
    if not tm:
        return None
    kind = tm.group(1)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm, body = text[3:end], text[end + 4:]
    m = _TITLE_RE.search(fm)
    title = (m.group(1).strip() if m else "") \
        or path.stem.replace("-", " ").title()
    rec = {"kind": kind, "slug": path.stem.lower(), "title": title,
           "tags": _tags(fm)}
    if kind == "person":
        qualifier = ""
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            qualifier = re.sub(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]",
                               lambda m: m.group(2) or m.group(1),
                               line)
            qualifier = re.sub(r"[*_`]", "", qualifier)[:QUALIFIER_CAP]
            break
        rec["qualifier"] = qualifier
        rec["links"] = _links(body)
        rec["sources"] = _links(fm)
    return rec


def scan(root=None):
    """Every person and entity page in the wiki, cached on the dir
    fingerprint. {"people": {slug: rec}, "entities": {slug: rec}}."""
    root = Path(root or vault.vault_root()).expanduser()
    wiki = root / WIKI_SUBDIR
    fp = _fingerprint(wiki)
    if fp is None:
        return {"people": {}, "entities": {}}
    if _cache["fp"] == (str(wiki), fp):
        return _cache["scan"]
    people, entities = {}, {}
    for p in sorted(wiki.glob("*.md")):
        rec = _parse_page(p)
        if not rec:
            continue
        rec["ref"] = f"{WIKI_SUBDIR}/{p.name}"
        if rec["kind"] == "person":
            people[rec["slug"]] = rec
        else:
            entities[rec["slug"]] = rec
    out = {"people": people, "entities": entities}
    _cache["fp"] = (str(wiki), fp)
    _cache["scan"] = out
    return out


def _person_orgs(rec, entities):
    """Ordered entity slugs a person belongs to: body links first (the
    deliberate statement), then tags. Links to entities that are clearly
    not employers still count — the edge says "both tied to X", and for
    brainstorming a shared institution is exactly the tie wanted."""
    seen, out = set(), []
    for slug in list(rec.get("links", [])) + [t.lower()
                                             for t in rec.get("tags", [])]:
        if slug in entities and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _pair(a, b):
    return (a, b) if a < b else (b, a)


def overlay(graph, c=None, root=None, min_weight=None):
    """The vault overlay for a composed CRM graph: nodes, edges, and the
    wiki refs of CRM nodes that have their own page."""
    from . import data as crm
    from .atlas import _min_weight, _norm_company, owner_pid
    from . import atlaslens

    data = scan(root)
    if not data["people"]:
        return {"nodes": [], "edges": [], "wiki_refs": {}}
    c = c or crm._load()
    entities = data["entities"]
    graph_pids = {n["id"] for n in graph.get("nodes", [])}
    floor = _min_weight() if min_weight is None else min_weight

    # ---- name index over the WHOLE registry, not just graph nodes ----
    # Exact normalized name first; then (first, last) token pair as a
    # fallback for middle-name variants ("Ann Reef" -> "Ann Q. Reef") —
    # but ONLY when the pair is unique in the CRM, so a shared
    # first-plus-surname can never fold a stranger onto a contact.
    by_name, by_fl = {}, {}
    for p in c.get("people", []):
        nm = _norm_name(p.get("name"))
        toks = nm.split()
        if len(toks) < 2 or p.get("name", "").startswith("("):
            continue
        by_name.setdefault(nm, p["id"])
        key = (toks[0], toks[-1])
        if key not in by_fl:
            by_fl[key] = p["id"]
        elif by_fl[key] != p["id"]:
            by_fl[key] = None                    # ambiguous — never match
    own = owner_pid(c)
    from . import settings
    owner_name = _norm_name(settings.get("owner_name") or "")

    matched, dropped, fresh = {}, set(), []
    for slug, rec in data["people"].items():
        nm = _norm_name(rec["title"])
        if owner_name and nm == owner_name:
            dropped.add(slug)
            continue
        toks = nm.split()
        pid = None
        if len(toks) >= 2:
            pid = by_name.get(nm) or by_fl.get((toks[0], toks[-1]))
        if pid and pid == own:
            dropped.add(slug)
        elif pid and pid in graph_pids:
            matched[slug] = pid
        elif pid:
            dropped.add(slug)          # known person below the cutoff
        else:
            fresh.append(rec)
    fresh = fresh[:MAX_VAULT_NODES]
    fresh_slugs = {r["slug"] for r in fresh}

    def node_id(slug):
        if slug in matched:
            return matched[slug]
        if slug in fresh_slugs:
            return "v:" + slug
        return None

    # ---- org index: entity slug -> members; CRM company -> bridge ----
    person_orgs = {slug: _person_orgs(rec, entities)
                   for slug, rec in data["people"].items()
                   if slug in fresh_slugs or slug in matched}
    org_members = defaultdict(list)          # entity slug -> [node id]
    for slug, orgs in person_orgs.items():
        nid = node_id(slug)
        for org in orgs:
            if nid:
                org_members[org].append(nid)
    # CRM contacts whose employer normalizes onto an entity title
    ab = atlaslens._ab_index()
    ent_by_norm = {}
    for slug, rec in entities.items():
        key = _norm_company(rec["title"])
        if key:
            ent_by_norm.setdefault(key, slug)
    for n in graph.get("nodes", []):
        org = (n.get("company") or "").strip() \
            or (ab.get(n["id"], {}).get("org") or "")
        slug = ent_by_norm.get(_norm_company(org)) if org else None
        if slug and n["id"] not in org_members[slug]:
            org_members[slug].append(n["id"])

    # ---- edges ----
    sig = defaultdict(list)                  # pair -> [signal]

    # wikilinks between person pages (either direction)
    linked = set()
    for slug, rec in data["people"].items():
        a = node_id(slug)
        if not a:
            continue
        for tgt in rec.get("links", ()):
            if tgt == slug or tgt not in data["people"]:
                continue
            b = node_id(tgt)
            if not b or a == b:
                continue
            pair = _pair(a, b)
            if pair in linked:
                continue
            linked.add(pair)
            sig[pair].append({"type": "wiki_link", "strength": 1.0,
                              "detail": "linked on the wiki"})

    # shared sources — both people appear in the same material
    by_source = defaultdict(list)
    for slug, rec in data["people"].items():
        nid = node_id(slug)
        if not nid:
            continue
        for src in rec.get("sources", ()):
            by_source[src].append(nid)
    shared_n = defaultdict(int)
    for ids in by_source.values():
        ids = sorted(set(ids))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                shared_n[_pair(a, b)] += 1
    for pair, n in shared_n.items():
        sig[pair].append({
            "type": "wiki_cosource", "strength": min(1.0, n / 2),
            "detail": f"{n} shared source{'s' if n != 1 else ''} "
                      "in your notes"})

    # shared org — vault-to-vault and the bridge into the CRM
    matched_pids = set(matched.values())
    for org, ids in org_members.items():
        ids = sorted(set(ids))
        if len(ids) < 2:
            continue
        label = entities[org]["title"]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if not (a.startswith("v:") or b.startswith("v:")
                        or a in matched_pids or b in matched_pids):
                    continue     # never re-derive CRM-to-CRM colleague ties
                pair = _pair(a, b)
                if any(s["type"] == "wiki_org" for s in sig[pair]):
                    continue
                sig[pair].append({"type": "wiki_org", "strength": 1.0,
                                  "detail": f"both tied to {label[:60]}"})

    edges = []
    for (a, b), signals in sig.items():
        w = round(sum(COEF[s["type"]] * s["strength"] for s in signals), 3)
        if w >= floor:
            edges.append({"a": a, "b": b, "weight": w, "signals": signals})
    edges.sort(key=lambda e: -e["weight"])

    # ---- nodes ----
    link_count = defaultdict(int)
    for e in edges:
        link_count[e["a"]] += 1
        link_count[e["b"]] += 1
    # A geopolitics-tagged entity (a country, a conflict) still makes
    # edges — "both tied to Iran" is a real shared context — but never
    # claims the company FIELD, which feeds the Companies lens.
    def orgish(slug):
        tags = {t.lower() for t in entities[slug].get("tags", [])}
        return not (tags & {"geopolitics", "cat/geopolitics", "country"})

    nodes = []
    for rec in fresh:
        nid = "v:" + rec["slug"]
        orgs = [o for o in person_orgs.get(rec["slug"]) or [] if orgish(o)]
        company = entities[orgs[0]]["title"][:60] if orgs else ""
        nodes.append({
            "id": nid, "name": rec["title"],
            "tier": None, "company": company, "title": "",
            "relationship_class": None,
            "degree": None, "cluster": None, "face": None,
            "act": min(150, 10 * (1 + len(rec.get("sources", ()))
                                  + link_count[nid])),
            "vault": True, "ref": rec["ref"],
            "qualifier": rec.get("qualifier") or "",
        })

    # ---- degrees: BFS from the ego over the MERGED adjacency ----
    adj = defaultdict(set)
    for e in list(graph.get("edges", [])) + edges:
        adj[e["a"]].add(e["b"])
        adj[e["b"]].add(e["a"])
    dist = {}
    q = deque()
    for e in graph.get("ego_edges", []):
        dist[e["b"]] = 1
        q.append(e["b"])
    while q:
        cur = q.popleft()
        if dist[cur] >= 3:
            continue
        for nxt in adj[cur]:
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
    for n in nodes:
        n["degree"] = dist.get(n["id"])

    wiki_refs = {pid: data["people"][slug]["ref"]
                 for slug, pid in matched.items()}
    return {"nodes": nodes, "edges": edges, "wiki_refs": wiki_refs}


def merge(graph, root=None):
    """Mutate a composed graph in place: append the vault overlay's nodes
    and edges (folding signals onto an existing CRM edge when the pair
    already exists), stamp wiki refs, and summarize under graph["vault"].
    Runs BEFORE lenses so the companies lens bands vault people too."""
    ov = overlay(graph, root=root)
    have = {n["id"] for n in graph.get("nodes", [])}
    graph.setdefault("nodes", []).extend(
        n for n in ov["nodes"] if n["id"] not in have)
    by_pair = {_pair(e["a"], e["b"]): e for e in graph.get("edges", [])}
    added = 0
    for e in ov["edges"]:
        prior = by_pair.get(_pair(e["a"], e["b"]))
        if prior:
            prior["signals"] = prior["signals"] + e["signals"]
            prior["weight"] = round(prior["weight"] + e["weight"], 3)
        else:
            graph.setdefault("edges", []).append(e)
            added += 1
    for pid, ref in ov["wiki_refs"].items():
        for n in graph["nodes"]:
            if n["id"] == pid:
                n["wiki"] = ref
                break
    graph["vault"] = {"people": len(ov["nodes"]), "ties": added,
                      "linked": len(ov["wiki_refs"])}
    return graph
