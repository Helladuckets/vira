"""Contact Atlas — a face-graph of interconnection across the CRM.

Every contact is a node (rendered with their face in the UI); edges are
the web of interconnection fused from six deterministic signals the app
already holds — no new inference on the hot path:

  photo_cooccur   mediaindex.faces — two named faces in the same photo
  group_cochat    imessage-archive group threads — same group chat
  colleague       master.company equality — the anchor-org cluster
  family          shared name tokens + family relationship tags
  shared_topic    radar's rare-token profile fingerprints
  vault_comention two people mentioned in the same vault notes

The owner sits at the center as an ego node: contacts with direct 1:1
history hang off it at degree 1, and BFS over the contact-to-contact
edges yields LinkedIn-style 2nd/3rd degrees for people reachable only
through others. Clusters come from an anchor-org pin (atlas_anchor_org)
plus a deterministic label-propagation pass.

The graph is a MATERIALIZED VIEW: build_graph() writes
data/atlas-graph.json under the cross-process file lock, GET /api/atlas
serves the cached file, and refresh happens on demand or on the weekly
routine — never per page load (the radar-groupings discipline). The one
optional AI step, narrate_edges(), labels the strongest cross-cluster
edges with a one-line "why" via suggest.complete; deterministic signal
labels render without it.

Face crops: face_crop() cuts the best-scoring media-index face for a
contact with sips (native HEIC, no model inference — the builder only
reads existing faces rows) into data/atlas-faces/, for nodes that have
no AddressBook contact photo.
"""
import json
import re
import sqlite3
import subprocess
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import data as crm
from . import mediaindex, radar, settings
from .filelock import locked

ROOT = Path(__file__).resolve().parent.parent
GRAPH = ROOT / "data" / "atlas-graph.json"
GROUPS = ROOT / "data" / "atlas-groups.json"
FACES_DIR = ROOT / "data" / "atlas-faces"

EGO = "ego"                  # the owner's node id in the served graph
MAX_GROUP = 10               # bigger group chats (scam blasts) carry no signal
MIN_SHARED_TOPICS = 2        # radar's MIN_SHARED — same bar for topic edges
NARRATE_TOP = 12             # cross-cluster edges labeled per AI pass

# per-signal contribution cap: edge weight = sum of coef * strength, each
# strength normalized to [0,1]; edges below atlas_min_edge_weight drop
COEF = {
    "photo_cooccur": 1.0,
    "group_cochat": 0.9,
    "family": 1.0,
    "colleague": 0.8,
    "shared_topic": 0.5,
    "vault_comention": 0.4,
}

FAMILY_WORDS = re.compile(
    r"\b(family|wife|husband|spouse|daughter|son|mother|father|mom|dad|"
    r"sister|brother|sibling|aunt|uncle|cousin|grandm|grandf|grandp|"
    r"in-law|niece|nephew)", re.I)

_lock = threading.Lock()            # in-process store access
_refresh_lock = threading.Lock()    # serialize builds
_building = threading.Event()


# ---------- settings ----------

def _max_nodes():
    try:
        return max(10, int(settings.get("atlas_max_nodes")))
    except (TypeError, ValueError):
        return 200


def _min_weight():
    try:
        return float(settings.get("atlas_min_edge_weight"))
    except (TypeError, ValueError):
        return 0.15


def _anchor_org():
    return str(settings.get("atlas_anchor_org") or "").strip()


# ---------- node selection ----------

def owner_pid(c=None):
    """The owner's own CRM row, when one exists: resolved from the
    notify_handle self-thread, falling back to a unique owner_name match.
    Merged into the ego node — never a regular contact node."""
    c = c or crm._load()
    handle = str(settings.raw().get("notify_handle") or "").strip()
    if handle:
        pid = crm.resolve_handle(handle)
        if pid:
            return pid
    owner = str(settings.get("owner_name") or "").strip().lower()
    if owner:
        hits = [p["id"] for p in c["people"]
                if p.get("name", "").lower().startswith(owner + " ")
                or p.get("name", "").lower() == owner]
        if len(hits) == 1:
            return hits[0]
    return None


def _activity(p):
    act = p.get("activity", {})
    return (act.get("imsg_n") or 0) + (act.get("email_n") or 0) * 2


def select_nodes(c=None, cap=None):
    """The atlas node set: tiered-or-named people (radar's grouping gate),
    most-active first, capped for legibility. The owner's own row is
    excluded — it IS the ego."""
    c = c or crm._load()
    cap = cap or _max_nodes()
    own = owner_pid(c)
    ranked = sorted(
        (p for p in c["people"]
         if p["id"] != own
         and (p.get("profile_tier") or p.get("master_tier"))
         and not p.get("name", "").startswith("(")),
        key=_activity, reverse=True)
    return ranked[:cap]


# ---------- edge helpers (each returns {(a, b): signal dict} with a < b) ----

def _pair(a, b):
    return (a, b) if a < b else (b, a)


