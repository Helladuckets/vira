"""Receipts pass: the email-evidence stream for subscriptions.

Charges say money LEFT; receipts are the only source that knows the future
(next billing date, plan names, price changes, cancellations) and the only
place off-pattern charges get explained (engine rule 8: an anomalous charge
is a new charge to VERIFY, never a silent repricing). This module finds the
receipt/invoice emails and turns them into `evidence` ledger rows.

Three candidate sources, local-first:
  1. The media index — mailindex.py already downloads every mail attachment
     and extracts document text, so invoice PDFs are searchable on disk
     before any network call.
  2. Microsoft Graph ($search) for the connected M365 mailbox.
  3. Gmail IMAP (X-GM-RAW, searching All Mail — receipts often skip the
     inbox via filters).

Extraction is the ONE AI step (suggest.complete, one call per merchant over
all its candidates). On the CLI backend, mail content never leaves the
machine — the same privacy boundary as reply drafting. Everything around the
model call is deterministic, and every evidence row keeps its message_ref so
a card can show "from receipt, Jun 6" and the row can be traced back.

Sweep order is targeted-first: merchants whose reconcile carries
evidence_needed entries (anomalous charges, cadence questions, amount
variance) sweep before the rest, and the anomaly context rides into the
extraction prompt ("find what explains the $762.13 on Jul 9").

Weekly background sweep (config `receipts_sweep_days`, dormant when no mail
account is configured) + on-demand per-merchant sweep from the card button.
"""
import email
import email.utils
import imaplib
import json
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import mail, msgraph, settings, subscriptions, suggest

DATA = Path(__file__).resolve().parent.parent / "data"
MEDIA_INDEX = DATA / "media-index.sqlite"
ACCOUNTS = DATA / "mail-accounts.json"

CAND_PER_SOURCE = 5          # newest candidates kept per merchant per source
TEXT_CAP = 2600              # chars of each candidate handed to the model
RECEIPT_TERMS = "receipt OR invoice OR renewal OR billing OR payment OR subscription"
EVIDENCE_KINDS = ("receipt", "renewal_notice", "price_change", "trial_end",
                  "cancel_confirm")


