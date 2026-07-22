"""Subscriptions: merchant registry + charge ledger + deterministic cadence
engine — what does the owner actually pay for, at what cadence, and what
renews next?

Two stores, split by the code-data seam doctrine:

- data/subscriptions.json — the merchant REGISTRY. Human curation (canonical
  name, aliases, login URL, category, cadence override, lifecycle status).
  Non-regenerable; joins the backup rotation. Atomic tmp+rename writes.
- data/subs-ledger.sqlite — the charge LEDGER. Regenerable cache of Mercury
  charges (server/mercury.py writes it) and receipt evidence (phase 3).

The cadence engine (reconcile) is fully deterministic — no AI, no wall-clock
dependence. All math anchors to the newest ledger charge ("data through"),
never today's date: the stale-window deflation that broke the old page
(estimates sagging ~30% as the data aged) is impossible by construction.

Engine rules, in order:

1. Grouping: charges map to merchants via registry aliases (exact lowercase
   counterparty match). Unknown counterparties auto-create a stub merchant
   flagged needs_review.
2. Classification uses intervals between the FIRST charge of each distinct
   charge-bearing calendar month — NOT successive raw charges, because two
   concurrent subscriptions at one merchant interleave and destroy raw
   intervals (Anthropic: two subs -> gaps of 2-4 days that read as noise).
   Median month-collapsed interval -> band: 25-35d monthly, 75-105 quarterly,
   150-210 semi-annual, 330-400 annual. Single charge-bearing month ->
   one-time. Median in a gap between bands -> unclear.
3. Estimates: monthly -> median_low of per-calendar-month sums over the last
   3 charge-bearing months (recent window = the CURRENT subscription mix;
   median over all history undercounts after a second seat/plan is added,
   and median_low keeps the figure an actually-observed month). Annual /
   semi-annual / quarterly -> last cycle amount x1/x2/x4 for the year, /12
   monthly. One-time -> the charge IS the yearly figure, /12 monthly (the
   owner's rule).
4. Anchoring: "last charge" is by DATE (the old page read array position);
   staleness is displayed, never silently corrected for.
5. Projection: next renewal = last charge + median observed interval (band
   nominal when no interval). A receipt-derived next_billing_date overrides
   the projection when evidence exists (phase 3).
6. Lapse: a recurring merchant with no charge within 1.5x its period of
   data-through -> flag possibly_canceled.
7. Conflicts are surfaced, never silently resolved: observed cadence beats a
   disagreeing registry override for the math but raises cadence_conflict
   (Google One: catalog said annual, card says monthly). A cadence read from
   a single interval raises cadence_uncertain (Archer: two charges 77 days
   apart — quarterly or semi-annual? only the next charge can answer).
8. Evidence-first anomaly handling (the owner's rule, 2026-07-10): a charge
   outside a merchant's established pattern is A NEW CHARGE to verify, never
   a silent repricing of the monthly. Monthly merchants decompose into
   recurring same-amount STREAMS (two concurrent subs = two streams); the
   estimate is the sum of active streams, and off-stream charges (a one-off
   spike, an extra same-month occurrence) go to `evidence_needed` — "locate
   the invoice or receipt that classifies this" (overage? plan change?
   one-off?). Merchants whose amounts vary too much to form streams (usage
   billing, base-plus-overage) estimate by month-sum median and carry an
   amount_variance entry instead. The receipts pass (phase 3) consumes this
   queue; until then it renders as a review prompt — which is also the
   record-keeping nudge: everything here should have an invoice somewhere.
"""
import json
import re
import sqlite3
import statistics
import threading
from datetime import date, datetime
from pathlib import Path

from . import jsonstore, settings

DATA = Path(__file__).resolve().parent.parent / "data"
REGISTRY = DATA / "subscriptions.json"
LEDGER = DATA / "subs-ledger.sqlite"

STATUSES = ("active", "watching", "cancel-pending", "canceled", "ignored")
EXCLUDED_STATUSES = {"canceled", "ignored"}   # out of KPIs; ignored = "real
                                              # money-out, not a subscription"
                                              # (rent, card payments to other
                                              # banks, one-off vendor bills)
CADENCES = ("monthly", "quarterly", "semi-annual", "annual", "one-time", "unclear")