def _photo_pairs(pid_set):
    """(a, b) -> shared-photo count from the media index's named faces.
    Best-effort: no index (or no faces yet) means no photo edges."""
    if not mediaindex.DB.exists():
        return {}
    pairs = Counter()
    try:
        con = sqlite3.connect(f"file:{mediaindex.DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """SELECT a.person_id, b.person_id, COUNT(DISTINCT a.seq)
                   FROM faces a JOIN faces b
                     ON a.seq = b.seq AND a.person_id < b.person_id
                   WHERE a.person_id IS NOT NULL
                     AND b.person_id IS NOT NULL
                   GROUP BY a.person_id, b.person_id""").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    for a, b, n in rows:
        if a in pid_set and b in pid_set:
            pairs[_pair(a, b)] = n
    return pairs


def _photo_edges(pid_set):
    out = {}
    for (a, b), n in _photo_pairs(pid_set).items():
        out[(a, b)] = {"type": "photo_cooccur",
                       "strength": min(1.0, n / 6),
                       "detail": f"{n} shared photo{'s' if n != 1 else ''}"}
    return out


def _group_edges(c, pid_set):
    """Same iMessage group thread, from the archive index. Small groups are
    strong signal; groups past MAX_GROUP members (mass blasts) are skipped."""
    strength = defaultdict(float)
    count = Counter()
    seen_titles = {}
    # chats_by_person is per-person; walk each archive entry once
    entries, seen = [], set()
    for lst in c.get("chats_by_person", {}).values():
        for e in lst:
            key = e.get("file") or id(e)
            if key not in seen:
                seen.add(key)
                entries.append(e)
    for e in entries:
        if e.get("type") != "group":
            continue
        members = [p.get("person_id") for p in e.get("participants", [])]
        members = sorted({m for m in members if m and m in pid_set})
        total = len(e.get("participants", []))
        if len(members) < 2 or total > MAX_GROUP:
            continue
        per = 1.0 / (total - 1)          # a 3-person group binds tighter
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                strength[(a, b)] += per
                count[(a, b)] += 1
                seen_titles.setdefault((a, b), e.get("title") or "")
    out = {}
    for pair, s in strength.items():
        n = count[pair]
        out[pair] = {"type": "group_cochat",
                     "strength": min(1.0, s),
                     "detail": (f"{n} shared group chat{'s' if n != 1 else ''}"
                                + (f" ({seen_titles[pair][:60]})"
                                   if n == 1 and seen_titles[pair] else ""))}
    return out


_COMPANY_STOP = {"", "self", "self-employed", "n/a", "none", "retired",
                 "unknown", "freelance"}


def _norm_company(v):
    v = re.sub(r"[^a-z0-9 ]+", " ", str(v or "").lower())
    v = re.sub(r"\b(inc|llc|llp|ltd|corp|corporation|company|co)\b", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _org_edges(c, pid_set):
    by_org = defaultdict(list)
    for pid in pid_set:
        org = _norm_company((c["master"].get(pid) or {}).get("company"))
        if org and org not in _COMPANY_STOP:
            by_org[org].append(pid)
    out = {}
    for org, pids in by_org.items():
        if len(pids) < 2:
            continue
        label = (c["master"][pids[0]].get("company") or org).strip()
        for i, a in enumerate(sorted(pids)):
            for b in sorted(pids)[i + 1:]:
                out[_pair(a, b)] = {"type": "colleague", "strength": 1.0,
                                    "detail": f"both at {label[:60]}"}
    return out


def _surname_tokens(name):
    """Name tokens minus the leading first name — shared first names must
    never read as family ties."""
    toks = re.findall(r"[a-z][a-z'-]{2,}", (name or "").lower())
    return set(toks[1:])


def _is_family(c, pid):
    prof = c["profiles"].get(pid) or {}
    if (prof.get("relationship_class") or "").lower() == "family":
        return True
    master = c["master"].get(pid) or {}
    return bool(FAMILY_WORDS.search(str(master.get("relationship") or "")))


def _family_edges(c, pid_set):
    """Shared name token + at least one side carrying a family tag — the
    brief's family heuristics applied pairwise."""
    fam = {pid for pid in pid_set if _is_family(c, pid)}
    if not fam:
        return {}
    toks = {pid: _surname_tokens(c["by_id"][pid]["name"])
            for pid in pid_set if c["by_id"].get(pid)}
    out = {}
    for a in sorted(fam):
        for b in sorted(pid_set):
            if a == b:
                continue
            pair = _pair(a, b)
            if pair in out:
                continue
            shared = toks.get(a, set()) & toks.get(b, set())
            if not shared:
                continue
            out[pair] = {"type": "family", "strength": 1.0,
                         "detail": "family — " + sorted(shared)[0]}
    return out


def _fingerprints(c, pid_set):
    fps = {}
    for pid in pid_set:
        p = c["by_id"].get(pid)
        if not p:
            continue
        prof = c["profiles"].get(pid) or {}
        master = c["master"].get(pid) or {}
        toks = radar.person_tokens(p, prof, master)
        if toks:
            fps[pid] = toks
    return fps


def _topic_edges(c, pid_set):
    """Radar's rare-but-shared token pass, generalized from a top-40 pair
    list into a full edge set over the atlas nodes."""
    fps = _fingerprints(c, pid_set)
    df = Counter()
    for toks in fps.values():
        df.update(toks)
    pids = sorted(fps)
    out = {}
    for i, a in enumerate(pids):
        for b in pids[i + 1:]:
            shared = [t for t in fps[a] & fps[b] if 2 <= df[t] <= 12]
            if len(shared) < MIN_SHARED_TOPICS:
                continue
            score = sum(1.0 / df[t] for t in shared)
            shared.sort(key=lambda t: df[t])
            out[(a, b)] = {"type": "shared_topic",
                           "strength": min(1.0, score / 0.8),
                           "detail": "shared: " + ", ".join(shared[:4]),
                           "topics": shared[:8]}
    return out


def _vault_edges(c, pid_set, per_person_cap=200):
    """Two people mentioned in the same vault notes. Best-effort FTS-only
    (no embeddings at build time); a missing vault index means no edges."""
    try:
        from . import vault
        if not vault.DB_PATH.exists():
            return {}
        con = vault._connect()
    except Exception:  # noqa: BLE001 — the vault layer is optional
        return {}
    notes_by_pid = {}
    try:
        vault._init(con)
        for pid in sorted(pid_set):
            person = c["by_id"].get(pid)
            name = (person or {}).get("name") or ""
            if len(name.split()) < 2:
                continue      # bare first names false-hit everywhere
            q = '"' + name.replace('"', " ") + '"'
            try:
                rows = con.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "LIMIT ?", (q, per_person_cap)).fetchall()
            except sqlite3.OperationalError:
                continue
            if not rows:
                continue
            marks = ",".join("?" * len(rows))
            paths = {r[0] for r in con.execute(
                f"SELECT DISTINCT path FROM chunks WHERE id IN ({marks})",
                [x[0] for x in rows])}
            if paths:
                notes_by_pid[pid] = paths
    except Exception:  # noqa: BLE001
        return {}
    finally:
        con.close()
    out = {}
    pids = sorted(notes_by_pid)
    for i, a in enumerate(pids):
        for b in pids[i + 1:]:
            shared = notes_by_pid[a] & notes_by_pid[b]
            if not shared:
                continue
            sample = sorted(shared)[0]
            out[(a, b)] = {"type": "vault_comention",
                           "strength": min(1.0, len(shared) / 3),
                           "detail": (f"{len(shared)} shared note"
                                      f"{'s' if len(shared) != 1 else ''}"
                                      f" ({Path(sample).stem[:40]})")}
    return out