def _accounts():
    try:
        return json.loads(ACCOUNTS.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _merchant_terms(m):
    """Search terms for a merchant: display name + distinct aliases,
    longest first (most specific match first)."""
    terms = {m.get("display_name", "").strip()}
    terms.update(a.strip() for a in m.get("aliases", []))
    terms.update(s.strip() for s in m.get("receipt_senders", []))
    return sorted((t for t in terms if len(t) >= 3), key=len, reverse=True)


# ---------- candidate sources ----------

def _candidates_media(merchant):
    """Invoice-ish mail attachments already on disk with extracted text."""
    if not MEDIA_INDEX.exists():
        return []
    conn = sqlite3.connect(str(MEDIA_INDEX))
    conn.row_factory = sqlite3.Row
    out = []
    try:
        for term in _merchant_terms(merchant):
            like = f"%{term}%"
            rows = conn.execute(
                "SELECT i.seq, i.name, i.date_ns, co.doc_text FROM items i "
                "JOIN content co ON co.seq = i.seq "
                "WHERE i.source='email' AND co.doc_text != '' "
                "AND (co.doc_text LIKE ? OR i.name LIKE ?) "
                "ORDER BY i.date_ns DESC LIMIT ?",
                (like, like, CAND_PER_SOURCE)).fetchall()
            for r in rows:
                if any(c["ref"] == f"media:{r['seq']}" for c in out):
                    continue
                when = datetime.fromtimestamp(
                    r["date_ns"] / 1e9, tz=timezone.utc).date().isoformat()
                out.append({"ref": f"media:{r['seq']}", "account": "attachment",
                            "date": when, "subject": r["name"],
                            "text": r["doc_text"][:TEXT_CAP]})
            if out:
                break
    finally:
        conn.close()
    return out[:CAND_PER_SOURCE]


def _strip_html(html):
    html = re.sub(r"<style.*?</style>|<script.*?</script>", " ", html,
                  flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _search_queries(merchant):
    """Layered queries, most precise first: explicit receipt senders, then
    mail FROM the merchant (receipts come from the merchant; a broad name
    match drowns in newsletters that merely mention it), then the broad
    name + receipt-terms match as the last resort."""
    name = merchant.get("display_name", "").strip()
    token = re.sub(r"[^a-z0-9]", "", (name.split() or [""])[0].lower())
    qs = [f"from:{s.strip()}" for s in merchant.get("receipt_senders", [])
          if s.strip()]
    if token:
        qs.append(f"from:{token} ({RECEIPT_TERMS})")
        qs.append(f"from:{token}")
    if name:
        clean = re.sub(r'[()"]', " ", name).strip()
        qs.append(f"{clean} AND ({RECEIPT_TERMS})")
    return qs


def _candidates_graph(merchant, account_email):
    """$search the M365 mailbox; pull full bodies for the top hits."""
    hits = []
    for kql in _search_queries(merchant):
        # ONE pair of quotes around the whole KQL expression — a quoted
        # term inside an already-quoted $search value is a Graph 400.
        q = ("/me/messages?$search=" + urllib.parse.quote(f'"{kql}"')
             + f"&$top={CAND_PER_SOURCE}"
             + "&$select=id,subject,from,receivedDateTime,bodyPreview")
        hits = msgraph._graph_request(account_email, q).get("value", [])
        if hits:
            break
    out = []
    for h in hits[:CAND_PER_SOURCE]:
        body = ""
        try:
            full = msgraph._graph_request(
                account_email, f"/me/messages/{h['id']}?$select=body")
            body = _strip_html(full.get("body", {}).get("content", ""))
        except Exception:  # noqa: BLE001 — bodyPreview still usable
            body = h.get("bodyPreview", "")
        sender = (h.get("from", {}).get("emailAddress", {}) or {}).get("address", "")
        out.append({"ref": f"graph:{h['id']}", "account": account_email,
                    "date": (h.get("receivedDateTime") or "")[:10],
                    "subject": f"{h.get('subject', '')} (from {sender})",
                    "text": body[:TEXT_CAP]})
    return out


def _candidates_imap(merchant, acct):
    """Search Gmail All Mail (X-GM-RAW) / generic IMAP for receipt mail."""
    addr, host = acct.get("email"), acct.get("host", "")
    password = mail.keychain_password(addr)
    if not password:
        return []
    name = merchant.get("display_name", "").strip()
    con = imaplib.IMAP4_SSL(host)
    out = []
    try:
        con.login(addr, password)
        gmail = "gmail" in host
        folder = '"[Gmail]/All Mail"' if gmail else "INBOX"
        con.select(folder, readonly=True)
        ids = []
        if gmail:
            for raw in _search_queries(merchant):
                typ, data = con.search(
                    None, "X-GM-RAW", f'"{raw.replace(chr(34), "")}"')
                ids = (data[0].split() if typ == "OK" and data and data[0]
                       else [])
                if ids:
                    break
        else:
            typ, data = con.search(None, "SUBJECT", f'"{name}"')
            ids = (data[0].split() if typ == "OK" and data and data[0] else [])
        for uid in reversed(ids[-CAND_PER_SOURCE:]):
            typ, msg_data = con.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            when = email.utils.parsedate_to_datetime(msg.get("Date"))
            body = mail._body_preview(msg, limit=TEXT_CAP)
            mid = (msg.get("Message-ID") or f"uid-{uid.decode()}").strip()
            out.append({"ref": f"imap:{mid}", "account": addr,
                        "date": when.date().isoformat() if when else "",
                        "subject": f"{mail._decode_header(msg.get('Subject'))} "
                                   f"(from {msg.get('From', '')})",
                        "text": body})
    finally:
        try:
            con.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def gather_candidates(merchant):
    """All three sources, local-first, errors isolated per source."""
    cands, errors = [], []
    for fn, label in ((lambda: _candidates_media(merchant), "media-index"),):
        try:
            cands.extend(fn())
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: {e}")
    for acct in _accounts():
        try:
            if acct.get("type") == "graph":
                cands.extend(_candidates_graph(merchant, acct["email"]))
            else:
                cands.extend(_candidates_imap(merchant, acct))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{acct.get('email', '?')}: {e}")
    seen, unique = set(), []
    for c in cands:
        if c["ref"] not in seen and c["text"].strip():
            seen.add(c["ref"])
            unique.append(c)
    return unique, errors


# ---------- AI extraction (the one model step) ----------

def _parse_rows(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        rows = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return rows if isinstance(rows, list) else []


def _extraction_prompt(merchant, candidates, anomalies):
    lines = [
        "You are extracting billing evidence for one merchant from email/",
        "document excerpts. Return ONLY a JSON array (no prose). Each element:",
        '{"candidate": <1-based index>, "kind": "receipt|renewal_notice|'
        'price_change|trial_end|cancel_confirm", "date": "YYYY-MM-DD",',
        '"amount": <number or null>, "next_billing_date": "YYYY-MM-DD" or null,',
        '"plan": "<short plan/tier/seat description or empty>"}',
        "Only emit rows the text actually supports — an empty array [] is a",
        "correct answer. Dates are the document's own dates, never today.",
        f"\nMerchant: {merchant.get('display_name')} "
        f"(aliases: {', '.join(merchant.get('aliases', []))})",
    ]
    if anomalies:
        lines.append(
            "\nPriority: these charges are OUTSIDE the recurring pattern and "
            "need explaining — if any excerpt accounts for one (an overage "
            "invoice, a plan change, an extra seat), extract it and describe "
            "it in `plan`:")
        for a in anomalies:
            lines.append(f"  - ${a['amount']} on {a['date']}")
    for i, c in enumerate(candidates, 1):
        lines.append(f"\n--- candidate {i} | {c['date']} | {c['subject']} "
                     f"| via {c['account']} ---\n{c['text']}")
    return "\n".join(lines)


def extract_evidence(merchant, candidates, anomalies=()):
    """One completion over all candidates -> validated evidence rows."""
    if not candidates:
        return []
    raw = suggest.complete(_extraction_prompt(merchant, candidates, anomalies))
    rows = []
    for r in _parse_rows(raw):
        try:
            idx = int(r.get("candidate", 0)) - 1
            cand = candidates[idx]
        except (ValueError, TypeError, IndexError):
            continue
        kind = r.get("kind")
        d = str(r.get("date") or "")[:10]
        if kind not in EVIDENCE_KINDS or not re.match(r"\d{4}-\d{2}-\d{2}", d):
            continue
        nbd = str(r.get("next_billing_date") or "")[:10]
        rows.append({
            "merchant_id": merchant["id"], "kind": kind, "date": d,
            "amount": (round(float(r["amount"]), 2)
                       if isinstance(r.get("amount"), (int, float)) else None),
            "next_billing_date": nbd if re.match(r"\d{4}-\d{2}-\d{2}", nbd) else None,
            "plan": str(r.get("plan") or "")[:200],
            "message_ref": cand["ref"], "account": cand["account"],
        })
    return rows


def store_evidence(conn, rows):
    """Idempotent insert: one row per (message_ref, kind, date)."""
    added = 0
    for r in rows:
        dup = conn.execute(
            "SELECT 1 FROM evidence WHERE message_ref=? AND kind=? AND date=?",
            (r["message_ref"], r["kind"], r["date"])).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO evidence (merchant_id, kind, date, amount, "
            "next_billing_date, plan, message_ref, account) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (r["merchant_id"], r["kind"], r["date"], r["amount"],
             r["next_billing_date"], r["plan"], r["message_ref"], r["account"]))
        added += 1
    conn.commit()
    return added


# ---------- the sweep ----------

_sweep_lock = threading.Lock()   # serialize background + card-button sweeps
                                 # (dedup keeps data safe either way; the lock
                                 # keeps the AI from extracting twice)


def sweep(merchant_ids=None, conn=None):
    """Targeted-first receipts pass. Returns a per-merchant summary."""
    own = conn is None
    conn = conn or subscriptions.ledger_connect()
    try:
        _sweep_lock.acquire()
        recon = subscriptions.reconcile(conn)
        by_id = {m["id"]: m for m in recon["merchants"]}
        reg = subscriptions.load_registry()
        merchants = [m for m in reg["merchants"]
                     if m.get("status") not in ("canceled", "ignored")]
        if merchant_ids:
            merchants = [m for m in merchants if m["id"] in set(merchant_ids)]
        # evidence-needed merchants first — the queue is the point
        merchants.sort(key=lambda m: -len(
            (by_id.get(m["id"]) or {}).get("evidence_needed", [])))

        summary = []
        for m in merchants:
            view = by_id.get(m["id"]) or {}
            anomalies = [e for e in view.get("evidence_needed", [])
                         if e["kind"] == "anomalous_charge"]
            cands, errors = gather_candidates(m)
            rows = extract_evidence(m, cands, anomalies) if cands else []
            added = store_evidence(conn, rows) if rows else 0
            summary.append({"merchant": m["id"], "candidates": len(cands),
                            "evidence_added": added,
                            **({"errors": errors} if errors else {})})
        subscriptions.meta_set(
            conn, "receipts_last_sweep",
            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        errors = [f"{s['merchant']}: {e}" for s in summary
                  for e in s.get("errors", [])]
        subscriptions.meta_set(conn, "receipts_last_errors",
                               json.dumps(errors[:40]))
        conn.commit()
        return summary
    finally:
        _sweep_lock.release()
        if own:
            conn.close()


class Sweeper(threading.Thread):
    """Weekly background sweep. Dormant until a mail account is configured
    AND the ledger has charges (nothing to explain before then)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.status = "starting"

    def run(self):
        while True:
            try:
                if not _accounts():
                    self.status = "dormant — no mail accounts configured"
                else:
                    conn = subscriptions.ledger_connect()
                    try:
                        last = subscriptions.meta_get(conn, "receipts_last_sweep")
                        days = float(settings.get("receipts_sweep_days") or 7)
                        due = (last is None or
                               (datetime.now(timezone.utc)
                                - datetime.fromisoformat(last)).days >= days)
                        has_charges = conn.execute(
                            "SELECT 1 FROM charges LIMIT 1").fetchone()
                    finally:
                        conn.close()
                    if due and has_charges:
                        self.status = "sweeping"
                        result = sweep()
                        added = sum(r["evidence_added"] for r in result)
                        self.status = (f"ok — {added} evidence rows at "
                                       f"{datetime.now().strftime('%H:%M')}")
                    elif not has_charges:
                        self.status = "dormant — ledger empty"
                    else:
                        self.status = f"idle — last sweep {last[:16]}"
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                self.status = f"error: {e}"
            time.sleep(3600)
