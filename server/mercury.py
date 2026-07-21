"""Mercury charge poller: the bank-side evidence stream for subscriptions.

Deterministic — pulls transactions from the Mercury API past a watermark and
upserts them into the charge ledger (server/subscriptions.py). Each charge
carries Mercury's own note field through, so annotating a transaction in the
Mercury dashboard ("teammate's seat", "cancel after close") is the primary
curation channel.

Dormant until a token is configured (the mail-watcher pattern). Setup, one
time:

  1. In Mercury (app.mercury.com) -> Settings -> API Tokens, create a token
     with READ ONLY permission. Read-only tokens cannot send money, create
     transfers, or manage recipients, and need no IP allowlist.
  2. Store it in the macOS Keychain (never in a file):
       security add-generic-password -U -a mercury -s vira-mercury -w

The poller self-heals within a minute of the token landing — no restart.
Poll cadence: config "mercury_poll_hours" (default 6) — subscriptions move
slowly; a poller that catches up when the Mac wakes is fine.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from . import secrets, settings, subscriptions

API = "https://api.mercury.com/api/v1"
PAGE = 500
OVERLAP_DAYS = 7          # re-fetch window behind the watermark; upsert dedups
FIRST_RUN_START = "2025-01-01"
SKIP_KINDS = {"internalTransfer"}   # own-account moves are never subscriptions
# Card-balance autopay from checking: the same dollars land again as the
# individual card charges via the /credit account — counting both doubles
# every card subscription.
SKIP_COUNTERPARTIES = {"mercury credit"}


def keychain_service():
    return settings.keychain_service("vira-mercury")


def keychain_token():
    return secrets.get(keychain_service()) or None


def _get(path, token, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def fetch_accounts(token):
    """Deposit accounts PLUS credit accounts. /accounts only lists
    checking/savings; the credit card — where nearly every subscription
    bills — is a separate /credit listing whose ids work with the same
    /account/{id}/transactions endpoint."""
    accounts = _get("/accounts", token).get("accounts", [])
    try:
        accounts += _get("/credit", token).get("accounts", [])
    except urllib.error.HTTPError as e:
        if e.code != 404:      # no credit product on the account is fine
            raise
    return accounts


def fetch_transactions(token, account_id, start):
    """All transactions for one account since `start` (ISO date), paged."""
    out, offset = [], 0
    while True:
        page = _get(f"/account/{account_id}/transactions", token,
                    {"limit": PAGE, "offset": offset, "start": start})
        txns = page.get("transactions", [])
        out.extend(txns)
        offset += len(txns)
        if len(txns) < PAGE or offset >= int(page.get("total") or 0):
            return out


def ingest(conn, txns, reg=None):
    """Filter to settled outbound charges, group to merchants, upsert.
    Returns (ingested_count, newest_posted_at_or_None)."""
    own_reg = reg is None
    reg = reg if reg is not None else subscriptions.load_registry()
    count, newest = 0, None
    for t in txns:
        amount = t.get("amount") or 0
        posted = t.get("postedAt")
        if (amount >= 0 or not posted
                or t.get("kind") in SKIP_KINDS
                or t.get("status") in ("pending", "cancelled", "failed")):
            continue
        counterparty = (t.get("counterpartyName") or
                        t.get("bankDescription") or "Unknown")
        if counterparty.strip().lower() in SKIP_COUNTERPARTIES:
            continue
        mid = subscriptions.ensure_merchant(counterparty, reg)
        subscriptions.upsert_charge(
            conn, mid, round(-amount, 2), posted, t["id"],
            counterparty=counterparty,
            bank_description=t.get("bankDescription") or "",
            mercury_note=t.get("note") or "",
            mercury_category=t.get("mercuryCategory") or "")
        count += 1
        newest = max(newest or posted, posted)
    if own_reg:
        subscriptions.save_registry(reg)
    return count, newest


def poll_once(conn=None):
    """One full poll: accounts -> transactions since watermark -> ledger.
    Raises on auth/network errors (caller renders them into status)."""
    token = keychain_token()
    if not token:
        raise RuntimeError(f"no token in keychain (service {keychain_service()})")
    own = conn is None
    conn = conn or subscriptions.ledger_connect()
    try:
        watermark = subscriptions.meta_get(conn, "mercury_watermark")
        if watermark:
            start = date.fromordinal(
                date.fromisoformat(watermark[:10]).toordinal()
                - OVERLAP_DAYS).isoformat()
        else:
            start = FIRST_RUN_START
        reg = subscriptions.load_registry()
        total, newest_all = 0, watermark
        for acct in fetch_accounts(token):
            txns = fetch_transactions(token, acct["id"], start)
            n, newest = ingest(conn, txns, reg)
            total += n
            if newest:
                newest_all = max(newest_all or newest, newest)
        subscriptions.save_registry(reg)
        if newest_all:
            subscriptions.meta_set(conn, "mercury_watermark", newest_all)
        subscriptions.meta_set(
            conn, "mercury_last_poll",
            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        conn.commit()
        return total
    finally:
        if own:
            conn.close()


class Poller(threading.Thread):
    """Background poll loop. Ticks every minute so a freshly-added Keychain
    token is picked up fast; actually polls every mercury_poll_hours."""

    def __init__(self):
        super().__init__(daemon=True)
        self.status = "starting"
        self.next_poll = 0.0

    def poll_now(self):
        self.next_poll = 0.0

    def run(self):
        while True:
            try:
                if not keychain_token():
                    self.status = f"no token in keychain (service {keychain_service()})"
                elif time.time() >= self.next_poll:
                    n = poll_once()
                    hours = float(settings.get("mercury_poll_hours") or 6)
                    self.next_poll = time.time() + hours * 3600
                    self.status = (f"ok — {n} charges ingested at "
                                   f"{datetime.now().strftime('%H:%M')}")
                    try:                     # renewal radar rides the poll
                        from . import notify
                        notify.subs_renewals()
                    except Exception:  # noqa: BLE001 — never break the poll
                        pass
            except urllib.error.HTTPError as e:
                self.status = f"mercury api {e.code}"
                self.next_poll = time.time() + 1800   # back off 30 min
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                self.status = f"error: {e}"
                self.next_poll = time.time() + 1800
            time.sleep(60)