# ---------- fusion ----------

def build_edges(c, pid_set):
    """Fuse all signal passes into weighted, typed, explainable edges:
    [{a, b, weight, signals: [{type, strength, detail}]}], thresholded."""
    passes = [_photo_edges(pid_set), _group_edges(c, pid_set),
              _org_edges(c, pid_set), _family_edges(c, pid_set),
              _topic_edges(c, pid_set), _vault_edges(c, pid_set)]
    merged = defaultdict(list)
    for signals in passes:
        for pair, sig in signals.items():
            merged[pair].append(sig)
    floor = _min_weight()
    edges = []
    for (a, b), sigs in merged.items():
        w = sum(COEF[s["type"]] * s["strength"] for s in sigs)
        if w < floor:
            continue
        sigs.sort(key=lambda s: -COEF[s["type"]] * s["strength"])
        edges.append({"a": a, "b": b, "weight": round(w, 3),
                      "signals": [{k: v for k, v in s.items()
                                   if k in ("type", "detail", "topics")}
                                  for s in sigs]})
    edges.sort(key=lambda e: -e["weight"])
    return edges


def _ego_edges(c, pid_set, own_pid):
    """The owner's direct edges: 1:1 message/email history (always), plus
    shared photos with the owner's own face when his row is resolvable."""
    photo = {}
    if own_pid:
        wide = set(pid_set) | {own_pid}
        for (a, b), n in _photo_pairs(wide).items():
            if own_pid in (a, b):
                other = b if a == own_pid else a
                photo[other] = n
    out = []
    for pid in sorted(pid_set):
        p = c["by_id"].get(pid)
        if not p:
            continue
        act = p.get("activity", {})
        vol = (act.get("imsg_n") or 0) + (act.get("email_n") or 0) * 2
        sigs = []
        if vol > 0:
            sigs.append({"type": "direct",
                         "detail": f"{act.get('imsg_n') or 0} messages, "
                                   f"{act.get('email_n') or 0} emails"})
        n = photo.get(pid)
        if n:
            sigs.append({"type": "photo_cooccur",
                         "detail": f"{n} photo{'s' if n != 1 else ''} together"})
        if not sigs:
            continue
        w = 0.25 + 0.75 * min(1.0, vol / 400)
        if n:
            w = min(1.0, w + min(0.4, n / 20))
        out.append({"a": EGO, "b": pid, "weight": round(w, 3),
                    "signals": sigs})
    return out


