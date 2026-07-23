"""CRM data layer: loads the registry, master evidence, profiles, and chat archive
index from the configured CRM root and answers merged person queries.

Everything here is deterministic — pure file/sqlite reads, no AI.
The root comes from settings (data/config.json `crm_root`); in fixture mode
it is the seeded copy of fixtures/crm-data.
"""
import datetime as _dt
import json
import re
import shutil
import threading
import time

from . import settings


class ProfileCorruptError(RuntimeError):
    """An EXISTING profile file could not be parsed. Writes fail closed —
    the original is quarantined, never replaced (audit P0-3)."""

# serialize concurrent profile read-modify-write cycles inside this process
# (two brief rows closed back-to-back, a journal integration racing a click)
_write_lock = threading.Lock()


def _crm():
    return settings.crm_root()

_cache = {"loaded_at": 0}
_TTL = 300  # reload CRM files at most every 5 minutes


def norm_digits(h):
    d = re.sub(r"\D", "", h or "")
    return d[-10:] if len(d) >= 10 else d


def _load():
    now = time.time()
    if _cache.get("people") and now - _cache["loaded_at"] < _TTL:
        return _cache

    root = _crm()
    try:
        people = json.loads((root / "people.json").read_text())["people"]
    except (OSError, json.JSONDecodeError, KeyError):
        people = []          # no registry yet — an empty CRM, not a crash
    try:
        master = {r["id"]: r
                  for r in json.loads((root / "master.json").read_text())
                  if isinstance(r, dict) and r.get("id")}
    except (OSError, json.JSONDecodeError):
        master = {}

    by_id, by_handle = {}, {}
    for p in people:
        by_id[p["id"]] = p
        h = p.get("handles", {})
        for e in h.get("emails", []) + h.get("imessage", []):
            if "@" in e:
                by_handle[e.lower()] = p["id"]
        for ph in h.get("phones10", []):
            by_handle[ph] = p["id"]
        for im in h.get("imessage", []):
            if "@" not in im:
                by_handle[norm_digits(im)] = p["id"]

    # Handles the owner added on a contact card but that never landed in the
    # registry (the write failed, or this is a fixture CRM) still resolve, so
    # the next message from that address joins the person it was added to. An
    # address the registry already owns is left alone — the card never steals
    # a handle from another contact.
    try:
        from . import contactcard
        for handle, pid in contactcard.added_handles().items():
            key = handle.lower() if "@" in handle else norm_digits(handle)
            if key and key not in by_handle and pid in by_id:
                by_handle[key] = pid
    except Exception:  # noqa: BLE001 — an overlay read must never break the CRM
        pass

    profiles = {}
    prof_dir = root / "profiles"
    if prof_dir.exists():
        for f in prof_dir.glob("p_*.json"):
            try:
                profiles[f.stem] = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue

    chats_by_person = {}
    try:
        idx = json.loads((root / "imessage-archive" / "index.json").read_text())
        for entry in idx.get("files", []):
            for part in entry.get("participants", []):
                pid = part.get("person_id")
                if pid:
                    chats_by_person.setdefault(pid, []).append(entry)
    except (OSError, json.JSONDecodeError):
        pass

    _cache.update(people=people, master=master, by_id=by_id, by_handle=by_handle,
                  profiles=profiles, chats_by_person=chats_by_person, loaded_at=now)
    return _cache


def resolve_handle(addr):
    """iMessage handle or email -> person_id or None."""
    c = _load()
    if not addr:
        return None
    if "@" in addr:
        return c["by_handle"].get(addr.lower())
    return c["by_handle"].get(norm_digits(addr))


def person_summary(p, profiles):
    prof = profiles.get(p["id"])
    act = p.get("activity", {})
    return {
        "id": p["id"],
        "name": p["name"],
        "class_hint": p.get("class_hint"),
        "tier": p.get("profile_tier") or p.get("master_tier"),
        "has_profile": p["id"] in profiles,
        "relationship_class": prof.get("relationship_class") if prof else None,
        "imsg_n": act.get("imsg_n", 0),
        "imsg_last": act.get("imsg_last"),
        "email_n": act.get("email_n", 0),
    }


def _last_contact(p):
    act = p.get("activity", {})
    return max(act.get("imsg_last") or "", act.get("email_last") or "")


def search_people(q=None, limit=60, sort="recent"):
    c = _load()
    people = c["people"]
    if q:
        ql = q.lower()
        hits = []
        for p in people:
            hay = p["name"].lower()
            handles = p.get("handles", {})
            extra = " ".join(handles.get("emails", []) + handles.get("phones10", []))
            if ql in hay or ql in extra.lower():
                hits.append(p)
        people = hits
    if sort == "alpha":
        # unnamed placeholders sink to the bottom instead of leading the list
        def alpha_key(p):
            n = p["name"].casefold()
            unnamed = not n[:1].isalpha()
            return (unnamed, n)
        people = sorted(people, key=alpha_key)
    else:  # most recent contact across channels (iMessage or email)
        people = sorted(people,
                        key=lambda p: (_last_contact(p),
                                       p.get("activity", {}).get("imsg_n") or 0),
                        reverse=True)
    return [person_summary(p, c["profiles"]) for p in people[:limit]]


