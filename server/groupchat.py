"""Group-chat profiles: a group thread as a first-class subject, with the
same depth as a person page.

Everything here except the brief is DETERMINISTIC — direct chat.db reads,
CRM joins, and a filter over the materialized atlas graph. The one AI pass
(`brief`) reads the room: dynamics, current threads, grounded reply
suggestions, live loops. It is cached per (group, latest message) in
data/group-briefs.json so reopening a quiet group costs nothing.

Group identity: chat.db carries several chat rows for one logical group
(SMS vs iMessage legs), so a group is addressed as a LIST of chat rowids,
merged the same way groups_for_person merges — identical member set +
display name. `resolve_group(chat_id)` goes from the one chat row a feed
item knows to the full merged group.

Sending: a reply goes to the LEG WHERE THE CONVERSATION IS — the chat row
with the newest message — addressed by its guid (send.send_to_group).
"""
import hashlib
from pathlib import Path

from . import data as crm
from . import imessage, jsonstore, settings

ROOT = Path(__file__).resolve().parent.parent
BRIEFS = ROOT / "data" / "group-briefs.json"

BRIEF_CACHE_MAX = 40      # groups worth of cached briefs
RELATED_MAX = 12          # related-group rows returned
RELATED_MIN_SHARED = 2    # fewer shared members than this is every group
EDGE_MAX = 20             # interconnection rows returned
THREAD_TAIL = 80          # messages the brief reads


# ---------- resolution: one chat row -> the merged logical group ----------

def _chat_members(con, cid):
    return sorted(r[0] for r in con.execute(
        """SELECT h.id FROM chat_handle_join chj
           JOIN handle h ON h.ROWID = chj.handle_id
           WHERE chj.chat_id = ?""", (cid,)).fetchall())


def _chat_meta(con, cid):
    row = con.execute(
        """SELECT c.display_name, c.guid, c.service_name,
                  (SELECT COUNT(*) FROM chat_message_join
                   WHERE chat_id = c.ROWID),
                  (SELECT MAX(m.date) FROM message m
                   JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                   WHERE cmj.chat_id = c.ROWID)
           FROM chat c WHERE c.ROWID = ?""", (cid,)).fetchone()
    if not row:
        return None
    name, guid, service, n_msgs, last_ns = row
    return {"name": name or None, "guid": guid, "service": service,
            "messages": n_msgs or 0, "last_ns": last_ns or 0}


def _legs_like(con, members, name):
    """Every style-43 chat row with exactly this member set and name —
    the legs of one logical group."""
    if not members:
        return []
    qmarks = ",".join("?" * len(members))
    cands = [r[0] for r in con.execute(
        f"""SELECT DISTINCT c.ROWID FROM chat c
            JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
            JOIN handle h ON h.ROWID = chj.handle_id
            WHERE h.id IN ({qmarks}) AND c.style = 43""",
        tuple(members)).fetchall()]
    legs = []
    for cid in cands:
        if _chat_members(con, cid) != members:
            continue
        meta = _chat_meta(con, cid)
        if meta and (meta["name"] or "") == (name or ""):
            legs.append((cid, meta))
    return legs


def _participants(members):
    c = crm._load()
    out = []
    for hd in members:
        pid = crm.resolve_handle(hd)
        p = c["by_id"].get(pid) if pid else None
        out.append({"handle": hd, "person_id": pid,
                    "name": p["name"] if p else hd,
                    "known": pid is not None})
    return out


def resolve_group(chat_id):
    """One chat rowid (e.g. off a feed item) -> the merged logical group:
    {chat_ids, name, participants, messages, last, send:{chat_id, guid,
    service}}. None when the row is unknown or not a group chat."""
    if settings.fixture_mode():
        return None
    con = imessage._connect()
    try:
        style = con.execute("SELECT style FROM chat WHERE ROWID = ?",
                            (chat_id,)).fetchone()
        if not style or style[0] != 43:
            return None
        members = _chat_members(con, chat_id)
        meta = _chat_meta(con, chat_id)
        if not meta:
            return None
        legs = _legs_like(con, members, meta["name"]) or [(chat_id, meta)]
        return _assemble(legs, members, meta["name"])
    finally:
        con.close()