# ---------- degrees / paths / clusters ----------

def degrees_from_ego(pids, edges, ego_edges):
    """BFS hops from the owner: degree 1 = a direct ego edge; deeper
    degrees ride the contact-to-contact mesh. Unreachable -> None."""
    adj = defaultdict(set)
    for e in edges:
        adj[e["a"]].add(e["b"])
        adj[e["b"]].add(e["a"])
    deg = {pid: None for pid in pids}
    frontier = [e["b"] for e in ego_edges if e["b"] in deg]
    d = 1
    while frontier:
        nxt = []
        for pid in frontier:
            if deg.get(pid) is None:
                deg[pid] = d
                nxt.extend(adj[pid])
        frontier = [p for p in set(nxt) if p in deg and deg[p] is None]
        d += 1
    return deg


def shortest_path(graph, a, b):
    """Fewest-hops path over the CONTACT mesh (the ego is excluded — every
    pair trivially connects through the owner). Ties prefer heavier edges."""
    if a == b:
        return [a]
    adj = defaultdict(list)
    for e in graph.get("edges", []):
        adj[e["a"]].append((e["b"], e["weight"]))
        adj[e["b"]].append((e["a"], e["weight"]))
    best = {a: (0, 0.0, None)}       # pid -> (hops, -sum weight, prev)
    frontier = [a]
    while frontier:
        nxt = []
        for pid in sorted(frontier):
            hops, negw, _ = best[pid]
            for other, w in adj[pid]:
                cand = (hops + 1, negw - w, pid)
                if other not in best or cand < best[other]:
                    best[other] = cand
                    nxt.append(other)
        frontier = nxt
    if b not in best:
        return None
    path, cur = [], b
    while cur is not None:
        path.append(cur)
        cur = best[cur][2]
    return list(reversed(path))


STRUCTURAL = {"photo_cooccur", "group_cochat", "family", "colleague"}


def clusters(c, pids, edges, anchor_org=None):
    """pid -> cluster id, plus cluster metadata.

    Identity clusters seed first — the pinned anchor org, then every
    shared employer, then family components (over family edges). The
    residual friend mesh settles by DEGREE-NORMALIZED label propagation
    over structural edges (each edge counts w / sqrt(deg_a * deg_b), so
    a hub like a spouse cannot pull the whole graph into one blob), with
    the seeded labels held fixed. Deterministic: sorted visit order,
    smallest-label tie break."""
    anchor_org = (anchor_org if anchor_org is not None else _anchor_org())
    anchor_norm = _norm_company(anchor_org)
    label = {pid: i for i, pid in enumerate(sorted(pids))}
    pinned = set()
    seed_names = {}                       # fixed label -> cluster name

    def pin(pid, lab):
        label[pid] = lab
        pinned.add(pid)

    if anchor_norm:
        for pid in sorted(pids):
            org = _norm_company((c["master"].get(pid) or {}).get("company"))
            if org and (anchor_norm in org or org in anchor_norm):
                pin(pid, -1)
        seed_names[-1] = anchor_org or "anchor"

    by_org = defaultdict(list)
    for pid in sorted(pids):
        if pid in pinned:
            continue
        org = _norm_company((c["master"].get(pid) or {}).get("company"))
        if org and org not in _COMPANY_STOP:
            by_org[org].append(pid)
    next_seed = -2
    for org, members_ in sorted(by_org.items()):
        if len(members_) < 2:
            continue
        for pid in members_:
            pin(pid, next_seed)
        seed_names[next_seed] = (
            (c["master"][members_[0]].get("company") or org).strip()[:40])
        next_seed -= 1

    # family components over the family edges (union-find)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    fam_edges = [e for e in edges
                 if any(s["type"] == "family" for s in e["signals"])]
    for e in fam_edges:
        if e["a"] not in pinned and e["b"] not in pinned:
            ra, rb = find(e["a"]), find(e["b"])
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    comps = defaultdict(list)
    for pid in parent:
        comps[find(pid)].append(pid)
    for root, members_ in sorted(comps.items()):
        if len(members_) < 2:
            continue
        for pid in members_:
            pin(pid, next_seed)
        surnames = Counter()
        for pid in members_:
            for t in sorted(_surname_tokens(c["by_id"][pid]["name"])):
                surnames[t.title()] += 1
        tops = [s for s, _ in surnames.most_common(2)]
        seed_names[next_seed] = ("–".join(tops) + " family") if tops \
            else "family"
        next_seed -= 1

    adj = defaultdict(list)
    for e in edges:
        w = sum(COEF[s["type"]] for s in e["signals"]
                if s["type"] in STRUCTURAL)
        if w <= 0:
            continue
        adj[e["a"]].append((e["b"], w))
        adj[e["b"]].append((e["a"], w))
    deg = {pid: max(1, len(adj[pid])) for pid in pids}
    order = sorted(pids)
    for _ in range(30):
        changed = 0
        for pid in order:
            if pid in pinned or not adj[pid]:
                continue
            tally = defaultdict(float)
            for other, w in adj[pid]:
                # seeded identity labels (negative) never propagate — being
                # married into the family's group chats is not membership
                if label.get(other, -1) >= 0:
                    tally[label[other]] += \
                        w / (deg[pid] * deg[other]) ** 0.5
            if not tally:
                continue
            new = min(k for k, v in tally.items()
                      if v == max(tally.values()))
            if new != label[pid]:
                label[pid] = new
                changed += 1
        if not changed:
            break

    members = defaultdict(list)
    for pid in pids:
        members[label[pid]].append(pid)
    out_label, meta = {}, []
    for lab, pids_in in sorted(
            members.items(),
            key=lambda kv: (kv[0] >= 0, kv[0], -len(kv[1]))):
        floor = 2 if lab < 0 else 3
        if lab != -1 and len(pids_in) < floor:
            continue                      # tiny clusters stay uncolored
        cid = f"c{len(meta)}"
        name = seed_names.get(lab)
        if not name:
            surnames = Counter()
            for pid in pids_in:
                parts = (c["by_id"][pid]["name"] or "").split()
                if len(parts) >= 2:
                    surnames[parts[-1]] += 1
            if surnames and surnames.most_common(1)[0][1] >= 3:
                name = surnames.most_common(1)[0][0] + " circle"
        meta.append({"id": cid, "label": name or f"circle {len(meta) + 1}",
                     "anchor": lab == -1, "size": len(pids_in)})
        for pid in pids_in:
            out_label[pid] = cid
    return out_label, meta


