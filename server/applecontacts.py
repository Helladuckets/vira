"""Apple Contacts write-back — the Apple spoke of the CRM sync engine.

Contact-card saves flow through to the matching card(s) in macOS
Contacts.app, which iCloud/CardDAV-syncs to the owner's phone — so an
edit made in Vira is an edit made everywhere, not an edit made in one
database the phone never reads (the sync gap, 2026-07-24: company
corrected in Vira while the iPhone card kept the old value).

Design per the 2026-07-22 engine session: the CRM is the hub and owns
every field; this module is the Apple SPOKE — AppleScript against
Contacts.app, never raw abcddb writes. The June 2026 dedup campaign
proved the mechanism (crm/scripts/dedup_apply.py: person-id addressing,
make-new-email, set-organization, save).

What it pushes: company, birthday, and the email/phone lists — ORDER
and LABELS. The order is the card's rank made visible: primary first,
archived last wearing an "old" label; `use` maps personal->home,
work->work. What it never does: DELETE an address that exists only on
the Apple card (deletes never auto-propagate — the engine rule), touch
the note or the photo, or create a card — push targets existing matched
cards only, and a person with no Apple card is simply not pushed.

Matching: the AddressBook sqlite stores are scanned read-only (the
photos.py discovery pattern); a card matches when it shares a
normalized handle with the person. Every matched card gets the same
push, so a person carded in several stores stays consistent in the
unified view the phone renders.

Gotchas, learned live 2026-07-24:
- Contacts.app must be RUNNING. `tell application "Contacts"` from a
  sandboxed server context cannot auto-launch it (error -600);
  _ensure_running uses `open -g -j -a Contacts` (background, hidden)
  and retries once.
- Birthdays are assembled from date components (set year/month/day on
  a `current date` copy) — `date "..."` string literals parse by the
  system locale and are not portable across machines.
- Email/phone REORDERING is a full rewrite of the list (delete every
  entry, re-add in order). That is why the plan is computed as a pure
  union first — the rewrite must re-add every Apple-side address, or a
  reorder would silently become a delete.
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from glob import glob
from pathlib import Path

from . import settings

OSA_TIMEOUT = 30
DATA = Path(__file__).resolve().parent.parent / "data"
# Apple's built-in labels arrive from sqlite wrapped like _$!<Home>!$_ —
# unwrap for re-use; anything else is a custom label and passes through.
_LABEL_WRAP = re.compile(r"^_\$!<(.+)>!\$_$")
ARCHIVED_LABEL = "old"
_USE_LABEL = {"personal": "home", "work": "work"}


def enabled():
    """The push runs only on a real, owner-mode Mac that asked for it."""
    if not settings.IS_MAC:
        return False
    if os.environ.get("VIRA_PASSIVE") or os.environ.get("VIRA_SANDBOX"):
        return False
    return bool(settings.get("apple_contacts_push"))


# ---------- reading the Apple side (sqlite, read-only) ----------

def _stores():
    home = Path.home()
    base = home / "Library/Application Support/AddressBook"
    paths = [base / "AddressBook-v22.abcddb"]
    paths += [Path(p) for p in glob(str(base / "Sources/*/AddressBook-v22.abcddb"))]
    return [p for p in paths if p.exists()]


def _norm_email(v):
    return (v or "").strip().lower()


def _norm_phone(v):
    d = re.sub(r"\D", "", v or "")
    return d[-10:] if len(d) >= 10 else d


def find_cards(emails, phones10):
    """Every Apple card sharing a handle with the person: [{uid, store,
    org, emails: [{value, label}], phones: [{value, label}]}]. Reads the
    stores read-only; never raises on a store it cannot open."""
    want_e = {_norm_email(e) for e in emails if e}
    want_p = {_norm_phone(p) for p in phones10 if p}
    cards = []
    for db in _stores():
        c = None
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            people = c.execute(
                "SELECT Z_PK, ZUNIQUEID, ZORGANIZATION FROM ZABCDRECORD "
                "WHERE ZUNIQUEID LIKE '%:ABPerson'").fetchall()
            emails_by = {}
            for r in c.execute("SELECT ZOWNER, ZADDRESS, ZLABEL, Z_PK "
                               "FROM ZABCDEMAILADDRESS ORDER BY Z_PK"):
                emails_by.setdefault(r["ZOWNER"], []).append(
                    {"value": (r["ZADDRESS"] or "").strip(),
                     "label": _unwrap_label(r["ZLABEL"])})
            phones_by = {}
            for r in c.execute("SELECT ZOWNER, ZFULLNUMBER, ZLABEL, Z_PK "
                               "FROM ZABCDPHONENUMBER ORDER BY Z_PK"):
                phones_by.setdefault(r["ZOWNER"], []).append(
                    {"value": (r["ZFULLNUMBER"] or "").strip(),
                     "label": _unwrap_label(r["ZLABEL"])})
        except Exception:
            continue
        finally:
            if c is not None:
                c.close()
        for p in people:
            es = emails_by.get(p["Z_PK"], [])
            ps = phones_by.get(p["Z_PK"], [])
            if ({_norm_email(e["value"]) for e in es} & want_e
                    or {_norm_phone(x["value"]) for x in ps} & want_p):
                cards.append({"uid": p["ZUNIQUEID"], "store": db.parent.name,
                              "org": p["ZORGANIZATION"] or "",
                              "emails": es, "phones": ps})
    return cards


def _unwrap_label(label):
    if not label:
        return ""
    m = _LABEL_WRAP.match(label)
    return (m.group(1) if m else label).strip().lower()


# ---------- the plan (pure, unit-tested) ----------

def plan_handles(rows, existing, kind):
    """The rewritten list for one kind: [(value, label)] in final order.

    `rows` are the card's handle rows (contactcard._handle_rows shape,
    already rank-sorted: primary, secondary, unranked, archived last);
    `existing` is the Apple card's current list [{value, label}]. Rules:
    - CRM rows lead, in rank order; archived rows sink to the end of the
      CRM block wearing the "old" label.
    - `use` maps to home/work for active rows; a row with no use keeps
      the label the Apple card already had (or none).
    - An address only the Apple card knows is APPENDED with its label —
      never dropped. Deletes do not propagate.
    """
    norm = _norm_email if kind == "email" else _norm_phone
    have = {}
    for h in existing:
        have.setdefault(norm(h["value"]), h)
    out, used = [], set()
    active = [r for r in rows if r["rank"] != "archived"]
    archived = [r for r in rows if r["rank"] == "archived"]
    for r in active + archived:
        key = norm(r["value"])
        if not key or key in used:
            continue
        used.add(key)
        ex = have.get(key)
        # Prefer the Apple card's own spelling/formatting of the value.
        value = ex["value"] if ex else r["value"]
        if r["rank"] == "archived":
            label = ARCHIVED_LABEL
        else:
            label = _USE_LABEL.get(r["use"]) or (ex["label"] if ex else "")
        out.append((value, label))
    for h in existing:
        key = norm(h["value"])
        if key and key not in used:
            used.add(key)
            out.append((h["value"], h["label"]))
    return out


def _same_handles(planned, existing, kind):
    norm = _norm_email if kind == "email" else _norm_phone
    cur = [(norm(h["value"]), (h["label"] or "").lower()) for h in existing]
    new = [(norm(v), (l or "").lower()) for v, l in planned]
    return cur == new


# ---------- the AppleScript ----------

def _esc(s):
    return (s or "").replace("\\", "\\\\").replace('"', '\\"') \
                    .replace("\n", " ").replace("\r", " ").strip()


def _birthday_parts(value):
    """The card's birthday string -> (year, month, day) or None. Accepts
    M/D/YYYY (the card's placeholder format) and YYYY-MM-DD."""
    v = (value or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", v)
    if m:
        return int(m.group(3)), int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", v)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def build_script(uid, *, company=None, birthday=None,
                 emails=None, phones=None):
    """The per-card AppleScript. Only the pieces that changed are
    included; None means leave that aspect of the card alone."""
    lines = [f'set P to person id "{_esc(uid)}"']
    if company is not None:
        lines.append(f'set organization of P to "{_esc(company)}"')
    if birthday is not None:
        y, mo, d = birthday
        lines += ["set bd to current date",
                  f"set year of bd to {y}",
                  f"set month of bd to {_MONTHS[mo - 1]}",
                  f"set day of bd to {d}",
                  "set time of bd to 0",
                  "set birth date of P to bd"]
    for prop, plan in (("email", emails), ("phone", phones)):
        if plan is None:
            continue
        lines.append(f"delete every {prop} of P")
        for value, label in plan:
            props = f'{{value:"{_esc(value)}"}}' if not label else \
                f'{{label:"{_esc(label)}", value:"{_esc(value)}"}}'
            lines.append(f"make new {prop} at end of {prop}s of P "
                         f"with properties {props}")
    body = "\n".join(lines)
    return f'tell application "Contacts"\n{body}\nsave\nend tell\nreturn "ok"'


def _ensure_running():
    r = subprocess.run(["pgrep", "-x", "Contacts"], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["open", "-g", "-j", "-a", "Contacts"],
                       capture_output=True, timeout=15)
        time.sleep(2)


def _osascript(script):
    _ensure_running()
    r = subprocess.run(["osascript", "-e", script], capture_output=True,
                       text=True, timeout=OSA_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return (r.stdout or "").strip()


def backup_vcard(uid):
    """Export the card's vCard next to the other data backups before the
    first mutating touch — restore is re-import. Best-effort."""
    try:
        out = _osascript('tell application "Contacts"\n'
                         f'return vcard of person id "{_esc(uid)}"\n'
                         "end tell")
        if not out:
            return None
        bdir = DATA / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9-]", "", uid.split(":")[0])[:40]
        path = bdir / f"applecard-{safe}-{time.strftime('%Y%m%d-%H%M%S')}.vcf"
        path.write_text(out, encoding="utf-8")
        return str(path)
    except Exception:
        return None


# ---------- the push ----------

def push_person(pid):
    """Push a person's card state to every matched Apple card. Returns
    {ok, cards: [{uid, changed}], detail} and never raises — a failed
    push must not fail the save that triggered it."""
    if not enabled():
        return {"ok": False, "detail": "apple push not available here",
                "cards": []}
    try:
        from . import contactcard, data as crm
        detail = crm.get_person(pid)
        if not detail:
            return {"ok": False, "detail": "unknown person", "cards": []}
        card = contactcard.compose(pid, detail)
        fields = {f["key"]: f["value"] for f in card["fields"]}
        rows = card["handles"]
        emails = [r["value"] for r in rows if r["kind"] == "email"]
        phones = [r["value"] for r in rows if r["kind"] == "phone"]
        matches = find_cards(emails, phones)
        if not matches:
            return {"ok": False, "detail": "no Apple contact matched",
                    "cards": []}
        bday = _birthday_parts(fields.get("birthday"))
        results = []
        for m in matches:
            changed = []
            company = fields.get("company") or ""
            kw = {}
            if company and company != (m["org"] or ""):
                kw["company"] = company
                changed.append("company")
            if bday:
                kw["birthday"] = bday
                changed.append("birthday")
            eplan = plan_handles([r for r in rows if r["kind"] == "email"],
                                 m["emails"], "email")
            if not _same_handles(eplan, m["emails"], "email"):
                kw["emails"] = eplan
                changed.append("emails")
            pplan = plan_handles([r for r in rows if r["kind"] == "phone"],
                                 m["phones"], "phone")
            if not _same_handles(pplan, m["phones"], "phone"):
                kw["phones"] = pplan
                changed.append("phones")
            if not kw:
                results.append({"uid": m["uid"], "changed": []})
                continue
            backup_vcard(m["uid"])
            _osascript(build_script(m["uid"], **kw))
            results.append({"uid": m["uid"], "changed": changed})
        n = sum(1 for r in results if r["changed"])
        return {"ok": True, "cards": results,
                "detail": (f"updated {n} Apple card" + ("s" if n != 1 else "")
                           if n else "Apple contact already current")}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:300], "cards": []}