def resolve_by_ids(chat_ids):
    """A known leg list (a groups_for_person row) -> the same shape.
    Members = the first leg's set; the legs are taken as given."""
    if settings.fixture_mode() or not chat_ids:
        return None
    con = imessage._connect()
    try:
        members = _chat_members(con, chat_ids[0])
        name = None
        legs = []
        for cid in chat_ids:
            meta = _chat_meta(con, cid)
            if not meta:
                continue
            name = name or meta["name"]
            legs.append((cid, meta))
        if not legs:
            return None
        return _assemble(legs, members, name)
    finally:
        con.close()


def _assemble(legs, members, name):
    total = sum(m["messages"] for _, m in legs)
    last_ns = max((m["last_ns"] for _, m in legs), default=0)
    # the reply goes where the conversation is: the newest-message leg
    send_cid, send_meta = max(legs, key=lambda lm: lm[1]["last_ns"])
    last = imessage.apple_dt(last_ns)
    return {
        "chat_ids": [cid for cid, _ in legs],
        "name": name or None,
        "participants": _participants(members),
        "messages": total,
        "last": last.isoformat() if last else None,
        "send": {"chat_id": send_cid, "guid": send_meta["guid"],
                 "service": send_meta["service"] or "iMessage"},
    }


def group_label(group):
    if group.get("name"):
        return group["name"]
    firsts = [p["name"].split(" ")[0] for p in group["participants"]]
    return ", ".join(firsts) or "Group"


# ---------- the deterministic profile ----------

def _member_dossiers(participants):
    """CRM depth per resolved member: relationship, title/company, hooks,
    open loops. Reads the already-loaded stores — no model, no network."""
    c = crm._load()
    out = []
    for part in participants:
        m = dict(part)
        pid = part.get("person_id")
        if pid:
            from . import photos
            prof = c["profiles"].get(pid) or {}
            master = c["master"].get(pid) or {}
            m["has_photo"] = bool(photos.photo_path(pid))
            m["relationship"] = (prof.get("relationship_class")
                                 or master.get("relationship") or None)
            m["title"] = (master.get("title") or "")[:60] or None
            m["company"] = (master.get("company") or "")[:60] or None
            m["summary"] = (prof.get("relationship_summary") or "")[:220] or None
            m["hooks"] = [h.get("hook") for h in (prof.get("hooks") or [])
                          if isinstance(h, dict) and h.get("hook")][:3]
            loops = []
            for lo in (prof.get("open_loops") or []):
                if not isinstance(lo, dict) or lo.get("status") == "closed":
                    continue
                loops.append({"what": lo.get("what") or "",
                              "owed_by": lo.get("owed_by") or "me"})
            m["open_loops"] = loops[:3]
        else:
            m["has_photo"] = False
        out.append(m)
    return out


def _activity(chat_ids):
    """Who drives this group: message counts per sender, the owner included
    as person_id None / name 'you' (is_from_me rows carry no handle)."""
    qmarks = ",".join("?" * len(chat_ids))
    con = imessage._connect()
    try:
        rows = con.execute(
            f"""SELECT h.id, m.is_from_me, COUNT(*)
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})
                  AND (m.associated_message_type = 0
                       OR m.associated_message_type IS NULL)
                GROUP BY h.id, m.is_from_me""",
            tuple(chat_ids)).fetchall()
        first_ns, = con.execute(
            f"""SELECT MIN(m.date) FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})""",
            tuple(chat_ids)).fetchone()
    finally:
        con.close()
    c = crm._load()
    counts = {}   # key -> {pid, name, n}
    for handle, from_me, n in rows:
        if from_me:
            key, pid, name = "me", None, "you"
        else:
            pid = crm.resolve_handle(handle) if handle else None
            p = c["by_id"].get(pid) if pid else None
            key = pid or (handle or "?")
            name = p["name"] if p else (handle or "?")
        e = counts.setdefault(key, {"person_id": pid, "name": name, "n": 0})
        e["n"] += n
    total = sum(e["n"] for e in counts.values()) or 1
    out = sorted(counts.values(), key=lambda e: -e["n"])
    for e in out:
        e["pct"] = round(100 * e["n"] / total)
    first = imessage.apple_dt(first_ns)
    return out, (first.isoformat() if first else None)