# ---------- the optional AI pass ----------

NARRATE_PROMPT = """You are {owner}'s chief of staff. Below are pairs of \
{owner}'s contacts that the Contact Atlas found strongly connected, with \
the deterministic signals behind each edge and a short dossier per person.

For each pair, write ONE short sentence (max 20 words) explaining HOW these \
two people most plausibly know each other or what connects them, grounded \
ONLY in the given signals and dossiers. Never invent facts.

Return ONLY a JSON object:
{{"labels": [{{"a_id": "...", "b_id": "...", "why": "..."}}]}}

Pairs:

{pairs}
"""


def narrate_edges(graph, c=None, top_n=NARRATE_TOP):
    """ONE suggest.complete pass over the strongest cross-cluster edges that
    have no narration yet. Mutates and returns the graph; deterministic
    labels keep rendering when this never runs."""
    c = c or crm._load()
    todo = [e for e in graph.get("edges", [])
            if not e.get("narrative")
            and graph["node_cluster"].get(e["a"])
            != graph["node_cluster"].get(e["b"])][:top_n]
    if not todo:
        return graph

    def dossier(pid):
        person = c["by_id"].get(pid) or {}
        prof = c["profiles"].get(pid) or {}
        master = c["master"].get(pid) or {}
        bits = [person.get("name", pid)]
        for k in ("company", "title", "relationship"):
            if master.get(k):
                bits.append(f"{k}: {master[k]}")
        if isinstance(prof.get("relationship_summary"), str):
            bits.append(prof["relationship_summary"][:200])
        return " | ".join(bits)[:320]

    blocks = []
    for e in todo:
        sig = "; ".join(s["detail"] for s in e["signals"] if s.get("detail"))
        blocks.append(f"- a_id: {e['a']}  b_id: {e['b']}\n  signals: {sig}\n"
                      f"  A: {dossier(e['a'])}\n  B: {dossier(e['b'])}")
    owner = settings.get("owner_name") or "the owner"
    prompt = NARRATE_PROMPT.format(owner=owner,
                                   pairs="\n".join(blocks)[:30_000])
    try:
        from . import suggest
        parsed = suggest._extract_json(suggest.complete(prompt))
        by_pair = {_pair(x.get("a_id"), x.get("b_id")): (x.get("why") or "")
                   for x in (parsed.get("labels") or [])
                   if x.get("a_id") and x.get("b_id")}
        for e in graph["edges"]:
            why = by_pair.get(_pair(e["a"], e["b"]))
            if why:
                e["narrative"] = why[:200]
    except Exception:  # noqa: BLE001 — narration is garnish, never a gate
        pass
    return graph


# ---------- build / store ----------