# (lo_days, hi_days, cadence, nominal_period_days, cycles_per_year)
BANDS = [
    (25, 35, "monthly", 30, 12),
    (75, 105, "quarterly", 91, 4),
    (150, 210, "semi-annual", 182, 2),
    (330, 400, "annual", 365, 1),
]
PERIOD_DAYS = {c: p for _, _, c, p, _ in BANDS}
CYCLES_PER_YEAR = {c: n for _, _, c, _, n in BANDS}

_reg_lock = threading.Lock()


# ---------- fixture mode (fresh public clone) ----------

def _fixture_seed():
    """Demo data for a fresh clone: the first touch of either store in
    fixture mode seeds a synthetic registry + ledger from fixtures/ into
    data/ (the crm_root copytree pattern), with charge dates generated
    relative to today so the demo always reads as live. Real mode — the
    CRM root exists — never enters here, and existing files are never
    overwritten."""
    try:
        if not settings.fixture_mode():
            return
    except Exception:  # noqa: BLE001 — settings trouble must not brick the engine
        return
    fx = settings.FIXTURES
    if not REGISTRY.exists() and (fx / "subscriptions.json").exists():
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY.write_bytes((fx / "subscriptions.json").read_bytes())
    if LEDGER.exists() or not (fx / "subs-charges.json").exists():
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(LEDGER))        # direct connect: ledger_connect
    conn.executescript(SCHEMA)                 # calls back into this seeder
    today = date.today().toordinal()
    charges = json.loads((fx / "subs-charges.json").read_text())
    for i, c in enumerate(charges):
        posted = date.fromordinal(today - int(c["days_ago"])).isoformat() \
            + "T12:00:00Z"
        conn.execute(
            "INSERT OR IGNORE INTO charges (merchant_id, amount, posted_at, "
            "source, counterparty, bank_description, mercury_note, "
            "mercury_category, dedup_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (c["merchant_id"], c["amount"], posted, "fixture",
             c.get("counterparty", c["merchant_id"]), c.get("desc", ""),
             c.get("note", ""), "", f"fx-{i}"))
    try:
        evidence = json.loads((fx / "subs-evidence.json").read_text())
    except (OSError, json.JSONDecodeError):
        evidence = []
    for e in evidence:
        nbd = (date.fromordinal(today + int(e["next_in_days"])).isoformat()
               if e.get("next_in_days") is not None else None)
        conn.execute(
            "INSERT INTO evidence (merchant_id, kind, date, amount, "
            "next_billing_date, plan, message_ref, account) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (e["merchant_id"], e["kind"],
             date.fromordinal(today - int(e["days_ago"])).isoformat(),
             e.get("amount"), nbd, e.get("plan", ""),
             e.get("message_ref", "fixture"), e.get("account", "fixture")))
    conn.commit()
    conn.close()


# ---------- registry ----------

def _blank_registry():
    return {"merchants": []}


def load_registry():
    _fixture_seed()
    try:
        reg = json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return _blank_registry()
    if not isinstance(reg, dict) or "merchants" not in reg:
        return _blank_registry()
    return reg


def save_registry(reg):
    jsonstore.write_atomic(REGISTRY, reg, indent=1, ensure_ascii=False)


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "unknown"


def match_merchant(counterparty, reg=None):
    """Exact lowercase alias match -> merchant id, else None."""
    reg = reg or load_registry()
    key = (counterparty or "").strip().lower()
    for m in reg["merchants"]:
        if key in [a.lower() for a in m.get("aliases", [])]:
            return m["id"]
    return None


_UNSET = object()


def _clean_pending_change(pc):
    """Validate + normalize a recorded change before it lands in the registry.
    A malformed record must never reach reconcile (the verifier trusts it)."""
    if not isinstance(pc, dict):
        raise ValueError("pending_change must be an object")
    kind = pc.get("kind")
    if kind not in CHANGE_KINDS:
        raise ValueError(f"bad change kind {kind!r}")
    eff = str(pc.get("effective_date") or "")
    if not re.match(r"\d{4}-\d{2}-\d{2}$", eff):
        raise ValueError("effective_date must be YYYY-MM-DD")
    out = {"kind": kind, "effective_date": eff}
    for k in ("new_plan", "source", "note", "recorded"):
        if pc.get(k):
            out[k] = str(pc[k]).strip()
    for k in ("new_amount", "prior_amount"):
        v = pc.get(k)
        if v is not None:
            out[k] = round(float(v), 2)
    return out


