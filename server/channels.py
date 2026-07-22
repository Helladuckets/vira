"""Shared channel mechanics: the pieces every inbound-channel connector
(mail, M365 Graph, WhatsApp, the Android companion) was carrying
privately.

Plain functions, not a class hierarchy — each connector keeps its own
watcher/poll shape and calls in with its own handles:

  push_feed_item     the ONE live-feed push contract (was mail.py
                     _push_item's tail, whatsapp._push,
                     companion._push_feed)
  first_run_baseline the first-poll contract: no watermark yet ->
                     baseline at the newest existing item, emit
                     NOTHING old
  imap_newest_uid    the STATUS UIDNEXT probe both IMAP baselines used
  graph_newest_received  the Graph top-1 probe both Graph baselines used
  mail_accounts /    the tolerant mail-accounts.json reader (+ the
  graph_accounts     graph-only filter brief.py wants)
  imap_special_folder  the RFC 6154 special-use mailbox finder
                     (\\Drafts for mail.py, \\All for mailindex.py)

Nothing here talks to the network on import; graph_newest_received
lazy-imports msgraph so channels stays dependency-light.
"""
import json
import re
from pathlib import Path

ACCOUNTS = Path(__file__).resolve().parent.parent / "data" / "mail-accounts.json"


# ---------- the feed-push contract ----------

def push_feed_item(shared, item):
    """Append one item to the shared live feed (the iMessage watcher's
    feed/listeners/lock handles): dedupe by rowid (the backstop against
    any refetch echo — one rowid, one feed item), keep the feed sorted
    by `when` and capped at feed_size, wake every SSE listener and drop
    dead queues. Returns True when the item entered the feed, False on
    a rowid dupe."""
    with shared.lock:
        if any(x.get("rowid") == item["rowid"] for x in shared.feed):
            return False
        shared.feed.append(item)
        shared.feed.sort(key=lambda i: i.get("when") or "")
        shared.feed = shared.feed[-shared.feed_size:]
        for q in list(shared.listeners):
            try:
                q.put_nowait(item)
            except Exception:  # noqa: BLE001 — dead SSE queue
                shared.listeners.remove(q)
    return True


# ---------- first-run baseline ----------

def first_run_baseline(watermark, newest):
    """The first-poll contract every channel watcher follows: with no
    watermark yet, baseline at the newest existing item (`newest` is a
    zero-arg callable, only invoked on first run) and emit nothing old.
    Returns (watermark, baselined)."""
    if watermark is not None:
        return watermark, False
    return newest(), True


def imap_newest_uid(con, mailbox):
    """The newest existing UID of a mailbox via STATUS UIDNEXT — avoids
    listing a huge mailbox (imaplib caps response lines at 1 MB, which
    "SEARCH ALL" can exceed). 0 when the response doesn't parse."""
    _, data = con.status(mailbox, "(UIDNEXT)")
    m = re.search(rb"UIDNEXT (\d+)", data[0])
    return int(m.group(1)) - 1 if m else 0


def graph_newest_received(email_addr, base_path):
    """The newest receivedDateTime under a Graph messages path (top-1
    descending), or the epoch when the mailbox is empty — the Graph
    flavor of the first-run baseline."""
    from . import msgraph
    top = msgraph._graph_request(
        email_addr, f"{base_path}?$orderby=receivedDateTime%20desc"
                    "&$top=1&$select=receivedDateTime")
    vals = top.get("value", [])
    return vals[0]["receivedDateTime"] if vals else "1970-01-01T00:00:00Z"


# ---------- accounts ----------

def mail_accounts(path=None):
    """Parsed mail-accounts.json, tolerating both the bare-list and
    {"accounts": [...]} shapes the file has carried. Missing or corrupt
    file reads as no accounts."""
    try:
        raw = json.loads((path or ACCOUNTS).read_text())
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else raw.get("accounts", [])


def graph_accounts(path=None):
    """The Graph-connected subset of the configured accounts."""
    return [a for a in mail_accounts(path) if a.get("type") == "graph"]


# ---------- IMAP folders ----------

def imap_special_folder(con, flag, default):
    """The mailbox carrying an RFC 6154 special-use flag (\\Drafts,
    \\All, ...) per LIST; Gmail names these "[Gmail]/Drafts" /
    "[Gmail]/All Mail". Falls back to default."""
    status, boxes = con.list()
    if status == "OK":
        for raw in boxes or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) \
                else str(raw)
            if flag in line:
                m = re.findall(r'"([^"]+)"', line)
                if m:
                    return m[-1]
    return default