def _read():
    try:
        return json.loads(GRAPH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write(graph):
    GRAPH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GRAPH.with_name(GRAPH.name + ".tmp")
    tmp.write_text(json.dumps(graph, ensure_ascii=False))
    tmp.replace(GRAPH)


def build_graph(narrate=False):
    """Assemble the full materialized view and write it to disk. Safe to
    call from a background thread; serialized against concurrent builds."""
    with _refresh_lock:
        _building.set()
        try:
            c = crm._load()
            own = owner_pid(c)
            people = select_nodes(c)
            pid_set = {p["id"] for p in people}
            edges = build_edges(c, pid_set)
            ego_edges = _ego_edges(c, pid_set, own)
            deg = degrees_from_ego(pid_set, edges, ego_edges)
            node_cluster, cluster_meta = clusters(c, pid_set, edges)

            owner_toks = set()
            if own:
                p = c["by_id"].get(own)
                if p:
                    owner_toks = radar.person_tokens(
                        p, c["profiles"].get(own) or {},
                        c["master"].get(own) or {})
            for e in edges:
                topics = next((s.get("topics") for s in e["signals"]
                               if s["type"] == "shared_topic"), None)
                if topics and owner_toks and set(topics) & owner_toks:
                    e["shared_interest"] = True

            from . import photos
            nodes = []
            crop_pids = _face_pids()
            for p in people:
                pid = p["id"]
                master = c["master"].get(pid) or {}
                prof = c["profiles"].get(pid) or {}
                face = ("photo" if photos.photo_path(pid)
                        else "crop" if pid in crop_pids else None)
                nodes.append({
                    "id": pid, "name": p["name"],
                    "tier": p.get("profile_tier") or p.get("master_tier"),
                    "company": (master.get("company") or "")[:60],
                    "title": (master.get("title") or "")[:60],
                    "relationship_class": prof.get("relationship_class"),
                    "degree": deg.get(pid),
                    "cluster": node_cluster.get(pid),
                    "face": face,
                    "act": _activity(p),
                })
            graph = {
                "generated": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
                "owner": {"name": settings.get("owner_name") or "me",
                          "pid": own},
                "params": {"max_nodes": _max_nodes(),
                           "min_edge_weight": _min_weight(),
                           "anchor_org": _anchor_org()},
                "nodes": nodes,
                "edges": edges,
                "ego_edges": ego_edges,
                "clusters": cluster_meta,
                "node_cluster": node_cluster,
            }
            prior = _read() or {}
            kept = {_pair(e["a"], e["b"]): e.get("narrative")
                    for e in prior.get("edges", []) if e.get("narrative")}
            for e in graph["edges"]:
                why = kept.get(_pair(e["a"], e["b"]))
                if why:
                    e["narrative"] = why
            if narrate:
                narrate_edges(graph, c)
            with _lock, locked(GRAPH):
                _write(graph)
            return graph
        finally:
            _building.clear()


def refresh(narrate=False):
    """Rebuild in a background thread (the /api/radar/intros pattern)."""
    threading.Thread(target=build_graph, kwargs={"narrate": narrate},
                     daemon=True, name="vira-atlas-refresh").start()


def compose():
    """The window payload: the cached view + build status, with the
    user-curated group overrides applied."""
    with _lock:
        graph = _read()
    if not graph:
        return {"status": "empty", "building": _building.is_set()}
    apply_overrides(graph)
    graph["status"] = "ok"
    graph["building"] = _building.is_set()
    return graph


# ---------- user-curated groups (the override layer) ----------
#
# The graph cache stays pristine-derived; edits live in data/atlas-groups
# .json and overlay at serve time, so they apply instantly and survive
# every rebuild. Derived cluster ids (c0, c1, ...) are positional and
# unstable across rebuilds, so the store never references them: any edit
# to a derived cluster PROMOTES it — its current members snapshot into a
# custom group with a stable id (g1, g2, ...) and the derived label joins
# the dissolved list so the superseded chip cannot re-form. Assignment
# overrides map pid -> custom gid ("" = explicitly ungrouped); deleting a
# custom group clears its assignments, dropping members back to whatever
# the next build derives.

def _groups_read():
    try:
        ov = json.loads(GROUPS.read_text())
    except (OSError, json.JSONDecodeError):
        ov = {}
    ov.setdefault("next", 1)
    ov.setdefault("groups", [])      # [{id, label}]
    ov.setdefault("assign", {})      # pid -> gid | "" (ungrouped)
    ov.setdefault("dissolved", [])   # lowered labels of removed derived
    return ov


def _groups_write(ov):
    GROUPS.parent.mkdir(parents=True, exist_ok=True)
    tmp = GROUPS.with_name(GROUPS.name + ".tmp")
    tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1))
    tmp.replace(GROUPS)