def update_merchant(mid, status=None, note=None, url=None,
                    cadence_override=_UNSET, needs_review=None,
                    pending_change=_UNSET):
    """Registry write-back for the UI (atomic, validated). cadence_override
    accepts a cadence, None to clear, or omitted to leave untouched.
    pending_change accepts a change record, None to clear, or omitted."""
    with _reg_lock:
        reg = load_registry()
        for m in reg["merchants"]:
            if m["id"] != mid:
                continue
            if status is not None:
                if status not in STATUSES:
                    raise ValueError(f"bad status {status!r}")
                m["status"] = status
            if note is not None:
                m["note"] = note.strip()
            if url is not None:
                m["url"] = url.strip()
            if cadence_override is not _UNSET:
                if cadence_override not in (None, "monthly", "quarterly",
                                            "semi-annual", "annual",
                                            "one-time"):
                    raise ValueError(f"bad cadence {cadence_override!r}")
                m["cadence_override"] = cadence_override
            if needs_review is not None:
                if needs_review:
                    m["needs_review"] = True
                else:
                    m.pop("needs_review", None)
            if pending_change is not _UNSET:
                if pending_change:
                    m["pending_change"] = _clean_pending_change(pending_change)
                else:
                    m.pop("pending_change", None)
            save_registry(reg)
            return m
    raise KeyError(mid)


def merchant_evidence(mid, conn=None):
    """Charge history + evidence rows for one merchant (card drill-in)."""
    own = conn is None
    conn = conn or ledger_connect()
    try:
        charges = [dict(r) for r in conn.execute(
            "SELECT amount, posted_at, source, bank_description, "
            "mercury_note, mercury_category FROM charges "
            "WHERE merchant_id=? ORDER BY posted_at DESC", (mid,))]
        evidence = [dict(r) for r in conn.execute(
            "SELECT kind, date, amount, next_billing_date, plan, "
            "message_ref, account FROM evidence "
            "WHERE merchant_id=? ORDER BY date DESC", (mid,))]
    finally:
        if own:
            conn.close()
    return {"charges": charges, "evidence": evidence}


def ensure_merchant(counterparty, reg=None):
    """Match an alias or auto-create a needs_review stub. Returns merchant id.

    Passing reg batches registry writes: mutations land on the caller's dict
    and the caller saves once (the poller does this per poll cycle)."""
    own = reg is None
    with _reg_lock:
        reg = reg if reg is not None else load_registry()
        mid = match_merchant(counterparty, reg)
        if mid:
            return mid
        base = slugify(counterparty)
        existing = {m["id"] for m in reg["merchants"]}
        mid, n = base, 2
        while mid in existing:
            mid, n = f"{base}-{n}", n + 1
        reg["merchants"].append({
            "id": mid,
            "display_name": (counterparty or "Unknown").strip() or "Unknown",
            "aliases": [(counterparty or "").strip().lower()],
            "url": "",
            "category": "Unknown",
            "cadence_override": None,
            "status": "active",
            "note": "",
            "receipt_senders": [],
            "needs_review": True,
        })
        if own:
            save_registry(reg)
        return mid


# ---------- ledger ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS charges (
  id INTEGER PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  amount REAL NOT NULL,             -- positive dollars (charge magnitude)
  posted_at TEXT NOT NULL,          -- ISO8601, sorts lexically
  source TEXT NOT NULL DEFAULT 'mercury',
  counterparty TEXT DEFAULT '',     -- raw name, so grouping can be redone
  bank_description TEXT DEFAULT '',
  mercury_note TEXT DEFAULT '',
  mercury_category TEXT DEFAULT '',
  dedup_key TEXT UNIQUE NOT NULL    -- mercury transaction id
);
CREATE INDEX IF NOT EXISTS idx_charges_merchant ON charges(merchant_id, posted_at);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  merchant_id TEXT NOT NULL,
  kind TEXT NOT NULL,               -- receipt|renewal_notice|price_change|trial_end|cancel_confirm
  date TEXT NOT NULL,
  amount REAL,
  next_billing_date TEXT,
  plan TEXT DEFAULT '',
  message_ref TEXT DEFAULT '',
  account TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def ledger_connect(path=None):
    if path is None:
        _fixture_seed()
    path = Path(path) if path else LEDGER
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_charge(conn, merchant_id, amount, posted_at, dedup_key,
                  source="mercury", counterparty="", bank_description="",
                  mercury_note="", mercury_category=""):
    conn.execute(
        """INSERT INTO charges (merchant_id, amount, posted_at, source,
                                counterparty, bank_description, mercury_note,
                                mercury_category, dedup_key)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(dedup_key) DO UPDATE SET
             amount=excluded.amount, posted_at=excluded.posted_at,
             mercury_note=excluded.mercury_note,
             mercury_category=excluded.mercury_category""",
        (merchant_id, amount, posted_at, source, counterparty,
         bank_description, mercury_note, mercury_category, dedup_key))


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