def _interconnections(participants):
    """Edges of the materialized Visual Network graph between the members.
    Purely a filter over the cached view — signals and weights come with
    each edge, so the UI can say WHY two people connect. Members below the
    graph's activity cutoff are reported as off_graph, not silently absent."""
    from . import atlas
    pids = {p["person_id"] for p in participants if p.get("person_id")}
    graph = atlas.compose()
    if graph.get("status") != "ok":
        return {"edges": [], "off_graph": sorted(pids), "available": False}
    on_graph = {n["id"] for n in graph.get("nodes", [])} & pids
    names = {p["person_id"]: p["name"] for p in participants
             if p.get("person_id")}
    edges = []
    for e in graph.get("edges", []):
        if e["a"] in on_graph and e["b"] in on_graph:
            edges.append({
                "a": e["a"], "b": e["b"],
                "a_name": names.get(e["a"], e["a"]),
                "b_name": names.get(e["b"], e["b"]),
                "weight": e.get("weight"),
                "signals": e.get("signals", []),
                "narrative": e.get("narrative"),
            })
    edges.sort(key=lambda e: -(e["weight"] or 0))
    return {"edges": edges[:EDGE_MAX],
            "off_graph": sorted(pids - on_graph),
            "available": True}


def _member_key(part):
    """Cross-group identity: the person when known (one person texts from
    several handles), the raw handle otherwise."""
    return part.get("person_id") or part["handle"]


def related_groups(group):
    """Other group chats sharing members with this one, merged into logical
    groups, each labeled by how it differs: same people, adds, misses.
    This is the 'scrub for past groups' — the diff is the payload."""
    member_handles = [p["handle"] for p in group["participants"]]
    if not member_handles:
        return []
    self_ids = set(group["chat_ids"])
    self_keys = {_member_key(p) for p in group["participants"]}
    qmarks = ",".join("?" * len(member_handles))
    con = imessage._connect()
    try:
        cands = [r[0] for r in con.execute(
            f"""SELECT DISTINCT c.ROWID FROM chat c
                JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE h.id IN ({qmarks}) AND c.style = 43""",
            tuple(member_handles)).fetchall()]
        merged = {}
        for cid in cands:
            if cid in self_ids:
                continue
            members = tuple(_chat_members(con, cid))
            meta = _chat_meta(con, cid)
            if not meta:
                continue
            key = (members, meta["name"] or "")
            g = merged.get(key)
            if g:
                g["chat_ids"].append(cid)
                g["messages"] += meta["messages"]
                g["last_ns"] = max(g["last_ns"], meta["last_ns"])
            else:
                merged[key] = {"chat_ids": [cid], "name": meta["name"],
                               "members": members,
                               "messages": meta["messages"],
                               "last_ns": meta["last_ns"]}
    finally:
        con.close()

    out = []
    for g in merged.values():
        parts = _participants(list(g["members"]))
        keys = {_member_key(p) for p in parts}
        shared = self_keys & keys
        if len(shared) < RELATED_MIN_SHARED and keys != self_keys:
            continue
        name_of = {_member_key(p): p["name"].split(" ")[0] for p in parts}
        for p in group["participants"]:
            name_of.setdefault(_member_key(p), p["name"].split(" ")[0])
        if keys == self_keys:
            relation = "same"
        elif keys > self_keys:
            relation = "superset"
        elif keys < self_keys:
            relation = "subset"
        else:
            relation = "overlap"
        last = imessage.apple_dt(g["last_ns"])
        out.append({
            "chat_ids": g["chat_ids"],
            "name": g["name"],
            "label": g["name"] or ", ".join(
                sorted(name_of[k] for k in keys)),
            "participants": parts,
            "messages": g["messages"],
            "last": last.isoformat() if last else None,
            "relation": relation,
            "shared": sorted(name_of[k] for k in shared),
            "added": sorted(name_of[k] for k in keys - self_keys),
            "missing": sorted(name_of[k] for k in self_keys - keys),
        })
    rank = {"same": 0, "superset": 1, "subset": 2, "overlap": 3}
    out.sort(key=lambda g: (rank[g["relation"]], -len(g["shared"]),
                            -(g["messages"] or 0)))
    return out[:RELATED_MAX]