def apply_overrides(graph):
    """Mutate a freshly-read graph: dissolve removed derived clusters,
    apply per-person assignments, append custom groups, recompute sizes.
    Emptied derived clusters drop; custom groups stay even at size 0 so
    a just-created group is visible for member-adding."""
    ov = _groups_read()
    if not (ov["groups"] or ov["assign"] or ov["dissolved"]):
        return graph
    dissolved = {d.lower() for d in ov["dissolved"]}
    dead = {c["id"] for c in graph.get("clusters", [])
            if (c.get("label") or "").lower() in dissolved}
    custom = {g["id"] for g in ov["groups"]}
    for n in graph.get("nodes", []):
        if n.get("cluster") in dead:
            n["cluster"] = None
        a = ov["assign"].get(n["id"])
        if a is not None:
            n["cluster"] = a if a in custom else None
    counts = Counter(n["cluster"] for n in graph.get("nodes", [])
                     if n.get("cluster"))
    clusters = []
    for c in graph.get("clusters", []):
        if c["id"] in dead:
            continue
        size = counts.get(c["id"], 0)
        if size:
            clusters.append({**c, "size": size})
    for g in ov["groups"]:
        clusters.append({"id": g["id"], "label": g["label"],
                         "custom": True, "size": counts.get(g["id"], 0)})
    graph["clusters"] = clusters
    graph["node_cluster"] = {n["id"]: n["cluster"]
                             for n in graph.get("nodes", [])
                             if n.get("cluster")}
    return graph


def _overlay_payload(gid=None):
    """What group mutations return — enough for the client to patch its
    state in place (no re-layout)."""
    g = compose()
    if g.get("status") != "ok":
        raise ValueError("atlas not built yet")
    out = {"clusters": g["clusters"], "node_cluster": g["node_cluster"]}
    if gid:
        out["gid"] = gid
    return out


def _members_of(cid):
    g = compose()
    if g.get("status") != "ok":
        raise ValueError("atlas not built yet")
    meta = next((c for c in g["clusters"] if c["id"] == cid), None)
    if not meta:
        raise ValueError("unknown group")
    return meta, [n["id"] for n in g["nodes"] if n.get("cluster") == cid]


def group_create(label):
    label = (label or "").strip()[:60]
    if not label:
        raise ValueError("a group needs a name")
    with _lock, locked(GROUPS):
        ov = _groups_read()
        gid = f"g{ov['next']}"
        ov["next"] += 1
        ov["groups"].append({"id": gid, "label": label})
        _groups_write(ov)
    return _overlay_payload(gid)


def _promote(cid):
    """Snapshot a derived cluster into a stable custom group (no-op for
    an already-custom id). The derived label is dissolved — the custom
    group supersedes it."""
    meta, members = _members_of(cid)
    if meta.get("custom"):
        return cid
    with _lock, locked(GROUPS):
        ov = _groups_read()
        gid = f"g{ov['next']}"
        ov["next"] += 1
        ov["groups"].append({"id": gid, "label": meta["label"]})
        for pid in members:
            ov["assign"][pid] = gid
        low = (meta.get("label") or "").lower()
        if low and low not in ov["dissolved"]:
            ov["dissolved"].append(low)
        _groups_write(ov)
    return gid


def group_rename(cid, label):
    label = (label or "").strip()[:60]
    if not label:
        raise ValueError("a group needs a name")
    gid = _promote(cid)
    with _lock, locked(GROUPS):
        ov = _groups_read()
        for g in ov["groups"]:
            if g["id"] == gid:
                g["label"] = label
        _groups_write(ov)
    return _overlay_payload(gid)


def group_dissolve(cid):
    """Remove a grouping. Derived: its label joins the dissolved list so
    rebuilds cannot resurrect it; people stay, ungrouped. Custom: the
    group is deleted and its assignments cleared, so members fall back
    to whatever the build derives for them."""
    meta, _members = _members_of(cid)
    with _lock, locked(GROUPS):
        ov = _groups_read()
        if meta.get("custom"):
            ov["groups"] = [g for g in ov["groups"] if g["id"] != cid]
            ov["assign"] = {p: a for p, a in ov["assign"].items()
                            if a != cid}
        else:
            low = (meta.get("label") or "").lower()
            if low and low not in ov["dissolved"]:
                ov["dissolved"].append(low)
        _groups_write(ov)
    return _overlay_payload()


def group_assign(pid, target):
    """Put a person in a group ("" = explicitly ungrouped). A derived
    target promotes first, so the store only ever references stable
    custom ids. People outside the rendered graph (below the activity
    cutoff) can be designated too — the membership sits in the store and
    applies the moment they enter the node set."""
    g = compose()
    if g.get("status") != "ok":
        raise ValueError("atlas not built yet")
    gid = ""
    if target:
        meta = next((c for c in g["clusters"] if c["id"] == target), None)
        if not meta:
            raise ValueError("unknown group")
        gid = target if meta.get("custom") else _promote(target)
    with _lock, locked(GROUPS):
        ov = _groups_read()
        ov["assign"][pid] = gid
        _groups_write(ov)
    return _overlay_payload(gid or None)