# ---------- cadence engine ----------

def _d(iso):
    return date.fromisoformat(str(iso)[:10])


def _classify(month_firsts):
    """Cadence + median interval from the first-charge-of-month date list."""
    if len(month_firsts) < 2:
        return "one-time", None
    gaps = [(b - a).days for a, b in zip(month_firsts, month_firsts[1:])]
    med = statistics.median(gaps)
    for lo, hi, cadence, _, _ in BANDS:
        if lo <= med <= hi:
            return cadence, med
    return "unclear", med


def _month_sums(charges):
    """Ordered {YYYY-MM: sum} over charge-bearing months only."""
    sums = {}
    for c in charges:
        key = str(c["posted_at"])[:7]
        sums[key] = round(sums.get(key, 0.0) + c["amount"], 2)
    return dict(sorted(sums.items()))


def _estimate(cadence, month_sums, last_cycle):
    """(monthly, yearly) per the plan's rules. last_cycle = newest
    charge-bearing month's sum (one full billing cycle for non-monthly)."""
    if cadence == "monthly":
        recent = list(month_sums.values())[-3:]
        monthly = statistics.median_low(recent)
        return round(monthly, 2), round(monthly * 12, 2)
    if cadence in ("quarterly", "semi-annual", "annual"):
        yearly = last_cycle * CYCLES_PER_YEAR[cadence]
        return round(yearly / 12, 2), round(yearly, 2)
    # one-time: the charge is the yearly figure (the owner's rule); unclear
    # falls back to the same conservative treatment and carries its flag.
    yearly = last_cycle if cadence == "one-time" else sum(month_sums.values())
    return round(yearly / 12, 2), round(yearly, 2)


STREAM_TOL = 0.01          # same-amount tolerance (1%) for stream membership
STREAM_MAX_GAP = 45        # median days between occurrences for a monthly stream
ANOMALY_WINDOW_DAYS = 90   # only recent off-pattern charges nag for evidence

# ---------- pending-change verification (curated, registry-borne) ----------
# A cancel / downgrade / price change the owner confirmed off a billing page
# or email, recorded on the merchant's REGISTRY entry (curated,
# non-regenerable, in the backup rotation) — NOT the regenerable ledger, since
# an on-page confirmation has no email the receipts pass could ever re-derive.
# reconcile checks it against the ledger once data passes the effective date:
# did the new price actually take, or did the old charge quietly recur?
# Deterministic — anchored to charge dates + data_through, never today's date.
CHANGE_KINDS = ("downgrade", "cancel", "price_change")
CHANGE_TAX_TOL = 0.15    # a post-change charge may carry up to ~15% sales tax
CHANGE_GRACE_DAYS = 5    # the ledger must reach this far past the effective
                         # date before an ABSENT charge reads as "it took"


def _amount_streams(charges):
    """Decompose a monthly merchant's charges into recurring same-amount
    streams. Returns (streams, leftover): each stream is a date-ordered
    charge list of ~equal amounts recurring at monthly spacing; leftover is
    every charge in no stream (one-off spikes, overages, price points seen
    once). Charges must arrive date-sorted."""
    groups = []
    for c in charges:
        for g in groups:
            ref = g[0]["amount"]
            if abs(c["amount"] - ref) <= STREAM_TOL * max(ref, 1.0):
                g.append(c)
                break
        else:
            groups.append([c])
    streams, leftover = [], []
    for g in groups:
        if len(g) < 2:
            leftover.extend(g)
            continue
        dates = [_d(c["posted_at"]) for c in g]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        # allow a same-month double (tiny gap) without breaking the stream,
        # but a median gap past monthly spacing is not a monthly stream
        if statistics.median(gaps) > STREAM_MAX_GAP:
            leftover.extend(g)
        else:
            streams.append(g)
    return streams, leftover