def profile(chat_ids=None, chat=None):
    """The whole deterministic payload the group panel renders. ~4 chat.db
    scans + CRM joins; the AI brief is a separate (cached) call."""
    if settings.fixture_mode():
        return {"status": "empty", "note": "fixture mode has no group chats"}
    group = resolve_group(chat) if chat else resolve_by_ids(chat_ids or [])
    if not group:
        return {"status": "empty", "note": "not a known group chat"}
    from . import media
    counts = media.counts_for_chats(group["chat_ids"])
    tot = {"photos": 0, "links": 0, "docs": 0}
    for cid in group["chat_ids"]:
        for k in tot:
            tot[k] += counts.get(cid, {}).get(k, 0)
    activity, first = _activity(group["chat_ids"])
    group["participants"] = _member_dossiers(group["participants"])
    return {
        "status": "ok",
        "group": group,
        "label": group_label(group),
        "media": tot,
        "activity": activity,
        "first": first,
        "connections": _interconnections(group["participants"]),
        "related": related_groups(group),
    }


# ---------- the AI brief: one pass, cached per (group, latest message) ----------

def _group_key(chat_ids):
    return hashlib.sha1(
        ",".join(str(c) for c in sorted(chat_ids)).encode()).hexdigest()[:16]


def _latest_rowid(chat_ids):
    qmarks = ",".join("?" * len(chat_ids))
    con = imessage._connect()
    try:
        row = con.execute(
            f"""SELECT MAX(m.ROWID) FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id IN ({qmarks})""",
            tuple(chat_ids)).fetchone()
        return row[0] or 0
    finally:
        con.close()


def _brief_prompt(prof, messages):
    owner = settings.get("owner_name") or "the owner"
    lines = [
        f"You are Vira, {owner}'s chief of staff, reading one iMessage "
        "GROUP thread to help them show up well in it.",
        "", f"GROUP: {prof['label']}",
        f"Members ({len(prof['group']['participants'])} + {owner}):"]
    for m in prof["group"]["participants"]:
        bits = [m["name"]]
        if m.get("relationship"):
            bits.append(m["relationship"])
        if m.get("title") or m.get("company"):
            bits.append(" ".join(x for x in [m.get("title"),
                                             m.get("company")] if x))
        lines.append("- " + " · ".join(bits))
        if m.get("summary"):
            lines.append("  about: " + m["summary"])
        for h in m.get("hooks", []):
            lines.append("  hook: " + h)
        for lo in m.get("open_loops", []):
            who = "you owe" if lo["owed_by"] == "me" else "they owe"
            lines.append(f"  open loop ({who}): {lo['what']}")
    act = ", ".join(f"{a['name']} {a['pct']}%" for a in prof["activity"][:6])
    if act:
        lines.append("Who talks: " + act)
    for e in prof["connections"]["edges"][:8]:
        kinds = ",".join(s.get("type", "") for s in e.get("signals", []))
        lines.append(f"Connection: {e['a_name']} <-> {e['b_name']} ({kinds})")
    for rg in prof["related"][:5]:
        diff = []
        if rg["added"]:
            diff.append("adds " + ", ".join(rg["added"]))
        if rg["missing"]:
            diff.append("without " + ", ".join(rg["missing"]))
        lines.append(f"Related group '{rg['label']}' ({rg['relation']}"
                     + (": " + "; ".join(diff) if diff else "") + ")")
    lines.append("")
    lines.append(f"RECENT THREAD (oldest first; 'me' = {owner}):")
    for msg in messages:
        who = "me" if msg["from_me"] else (msg.get("sender") or "?")
        lines.append(f"[{who}] {msg['text'][:200]}")
    lines.append("")
    lines.append(
        "Return STRICT JSON only, no prose around it:\n"
        "{\n"
        '  "read": "2-3 sentences: the state of this group right now — '
        "dynamics, what's live, what it needs from the owner\",\n"
        '  "highlights": ["up to 4 short current threads/topics"],\n'
        '  "suggestions": [{"label": "3-6 word handle", "text": "a reply '
        "the owner could send NOW\"}],\n"
        '  "loops": [{"what": "an open loop LIVE in this thread", "who": '
        '"you|them|group"}],\n'
        '  "watch": "one thing to watch for, or \\"\\""\n'
        "}\n"
        "Rules: 2-3 suggestions, matched to the owner's voice as evidenced "
        "in the thread; ground EVERYTHING in the messages and dossiers "
        "above — never invent facts, names, plans, or dates that are not "
        "there. Suggestions must answer the actual live thread (the most "
        "recent messages), not old topics. No emojis.")
    return "\n".join(lines)