def person_groups(pid):
    """The profile-row payload: this person's current group + every
    group they could move to."""
    g = compose()
    if g.get("status") != "ok":
        return {"status": "empty", "current": None, "groups": [],
                "in_atlas": False}
    cur = g["node_cluster"].get(pid)
    if cur is None:
        # not a rendered node — the store may still designate them
        cur = _groups_read()["assign"].get(pid) or None
    meta = next((c for c in g["clusters"] if c["id"] == cur), None)
    return {"status": "ok", "current": meta, "groups": g["clusters"],
            "in_atlas": any(n["id"] == pid for n in g["nodes"])}


def node_detail(pid):
    """One node + its resolved edges, for the side card."""
    graph = compose()
    if graph.get("status") != "ok":
        return None
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    node = next((n for n in graph["nodes"] if n["id"] == pid), None)
    if not node:
        return None
    edges = []
    for e in graph["edges"]:
        if pid in (e["a"], e["b"]):
            other = e["b"] if e["a"] == pid else e["a"]
            edges.append({"pid": other, "name": names.get(other, other),
                          "weight": e["weight"], "signals": e["signals"],
                          "narrative": e.get("narrative")})
    edges.sort(key=lambda x: -x["weight"])
    ego = next((e for e in graph["ego_edges"] if e["b"] == pid), None)
    return {"node": node, "edges": edges, "ego": ego}


def path_between(a, b):
    graph = compose()
    if graph.get("status") != "ok":
        return None
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    path = shortest_path(graph, a, b)
    if not path:
        return {"found": False, "from": a, "to": b}
    by_pair = {_pair(e["a"], e["b"]): e for e in graph["edges"]}
    hops = []
    for x, y in zip(path, path[1:]):
        e = by_pair.get(_pair(x, y), {})
        hops.append({"a": x, "b": y, "weight": e.get("weight"),
                     "signals": e.get("signals", []),
                     "narrative": e.get("narrative")})
    return {"found": True,
            "path": [{"pid": p, "name": names.get(p, p)} for p in path],
            "hops": hops}


# ---------- face crops ----------

def _face_pids():
    """People with at least one named face row (crop candidates)."""
    if not mediaindex.DB.exists():
        return set()
    try:
        con = sqlite3.connect(f"file:{mediaindex.DB}?mode=ro", uri=True)
        try:
            return {r[0] for r in con.execute(
                "SELECT DISTINCT person_id FROM faces "
                "WHERE person_id IS NOT NULL")}
        finally:
            con.close()
    except sqlite3.Error:
        return set()


def face_crop(pid):
    """Best media-index face for a contact, cropped with sips (native HEIC,
    no model inference) and cached in data/atlas-faces/. None when the
    index has nothing usable."""
    cached = FACES_DIR / f"{pid}.jpg"
    if cached.exists():
        return cached
    if not mediaindex.DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{mediaindex.DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """SELECT f.bbox, i.path FROM faces f
                   JOIN items i ON i.seq = f.seq
                   WHERE f.person_id = ? AND i.kind = 'photo'
                     AND i.purged = 0 AND i.path IS NOT NULL
                   ORDER BY f.det_score * IFNULL(f.match_score, 0.5) DESC
                   LIMIT 12""", (pid,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    for bbox_json, path in rows:
        src = Path(path).expanduser()
        if not src.exists():
            continue
        try:
            bbox = json.loads(bbox_json)
            out = _sips_crop(src, bbox, cached)
            if out:
                return out
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def _sips_dims(src):
    try:
        res = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(src)],
            capture_output=True, text=True, timeout=20)
    except OSError:  # no sips off-Mac — the caller falls back to no crop
        return None
    w = h = None
    for line in res.stdout.splitlines():
        if "pixelWidth" in line:
            w = int(line.split()[-1])
        elif "pixelHeight" in line:
            h = int(line.split()[-1])
    return (w, h) if w and h else None


def _sips_crop(src, bbox, dest, margin=0.45, out_px=256):
    """Crop a face bbox (with margin) out of src and cache a small JPEG.
    Mac-only (sips); elsewhere the face falls back to the letter tile."""
    if not settings.IS_MAC:
        return None
    dims = _sips_dims(src)
    if not dims:
        return None
    W, H = dims
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    side = max(x2 - x1, y2 - y1) * (1 + 2 * margin)
    side = min(side, W, H)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    left = max(0, min(W - side, cx - side / 2))
    top = max(0, min(H - side, cy - side / 2))
    if side < 40:
        return None                       # too tiny to read as a face
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp.jpg")
    # two passes — sips resamples before it crops when given both flags,
    # which yielded 25px thumbnails; crop first, then size the square
    res = subprocess.run(
        ["sips", "--cropOffset", str(int(top)), str(int(left)),
         "-c", str(int(side)), str(int(side)),
         "-s", "format", "jpeg", str(src), "--out", str(tmp)],
        capture_output=True, text=True, timeout=30)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return None
    res = subprocess.run(
        ["sips", "-z", str(out_px), str(out_px), str(tmp)],
        capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        tmp.unlink(missing_ok=True)
        return None
    tmp.replace(dest)
    return dest