def _stream_estimate(charges, data_through):
    """Evidence-first monthly estimate (rule 8). Returns
    (monthly, anomalies) or (None, []) when streams don't explain the
    merchant (then the caller falls back to month-sum medians)."""
    streams, leftover = _amount_streams(charges)
    if not streams:
        return None, []
    anomalies = []
    active_total = 0.0
    covered = 0
    for s in streams:
        covered += len(s)
        seen_months = set()
        for c in s:
            mk = str(c["posted_at"])[:7]
            if mk in seen_months:      # second same-month occurrence
                anomalies.append(c)
            seen_months.add(mk)
        last = _d(s[-1]["posted_at"])
        if (data_through - last).days <= 1.5 * PERIOD_DAYS["monthly"]:
            active_total += statistics.median([c["amount"] for c in s])
    if covered * 2 < len(charges) or active_total == 0:
        return None, []                # streams don't explain this merchant
    for c in leftover:
        if (data_through - _d(c["posted_at"])).days <= ANOMALY_WINDOW_DAYS:
            anomalies.append(c)
    return round(active_total, 2), anomalies


def _change_amount_matches(charge, stated):
    """A ledger charge matches a stated pre-tax amount if it lands within the
    stream tolerance below and up to sales tax above (the stated figure on a
    receipt is pre-tax; the card charge carries tax)."""
    if not stated:
        return False
    return stated * (1 - STREAM_TOL) <= charge <= stated * (1 + CHANGE_TAX_TOL)


def _verify_pending_change(pc, charges, data_through):
    """Check a recorded cancel/downgrade/price-change against the ledger.

    Returns (view, flags): `view` echoes the registry record plus a
    `verification` (pending|verified|failed|review) and a human `detail`;
    `flags` are the reconcile flags the outcome raises. `charges` must be
    date-sorted. A change with a non-zero `new_amount` should PRODUCE a
    charge near that amount on/after the effective date; a cancel (or a
    downgrade to a zero-cost plan) should STOP producing charges."""
    eff = _d(pc["effective_date"])
    new_amt = pc.get("new_amount")
    prior = pc.get("prior_amount")
    kind = pc.get("kind") or "change"
    label = pc.get("new_plan") or kind
    expect_charge = bool(new_amt)          # None / 0 -> the change should stop billing
    post = [c for c in charges if _d(c["posted_at"]) >= eff]
    view = {k: pc.get(k) for k in
            ("kind", "effective_date", "new_plan", "new_amount",
             "prior_amount", "source", "recorded", "note")}
    flags = []

    def money(x):
        return f"${x:,.2f}"

    if expect_charge:
        new_c = next((c for c in post
                      if _change_amount_matches(c["amount"], new_amt)), None)
        old_c = next((c for c in post
                      if _change_amount_matches(c["amount"], prior)), None)
        if new_c:
            view["verification"] = "verified"
            view["detail"] = (f"new price took — charged {money(new_c['amount'])} "
                              f"on {str(new_c['posted_at'])[:10]} "
                              f"(expected ~{money(new_amt)})")
        elif old_c:
            view["verification"] = "failed"
            view["detail"] = (f"still billing the prior {money(prior)} — "
                              f"{money(old_c['amount'])} on "
                              f"{str(old_c['posted_at'])[:10]}; the {kind} did "
                              "not take")
            flags.append("change_not_applied")
        elif post:
            c = post[-1]
            view["verification"] = "review"
            view["detail"] = (f"charge {money(c['amount'])} on "
                              f"{str(c['posted_at'])[:10]} after "
                              f"{pc['effective_date']} matches neither the prior "
                              f"{money(prior) if prior else '—'} nor the new "
                              f"{money(new_amt)} — verify")
            flags.append("change_unexpected")
        else:
            view["verification"] = "pending"
            view["detail"] = (f"awaiting {pc['effective_date']}: expect "
                              f"~{money(new_amt)}"
                              + (f", not {money(prior)}" if prior else ""))
            flags.append("change_pending")
    else:
        if post:
            c = post[0]
            view["verification"] = "failed"
            view["detail"] = (f"expected no charge after {pc['effective_date']} "
                              f"({label}), but charged {money(c['amount'])} on "
                              f"{str(c['posted_at'])[:10]}")
            flags.append("change_not_applied")
        elif data_through and (data_through - eff).days >= CHANGE_GRACE_DAYS:
            view["verification"] = "verified"
            view["detail"] = (f"no charge since {pc['effective_date']} — "
                              f"{label} confirmed")
        else:
            view["verification"] = "pending"
            view["detail"] = (f"awaiting {pc['effective_date']}: expect no "
                              f"further charge ({label})")
            flags.append("change_pending")
    return view, flags