def get_person(pid):
    c = _load()
    p = c["by_id"].get(pid)
    if not p:
        return None
    m = c["master"].get(pid, {})
    prof = c["profiles"].get(pid)
    chats = sorted(c["chats_by_person"].get(pid, []),
                   key=lambda e: e.get("date_last") or "", reverse=True)
    return {
        "person": p,
        "master": {k: m.get(k) for k in ("full_name", "company", "title",
                                         "relationship", "evidence", "tier",
                                         "emails", "phones")} if m else None,
        "profile": prof,
        "chats": [{k: e.get(k) for k in ("file", "chat_id", "type", "title",
                                         "messages", "date_first", "date_last")}
                  for e in chats[:12]],
    }


def profiles_map():
    return _load()["profiles"]


def invalidate():
    _cache["loaded_at"] = 0


PROFILE_EDITABLE_FIELDS = {"hooks", "open_loops", "personal_facts"}


def save_profile_field(pid, field, value):
    """Write one editable list (hooks / open_loops / personal_facts) back to
    the person's CRM profile JSON (the CRM stays the source of truth; the
    synthesis pipeline reads the same file). Creates a minimal profile for
    people who don't have one yet."""
    if field not in PROFILE_EDITABLE_FIELDS:
        raise ValueError(f"field {field} is not editable")
    c = _load()
    p = c["by_id"].get(pid)
    if not p:
        raise KeyError(pid)
    with _write_lock:
        return _save_field_locked(pid, p, field, value)


def _profile_path(pid):
    return _crm() / "profiles" / f"{pid}.json"


def _load_profile_for_write(pid, p):
    """Read a profile at the top of a read-modify-write. A MISSING file
    yields a minimal profile (first Vira touch of a person with no synthesis
    yet). A PRESENT-but-unreadable file fails CLOSED: the original bytes are
    copied to a .corrupt-<ts> sibling and the write is refused, so one bad
    read can never replace a real profile with a near-empty one."""
    path = _profile_path(pid)
    if not path.exists():
        return {"name": p["name"]}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        q = path.with_name(path.name + ".corrupt-"
                           + time.strftime("%Y%m%d-%H%M%S"))
        try:
            if not q.exists():
                shutil.copy2(path, q)
        except OSError:
            q = None
        raise ProfileCorruptError(
            f"profile for {pid} exists but is unreadable "
            f"({e.__class__.__name__}); write refused"
            + (f" — original quarantined to {q.name}" if q else "")) from e


def _save_field_locked(pid, p, field, value):
    path = _profile_path(pid)
    prof = _load_profile_for_write(pid, p)
    prof[field] = value
    prof[f"{field}_updated_by_vira"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(prof, indent=1, ensure_ascii=False))
    tmp.replace(path)
    invalidate()
    return prof


def _norm_what(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def update_loop(pid, match_what, action, new_what=None):
    """Targeted single-loop mutation, addressed by the loop's `what` text
    (loops carry no ids; the brief/profile UI holds the exact text it
    rendered). `close` stamps status/closed_on — the shape the CRM refresh
    merge (synthesize_profiles.vira_touched_loop) preserves verbatim.
    `edit` rewrites `what` and stamps `edited`. Returns the updated loop."""
    c = _load()
    p = c["by_id"].get(pid)
    if not p:
        raise KeyError(pid)
    with _write_lock:
        prof = _load_profile_for_write(pid, p)
        loops = prof.get("open_loops")
        if not isinstance(loops, list):
            raise LookupError("no open loops on file")
        target = None
        for lp in loops:
            if isinstance(lp, dict) and _norm_what(lp.get("what")) == \
                    _norm_what(match_what) and lp.get("status") != "closed":
                target = lp
                break
        if target is None:
            raise LookupError("loop not found (already closed or refreshed away)")
        today = _dt.date.today().isoformat()
        if action == "close":
            target["status"] = "closed"
            target["closed_on"] = today
        elif action == "edit":
            if not (new_what or "").strip():
                raise ValueError("new text required")
            target["what"] = new_what.strip()
            target["edited"] = today
        else:
            raise ValueError(f"unknown action {action!r}")
        _save_field_locked(pid, p, "open_loops", loops)
        return target


def add_loop(pid, what, owed_by="me"):
    """Append a hand/Vira-added open loop. No quote/channel — exactly the
    shape vira_touched_loop treats as human-curated, so it survives profile
    refreshes."""
    c = _load()
    p = c["by_id"].get(pid)
    if not p:
        raise KeyError(pid)
    with _write_lock:
        prof = _load_profile_for_write(pid, p)
        loops = prof.get("open_loops")
        if not isinstance(loops, list):
            loops = []
        entry = {"what": (what or "").strip(),
                 "owed_by": owed_by if owed_by in ("me", "them") else "me",
                 "since": _dt.date.today().isoformat(),
                 "status": "open"}
        if not entry["what"]:
            raise ValueError("loop text required")
        loops.append(entry)
        _save_field_locked(pid, p, "open_loops", loops)
        return entry


def add_fact(pid, fact):
    """Append an owner-told fact to the person's personal_facts, stamped
    source: "vira" so the CRM refresh merge preserves it (the model's own
    facts carry source: "imessage" etc. and regenerate each refresh)."""
    c = _load()
    p = c["by_id"].get(pid)
    if not p:
        raise KeyError(pid)
    fact = (fact or "").strip()
    if not fact:
        raise ValueError("fact text required")
    with _write_lock:
        prof = _load_profile_for_write(pid, p)
        facts = prof.get("personal_facts")
        if not isinstance(facts, list):
            facts = []
        entry = {"fact": fact, "as_of": _dt.date.today().isoformat(),
                 "source": "vira"}
        facts.append(entry)
        _save_field_locked(pid, p, "personal_facts", facts)
        return entry