def _clean_brief(raw):
    if not isinstance(raw, dict):
        raise ValueError("brief is not an object")
    out = {"read": str(raw.get("read") or "")[:600],
           "watch": str(raw.get("watch") or "")[:300],
           "highlights": [], "suggestions": [], "loops": []}
    for h in (raw.get("highlights") or [])[:4]:
        if isinstance(h, str) and h.strip():
            out["highlights"].append(h.strip()[:120])
    for s in (raw.get("suggestions") or [])[:3]:
        if isinstance(s, dict) and (s.get("text") or "").strip():
            out["suggestions"].append(
                {"label": str(s.get("label") or "Reply")[:48],
                 "text": str(s["text"]).strip()[:600]})
    for lo in (raw.get("loops") or [])[:4]:
        if isinstance(lo, dict) and (lo.get("what") or "").strip():
            who = str(lo.get("who") or "group")
            out["loops"].append(
                {"what": str(lo["what"]).strip()[:200],
                 "who": who if who in ("you", "them", "group") else "group"})
    if not out["read"]:
        raise ValueError("brief has no read")
    return out


def brief(chat_ids, force=False):
    """The one model pass, cached until the group has a newer message.
    Returns {status, brief, cached, generated}."""
    if settings.fixture_mode():
        return {"status": "empty", "note": "fixture mode"}
    key = _group_key(chat_ids)
    latest = _latest_rowid(chat_ids)
    store = jsonstore.read(BRIEFS, {"briefs": {}})
    hit = store.get("briefs", {}).get(key)
    if hit and not force and hit.get("latest") == latest:
        return {"status": "ok", "brief": hit["brief"], "cached": True,
                "generated": hit.get("generated")}

    prof = profile(chat_ids=chat_ids)
    if prof.get("status") != "ok":
        return prof
    messages = imessage.group_thread(chat_ids, limit=THREAD_TAIL)
    if not messages:
        return {"status": "empty", "note": "no visible messages"}

    from . import suggest
    text = suggest.complete(_brief_prompt(prof, messages))
    cleaned = _clean_brief(suggest._extract_json(text))
    from .jobshared import now_iso
    generated = now_iso()

    def _put(s):
        b = s.setdefault("briefs", {})
        b[key] = {"latest": latest, "generated": generated,
                  "brief": cleaned, "label": prof["label"]}
        jsonstore.prune_oldest(b, BRIEF_CACHE_MAX)
        return s
    jsonstore.mutate(BRIEFS, _put, {"briefs": {}})
    return {"status": "ok", "brief": cleaned, "cached": False,
            "generated": generated}


# ---------- sending ----------

def send(chat_ids, text):
    """Send to the merged group's active leg. Resolution is server-side so
    the client only ever names the group; the passive guard lives in
    send.send_to_group with the rest of the outbound discipline."""
    group = resolve_by_ids(chat_ids)
    if not group:
        raise ValueError("not a known group chat")
    from . import send as sender
    res = sender.send_to_group(group["send"]["guid"], text,
                               chat_ids=group["chat_ids"])
    res["label"] = group_label(group)
    return res