def merchant_view(merchant, charges, data_through, evidence=()):
    """Reconcile one merchant's registry entry with its ledger charges and
    receipt evidence."""
    flags = []
    if merchant.get("needs_review"):
        flags.append("needs_review")
    override = merchant.get("cadence_override")

    pending_change = None
    pc = merchant.get("pending_change")
    if pc and pc.get("effective_date"):
        pending_change, pc_flags = _verify_pending_change(
            pc, sorted(charges, key=lambda c: str(c["posted_at"])),
            data_through)
        flags.extend(pc_flags)

    if not charges:
        if evidence:               # plan: "evidence-only, no bank charge seen"
            flags.append("evidence_only")
        return {**_public(merchant), "cadence": override or "unclear",
                "cadence_source": "override" if override else "none",
                "monthly": 0.0, "yearly": 0.0, "charges": 0,
                "last_charge": None, "first_charge": None,
                "next_renewal": None, "renewal_source": None, "months": {},
                "flags": flags, "evidence_needed": [],
                "pending_change": pending_change}

    charges = sorted(charges, key=lambda c: str(c["posted_at"]))  # by DATE
    dates = [_d(c["posted_at"]) for c in charges]
    month_firsts, seen = [], set()
    for dt in dates:
        if (dt.year, dt.month) not in seen:
            seen.add((dt.year, dt.month))
            month_firsts.append(dt)

    observed, med_interval = _classify(month_firsts)
    n_intervals = len(month_firsts) - 1

    # Observed cadence wins the math; a disagreeing human override is
    # surfaced, never silently obeyed or discarded (rule 7).
    if observed == "one-time" and override:
        cadence, source = override, "override"
    else:
        cadence, source = observed, "observed"
        if override and override != observed:
            flags.append("cadence_conflict")
        if n_intervals == 1 and observed != "one-time":
            flags.append("cadence_uncertain")

    month_sums = _month_sums(charges)
    last_cycle = list(month_sums.values())[-1]
    monthly, yearly = _estimate(cadence, month_sums, last_cycle)

    # Rule 8: evidence-first anomaly handling for monthly merchants.
    evidence_needed = []
    if cadence == "monthly":
        stream_monthly, anomalies = _stream_estimate(charges, data_through)
        if stream_monthly is not None:
            monthly, yearly = stream_monthly, round(stream_monthly * 12, 2)
            for c in sorted(anomalies, key=lambda c: str(c["posted_at"])):
                evidence_needed.append({
                    "kind": "anomalous_charge",
                    "date": str(c["posted_at"])[:10], "amount": c["amount"],
                    "detail": "outside the recurring pattern — locate the "
                              "invoice/receipt to classify it (overage? plan "
                              "change? one-off?)"})
        else:
            amounts = [c["amount"] for c in charges]
            if max(amounts) - min(amounts) > STREAM_TOL * max(amounts):
                evidence_needed.append({
                    "kind": "amount_variance",
                    "detail": "amounts vary month to month (base plus "
                              "overages?) — receipts would pin down the "
                              "committed rate"})
    if "cadence_uncertain" in flags:
        evidence_needed.append({
            "kind": "cadence_unconfirmed",
            "detail": "cadence read from a single interval — an invoice "
                      "would confirm the billing period"})
    if "cadence_conflict" in flags:
        evidence_needed.append({
            "kind": "cadence_conflict",
            "detail": "observed cadence disagrees with the registry — a "
                      "receipt settles which is right"})
    if any(e["kind"] == "anomalous_charge" for e in evidence_needed):
        flags.append("unverified_charges")

    last = charges[-1]
    period = med_interval or PERIOD_DAYS.get(cadence)
    next_renewal = None
    if cadence not in ("one-time", "unclear") and period:
        nxt = date.fromordinal(dates[-1].toordinal() + round(period))
        next_renewal = nxt.isoformat()
        if (data_through - dates[-1]).days > 1.5 * period:
            flags.append("possibly_canceled")
    renewal_source = "projection" if next_renewal else None

    # Receipt evidence refines the picture (rules 5 + 8): explained
    # anomalies resolve in place, a receipt-stated next billing date beats
    # the projection, and a cancellation confirmation newer than the last
    # charge is worth a flag of its own.
    ev_rows = sorted((dict(e) for e in evidence), key=lambda e: e["date"])
    if ev_rows:
        last_iso = dates[-1].isoformat()
        for item in evidence_needed:
            if item["kind"] != "anomalous_charge":
                continue
            for ev in ev_rows:
                if ev.get("amount") is None:
                    continue
                close_amt = abs(ev["amount"] - item["amount"]) <= \
                    STREAM_TOL * max(item["amount"], 1.0)
                close_date = abs((_d(ev["date"]) - _d(item["date"])).days) <= 7
                if close_amt and close_date:
                    item["kind"] = "anomaly_explained"
                    item["detail"] = (ev["kind"].replace("_", " ")
                                      + (f" — {ev['plan']}" if ev["plan"] else "")
                                      + f" (from {ev['account']}, {ev['date']})")
                    break
        if ("unverified_charges" in flags and not any(
                e["kind"] == "anomalous_charge" for e in evidence_needed)):
            flags.remove("unverified_charges")
        stated = [e for e in ev_rows if e.get("next_billing_date")
                  and e["next_billing_date"] >= last_iso]
        if stated and cadence != "one-time":
            next_renewal = stated[-1]["next_billing_date"]
            renewal_source = "receipt"
        if any(e["kind"] == "cancel_confirm" and e["date"] >= last_iso
               for e in ev_rows):
            flags.append("cancel_confirmed")

    return {**_public(merchant),
            "cadence": cadence, "cadence_source": source,
            "monthly": monthly, "yearly": yearly,
            "charges": len(charges),
            "last_charge": {"amount": last["amount"],
                            "date": str(last["posted_at"])[:10],
                            "note": last["mercury_note"] or ""},
            "first_charge": dates[0].isoformat(),
            "next_renewal": next_renewal, "renewal_source": renewal_source,
            "months": month_sums, "flags": flags,
            "evidence_needed": evidence_needed,
            "pending_change": pending_change}


def _public(merchant):
    return {k: merchant.get(k) for k in
            ("id", "display_name", "url", "category", "status", "note",
             "cadence_override")}


def reconcile(conn=None, registry=None):
    """Full reconciled view: every merchant with charges or registry presence,
    estimates, flags, and KPIs. Deterministic; anchored to the ledger."""
    own = conn is None
    conn = conn or ledger_connect()
    try:
        reg = registry or load_registry()
        rows = conn.execute("SELECT * FROM charges").fetchall()
        ev_rows = conn.execute("SELECT * FROM evidence").fetchall()
    finally:
        if own:
            conn.close()

    by_merchant = {}
    for r in rows:
        by_merchant.setdefault(r["merchant_id"], []).append(dict(r))
    ev_by_merchant = {}
    for r in ev_rows:
        ev_by_merchant.setdefault(r["merchant_id"], []).append(dict(r))

    data_through = max((_d(r["posted_at"]) for r in rows), default=None)
    merchants = []
    known = set()
    for m in reg["merchants"]:
        known.add(m["id"])
        merchants.append(merchant_view(m, by_merchant.get(m["id"], []),
                                       data_through,
                                       ev_by_merchant.get(m["id"], [])))
    for mid, charges in by_merchant.items():   # ledger rows with no registry
        if mid not in known:                   # entry (shouldn't happen; the
            stub = {"id": mid, "display_name": mid,   # poller ensures first)
                    "status": "active", "needs_review": True}
            merchants.append(merchant_view(stub, charges, data_through,
                                           ev_by_merchant.get(mid, [])))

    counted = [m for m in merchants if m["status"] not in EXCLUDED_STATUSES]
    kpis = {
        "monthly_run_rate": round(sum(m["monthly"] for m in counted), 2),
        "annualized": round(sum(m["yearly"] for m in counted), 2),
        "by_cadence": {c: len([m for m in counted if m["cadence"] == c and m["charges"]])
                       for c in CADENCES},
        "flagged": len([m for m in merchants if m["flags"]]),
        "evidence_needed": sum(
            len([e for e in m["evidence_needed"]
                 if e["kind"] != "anomaly_explained"]) for m in counted),
    }
    merchants.sort(key=lambda m: -m["monthly"])
    return {"merchants": merchants, "kpis": kpis,
            "data_through": data_through.isoformat() if data_through else None,
            "staleness_days": (date.today() - data_through).days
                              if data_through else None}
