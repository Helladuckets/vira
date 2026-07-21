"""Cadence-engine regression tests over fully synthetic fixtures.

The charge histories below are invented, but each one replicates — shape
for shape — a hand-verified failure mode from the founding analysis the
engine was built against: one-time charges annualized by proration,
stale-window deflation of a true monthly, interleaved concurrent
same-merchant streams, base-plus-overage variable amounts, cadence read
from a single interval, and observation-vs-catalog conflicts. The shapes
are the regression contract; every expected number derives from these
synthetic fixtures. If one of these breaks, the engine has re-acquired a
bug the predecessor shipped with.

Run: .venv/bin/python -m unittest discover tests
"""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from server import brief, mercury, notify, receipts, settings, subscriptions

# Minimal registry mirroring catalog-seeded entries for the six merchants.
REG = {"merchants": [
    {"id": "nimbus-labs", "display_name": "Nimbus Labs",
     "aliases": ["nimbus labs"], "cadence_override": "monthly",
     "status": "active"},
    {"id": "querybird", "display_name": "Querybird",
     "aliases": ["querybird"], "cadence_override": "monthly",
     "status": "active"},
    {"id": "parcelscope", "display_name": "ParcelScope",
     "aliases": ["parcelscope"], "cadence_override": "monthly",
     "status": "active"},
    {"id": "cloudloft", "display_name": "Cloudloft", "aliases": ["cloudloft"],
     "cadence_override": "annual", "status": "active"},
    {"id": "aerodrome", "display_name": "Aerodrome", "aliases": ["aerodrome"],
     "cadence_override": "annual", "status": "active"},
    {"id": "brightline-media", "display_name": "Brightline",
     "aliases": ["brightline media"], "cadence_override": "one-time",
     "status": "active"},
    {"id": "skiff", "display_name": "Skiff Analytics",
     "aliases": ["skiff", "skiff analytics"], "cadence_override": None,
     "status": "active"},
]}


def txn(tid, name, amount, posted, kind="creditCardTransaction", **extra):
    return {"id": tid, "counterpartyName": name, "amount": amount,
            "postedAt": posted + "T12:00:00.000Z", "kind": kind, **extra}


# Synthetic charge histories, July-12 data cutoff.
FIXTURE = [
    txn("nl1", "Nimbus Labs", -183.50, "2026-03-09"),
    txn("nl2", "Nimbus Labs", -183.50, "2026-04-09"),
    txn("nl3", "Nimbus Labs", -183.50, "2026-05-09"),
    txn("nl4", "Nimbus Labs", -91.75, "2026-05-11"),
    txn("nl5", "Nimbus Labs", -183.50, "2026-06-05"),
    txn("nl6", "Nimbus Labs", -183.50, "2026-06-09"),
    txn("nl7", "Nimbus Labs", -91.75, "2026-06-12"),
    txn("nl8", "Nimbus Labs", -183.50, "2026-07-09"),
    txn("nl9", "Nimbus Labs", -642.40, "2026-07-12"),
    txn("qb1", "Querybird", -38.45, "2026-03-10"),
    txn("qb2", "Querybird", -38.45, "2026-04-10"),
    txn("qb3", "Querybird", -38.45, "2026-05-10"),
    txn("qb4", "Querybird", -38.45, "2026-06-10"),
    txn("pl1", "ParcelScope", -161.28, "2026-01-17"),
    txn("pl2", "ParcelScope", -161.28, "2026-02-16"),
    txn("pl3", "ParcelScope", -161.28, "2026-03-16"),
    txn("pl4", "ParcelScope", -161.28, "2026-04-16"),
    txn("pl5", "ParcelScope", -161.28, "2026-05-16"),
    txn("pl6", "ParcelScope", -161.28, "2026-06-16"),
    txn("ad1", "Aerodrome", -1200.00, "2026-03-20"),
    txn("ad2", "Aerodrome", -1200.00, "2026-06-05"),
    txn("cl1", "Cloudloft", -243.10, "2026-05-15"),
    txn("cl2", "Cloudloft", -198.20, "2026-06-15", note="storage bundle"),
    txn("bl1", "Brightline Media", -1380.00, "2026-05-11"),
]


class EngineGroundTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = subscriptions.ledger_connect(":memory:")
        reg = {"merchants": [dict(m) for m in REG["merchants"]]}
        mercury.ingest(cls.conn, FIXTURE, reg)
        cls.result = subscriptions.reconcile(cls.conn, registry=reg)
        cls.by_id = {m["id"]: m for m in cls.result["merchants"]}

    def m(self, mid):
        return self.by_id[mid]

    # -- the six verified failure shapes --------------------------------

    def test_brightline_one_time_is_not_annualized(self):
        """Predecessor bug: a one-time charge annualized by proration.
        Rule: the charge IS the yearly figure, /12 for the monthly
        equivalent."""
        m = self.m("brightline-media")
        self.assertEqual(m["cadence"], "one-time")
        self.assertEqual(m["yearly"], 1380.00)
        self.assertEqual(m["monthly"], 115.00)
        self.assertIsNone(m["next_renewal"])

    def test_querybird_true_monthly(self):
        """Predecessor bug: a true monthly deflated via stale-window
        proration."""
        m = self.m("querybird")
        self.assertEqual(m["cadence"], "monthly")
        self.assertEqual(m["monthly"], 38.45)
        self.assertEqual(m["flags"], [])

    def test_parcelscope_true_monthly(self):
        m = self.m("parcelscope")
        self.assertEqual(m["cadence"], "monthly")
        self.assertEqual(m["monthly"], 161.28)

    def test_parcelscope_no_false_lapse_at_window_edge(self):
        """Data ends Jul 12; ParcelScope bills the 16th-ish. The missing
        July charge is a window artifact, not a lapse — anchoring to
        data-through (not wall clock) must keep this clean."""
        self.assertNotIn("possibly_canceled", self.m("parcelscope")["flags"])

    def test_nimbus_concurrent_subs(self):
        """Two concurrent subscriptions (183.50 + 91.75) interleave: raw
        successive-charge intervals collapse to 2-4 days. Stream
        decomposition must see two active monthly streams at the combined
        rate."""
        m = self.m("nimbus-labs")
        self.assertEqual(m["cadence"], "monthly")
        self.assertEqual(m["monthly"], 275.25)

    def test_anomalous_charges_flag_not_reprice(self):
        """The owner's rule: an off-pattern charge is A NEW CHARGE to verify
        with an invoice, never a silent repricing. June's extra 183.50 and
        July's 642.40 spike must land in evidence_needed while the monthly
        stays at the stream rate (275.25, asserted above)."""
        m = self.m("nimbus-labs")
        self.assertIn("unverified_charges", m["flags"])
        anoms = [e for e in m["evidence_needed"]
                 if e["kind"] == "anomalous_charge"]
        self.assertEqual([(a["date"], a["amount"]) for a in anoms],
                         [("2026-06-09", 183.50), ("2026-07-12", 642.40)])

    def test_variable_amounts_ask_for_receipts(self):
        """Cloudloft's two amounts (243.10 / 198.20) form no stream —
        base-plus-overage billing estimates by month-sum median and asks
        for receipts instead of pretending precision."""
        m = self.m("cloudloft")
        self.assertIn("amount_variance",
                      [e["kind"] for e in m["evidence_needed"]])

    def test_cadence_ambiguity_requests_evidence(self):
        """Aerodrome: cadence read from one interval + registry
        disagreement — both should queue evidence requests for the
        receipts pass."""
        kinds = [e["kind"] for e in self.m("aerodrome")["evidence_needed"]]
        self.assertIn("cadence_unconfirmed", kinds)
        self.assertIn("cadence_conflict", kinds)

    def test_aerodrome_flagged_not_silently_priced(self):
        """Two $1,200 charges 77 days apart: reads quarterly, but one
        interval is not a pattern and the catalog said annual — both flags
        must surface (the predecessor had no conflict flagging at all)."""
        m = self.m("aerodrome")
        self.assertEqual(m["cadence"], "quarterly")
        self.assertEqual(m["yearly"], 4800.00)
        self.assertEqual(m["monthly"], 400.00)
        self.assertIn("cadence_uncertain", m["flags"])
        self.assertIn("cadence_conflict", m["flags"])

    def test_cloudloft_observation_beats_override(self):
        """Catalog said annual; the card shows monthly charges. Observation
        wins the math, the disagreement is flagged, never silently
        resolved."""
        m = self.m("cloudloft")
        self.assertEqual(m["cadence"], "monthly")
        self.assertEqual(m["cadence_source"], "observed")
        self.assertEqual(m["monthly"], 198.20)
        self.assertIn("cadence_conflict", m["flags"])

    # -- anchoring + structural regressions -----------------------------

    def test_data_through_is_newest_ledger_charge(self):
        self.assertEqual(self.result["data_through"], "2026-07-12")

    def test_last_charge_by_date_not_insert_order(self):
        """The predecessor read last-charge by array position; ours must be
        by date regardless of ledger insert order."""
        last = self.m("nimbus-labs")["last_charge"]
        self.assertEqual(last["date"], "2026-07-12")
        self.assertEqual(last["amount"], 642.40)

    def test_mercury_note_carried_through(self):
        self.assertEqual(self.m("cloudloft")["last_charge"]["note"],
                         "storage bundle")

    def test_next_renewal_projection(self):
        """Next renewal = last charge + median observed interval."""
        self.assertEqual(self.m("querybird")["next_renewal"], "2026-07-11")

    def test_kpis_sum_the_ground_truth(self):
        run_rate = self.result["kpis"]["monthly_run_rate"]
        self.assertEqual(run_rate, round(
            275.25 + 38.45 + 161.28 + 400.00 + 198.20 + 115.00, 2))


class EvidenceRefinement(unittest.TestCase):
    """Receipts evidence refining the reconcile (rules 5 + 8)."""

    def _reconcile_with_evidence(self, ev_rows):
        conn = subscriptions.ledger_connect(":memory:")
        reg = {"merchants": [dict(m) for m in REG["merchants"]]}
        mercury.ingest(conn, FIXTURE, reg)
        for r in ev_rows:
            conn.execute(
                "INSERT INTO evidence (merchant_id, kind, date, amount, "
                "next_billing_date, plan, message_ref, account) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r["merchant_id"], r["kind"], r["date"], r.get("amount"),
                 r.get("next_billing_date"), r.get("plan", ""),
                 r.get("message_ref", "test:1"), r.get("account", "test")))
        conn.commit()
        result = subscriptions.reconcile(conn, registry=reg)
        return {m["id"]: m for m in result["merchants"]}

    def test_receipt_next_billing_date_overrides_projection(self):
        by_id = self._reconcile_with_evidence([
            {"merchant_id": "querybird", "kind": "renewal_notice",
             "date": "2026-07-04", "next_billing_date": "2026-07-24"}])
        m = by_id["querybird"]
        self.assertEqual(m["next_renewal"], "2026-07-24")   # not Jul 11
        self.assertEqual(m["renewal_source"], "receipt")

    def test_matching_receipt_explains_anomaly(self):
        """The July 642.40 spike gets an invoice: its chip resolves to
        anomaly_explained; June's extra 183.50 stays open, so the
        unverified_charges flag survives."""
        by_id = self._reconcile_with_evidence([
            {"merchant_id": "nimbus-labs", "kind": "receipt",
             "date": "2026-07-12", "amount": 642.40,
             "plan": "usage overage", "account": "mailbox"}])
        m = by_id["nimbus-labs"]
        kinds = {(e["kind"], e.get("amount")) for e in m["evidence_needed"]}
        self.assertIn(("anomaly_explained", 642.40), kinds)
        self.assertIn(("anomalous_charge", 183.50), kinds)
        self.assertIn("unverified_charges", m["flags"])
        explained = next(e for e in m["evidence_needed"]
                         if e["kind"] == "anomaly_explained")
        self.assertIn("usage overage", explained["detail"])

    def test_cancel_confirm_after_last_charge_flags(self):
        by_id = self._reconcile_with_evidence([
            {"merchant_id": "parcelscope", "kind": "cancel_confirm",
             "date": "2026-07-04"}])
        self.assertIn("cancel_confirmed", by_id["parcelscope"]["flags"])

    def test_evidence_only_merchant_flagged(self):
        by_id = self._reconcile_with_evidence([
            {"merchant_id": "skiff", "kind": "receipt",
             "date": "2026-06-23", "amount": 14.0}])
        # skiff has no fixture charges -> evidence-only badge
        self.assertIn("evidence_only", by_id["skiff"]["flags"])


class ReceiptExtraction(unittest.TestCase):
    """The deterministic shell around the one AI step."""

    CANDS = [
        {"ref": "graph:abc", "account": "m365", "date": "2026-07-12",
         "subject": "Your Nimbus Labs receipt",
         "text": "Receipt: $642.40 usage"},
        {"ref": "imap:<x@y>", "account": "gmail", "date": "2026-06-09",
         "subject": "Invoice", "text": "Nimbus Team seat $183.50"},
    ]
    MERCHANT = {"id": "nimbus-labs", "display_name": "Nimbus Labs",
                "aliases": ["nimbus labs"]}

    def test_extraction_validates_and_maps_refs(self):
        canned = ('Here you go:\n[{"candidate": 1, "kind": "receipt", '
                  '"date": "2026-07-12", "amount": 642.40, '
                  '"next_billing_date": null, "plan": "usage overage"}, '
                  '{"candidate": 2, "kind": "bogus_kind", "date": "2026-06-09"}, '
                  '{"candidate": 9, "kind": "receipt", "date": "2026-06-09"}]')
        with mock.patch.object(receipts.suggest, "complete",
                               return_value=canned):
            rows = receipts.extract_evidence(self.MERCHANT, self.CANDS,
                                             [{"amount": 642.40,
                                               "date": "2026-07-12"}])
        self.assertEqual(len(rows), 1)      # bad kind + bad index dropped
        self.assertEqual(rows[0]["message_ref"], "graph:abc")
        self.assertEqual(rows[0]["amount"], 642.40)
        self.assertEqual(rows[0]["merchant_id"], "nimbus-labs")

    def test_extraction_tolerates_no_json(self):
        with mock.patch.object(receipts.suggest, "complete",
                               return_value="I found nothing relevant."):
            rows = receipts.extract_evidence(self.MERCHANT, self.CANDS)
        self.assertEqual(rows, [])

    def test_store_evidence_idempotent(self):
        conn = subscriptions.ledger_connect(":memory:")
        row = {"merchant_id": "nimbus-labs", "kind": "receipt",
               "date": "2026-07-12", "amount": 642.40,
               "next_billing_date": None, "plan": "x",
               "message_ref": "graph:abc", "account": "m365"}
        self.assertEqual(receipts.store_evidence(conn, [row]), 1)
        self.assertEqual(receipts.store_evidence(conn, [row]), 0)
        n = conn.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"]
        self.assertEqual(n, 1)


def _fake_reconcile(days_out, cadence="monthly", yearly=2388.00,
                    monthly=199.00, status="active", flags=(), evidence=()):
    renewal = (date.today() + timedelta(days=days_out)).isoformat() \
        if days_out is not None else None
    return {"merchants": [{
        "id": "demo", "display_name": "Demo Co", "status": status,
        "cadence": cadence, "monthly": monthly, "yearly": yearly,
        "charges": 4, "next_renewal": renewal, "renewal_source": "projection",
        "flags": list(flags), "evidence_needed": list(evidence)}],
        "kpis": {"monthly_run_rate": monthly, "annualized": yearly,
                 "by_cadence": {}, "flagged": 0, "evidence_needed": 0},
        "data_through": date.today().isoformat(), "staleness_days": 0}


class BriefSection(unittest.TestCase):
    def test_renewals_and_attention(self):
        fake = _fake_reconcile(3, flags=["possibly_canceled"],
                               evidence=[{"kind": "amount_variance",
                                          "detail": "x"}])
        with mock.patch.object(subscriptions, "reconcile", return_value=fake):
            s = brief._subs_section()
        self.assertEqual(s["renewals"][0]["in_days"], 3)
        self.assertEqual(s["attention"][0]["evidence"], 1)
        self.assertIn("possibly_canceled", s["attention"][0]["flags"])

    def test_section_none_when_ledger_empty(self):
        empty = {"merchants": [], "kpis": {"monthly_run_rate": 0,
                 "annualized": 0}, "data_through": None, "staleness_days": None}
        with mock.patch.object(subscriptions, "reconcile", return_value=empty):
            self.assertIsNone(brief._subs_section())


class RenewalPings(unittest.TestCase):
    def _run(self, fake, tmp):
        sent = []
        with mock.patch.object(notify, "config", return_value={
                "enabled": True, "handle": "demo-handle", "tier": "active"}), \
             mock.patch.object(notify, "_send",
                               side_effect=lambda h, t, i: sent.append(t)), \
             mock.patch.object(notify, "_throttled", return_value=None), \
             mock.patch.object(subscriptions, "reconcile", return_value=fake), \
             mock.patch.object(subscriptions, "LEDGER",
                               Path(tmp) / "ledger.sqlite"):
            first = notify.subs_renewals()
            second = notify.subs_renewals()
        return sent, first, second

    def test_pings_above_threshold_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent, first, second = self._run(_fake_reconcile(2), tmp)
        self.assertEqual((first, second), (1, 0))    # dedup on 2nd poll
        self.assertIn("Demo Co renews in 2d", sent[0])
        self.assertIn("$199.00 per monthly cycle", sent[0])

    def test_cheap_monthly_skipped_but_annual_always_pings(self):
        cheap = _fake_reconcile(2, monthly=4.10, yearly=49.20)
        with tempfile.TemporaryDirectory() as tmp:
            sent, first, _ = self._run(cheap, tmp)
        self.assertEqual(first, 0)
        annual = _fake_reconcile(5, cadence="annual", monthly=8.0, yearly=96.0)
        with tempfile.TemporaryDirectory() as tmp:
            sent, first, _ = self._run(annual, tmp)
        self.assertEqual(first, 1)
        self.assertIn("per annual cycle", sent[0])

    def test_far_renewals_and_ignored_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, first, _ = self._run(_fake_reconcile(12), tmp)
        self.assertEqual(first, 0)
        with tempfile.TemporaryDirectory() as tmp:
            _, first, _ = self._run(_fake_reconcile(2, status="ignored"), tmp)
        self.assertEqual(first, 0)


class FixtureMode(unittest.TestCase):
    def test_fresh_clone_seeds_demo_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(settings, "fixture_mode",
                                   return_value=True), \
                 mock.patch.object(subscriptions, "REGISTRY",
                                   Path(tmp) / "subscriptions.json"), \
                 mock.patch.object(subscriptions, "LEDGER",
                                   Path(tmp) / "subs-ledger.sqlite"):
                r = subscriptions.reconcile()
            by_id = {m["id"]: m for m in r["merchants"]}
            self.assertEqual(len(by_id), 5)
            quill = by_id["quill-notes"]                # receipt-driven renewal
            self.assertEqual(quill["renewal_source"], "receipt")
            self.assertLessEqual(
                (date.fromisoformat(quill["next_renewal"]) - date.today()).days, 7)
            hexa = by_id["hexagon-ai"]                  # streams + both chip states
            self.assertEqual(hexa["monthly"], 41.0)
            kinds = [e["kind"] for e in hexa["evidence_needed"]]
            self.assertIn("anomaly_explained", kinds)
            self.assertIn("anomalous_charge", kinds)
            self.assertIn("possibly_canceled", by_id["photon-vpn"]["flags"])
            self.assertIn("cadence_conflict", by_id["datastream"]["flags"])

    def test_fixture_stores_stable_across_dates(self):
        """Regression for the 2026-07-21 date drift: days_ago offsets
        crossing month boundaries flipped hexagon-ai's cadence to unclear
        (monthly read 22.5, not 41.0). The demo seed must satisfy the same
        assertions on ANY calendar day — sweep a month-and-a-bit of
        synthetic todays through the whole seed + reconcile path."""
        real_date = subscriptions.date
        for offset in range(0, 36):
            fake_today = real_date.today() + timedelta(days=offset)

            class FakeDate(real_date):
                @classmethod
                def today(cls):
                    return cls(fake_today.year, fake_today.month,
                               fake_today.day)

            with tempfile.TemporaryDirectory() as tmp:
                with mock.patch.object(settings, "fixture_mode",
                                       return_value=True), \
                     mock.patch.object(subscriptions, "date", FakeDate), \
                     mock.patch.object(subscriptions, "REGISTRY",
                                       Path(tmp) / "subscriptions.json"), \
                     mock.patch.object(subscriptions, "LEDGER",
                                       Path(tmp) / "subs-ledger.sqlite"):
                    r = subscriptions.reconcile()
                by_id = {m["id"]: m for m in r["merchants"]}
                msg = f"today={fake_today.isoformat()}"
                hexa = by_id["hexagon-ai"]
                self.assertEqual(hexa["monthly"], 41.0, msg)
                kinds = [e["kind"] for e in hexa["evidence_needed"]]
                self.assertIn("anomaly_explained", kinds, msg)
                self.assertIn("anomalous_charge", kinds, msg)
                self.assertIn("possibly_canceled",
                              by_id["photon-vpn"]["flags"], msg)
                self.assertIn("cadence_conflict",
                              by_id["datastream"]["flags"], msg)
                quill = by_id["quill-notes"]
                self.assertEqual(quill["renewal_source"], "receipt", msg)
                self.assertLessEqual(
                    (date.fromisoformat(quill["next_renewal"])
                     - fake_today).days, 7, msg)


class GroupingAndIngest(unittest.TestCase):
    def test_alias_match_display_name_variant(self):
        """Mercury sends a case variant of the display name; the seeded
        display-name alias must catch it."""
        reg = {"merchants": [dict(m) for m in REG["merchants"]]}
        self.assertEqual(subscriptions.ensure_merchant("Skiff Analytics", reg),
                         "skiff")

    def test_unknown_counterparty_creates_review_stub(self):
        reg = {"merchants": []}
        mid = subscriptions.ensure_merchant("Some New SaaS", reg)
        self.assertEqual(mid, "some-new-saas")
        stub = reg["merchants"][0]
        self.assertTrue(stub["needs_review"])
        self.assertIn("some new saas", stub["aliases"])

    def test_ingest_filters_and_idempotency(self):
        conn = subscriptions.ledger_connect(":memory:")
        reg = {"merchants": []}
        rows = [
            txn("t1", "Acme", -10.00, "2026-06-01"),
            txn("t2", "Refund Co", 25.00, "2026-06-01"),          # credit
            txn("t3", "Me", -500.00, "2026-06-01",
                kind="internalTransfer"),                          # own move
            {"id": "t4", "counterpartyName": "Pending Co",
             "amount": -9.0, "postedAt": None, "kind": "creditCardTransaction"},
            txn("t5", "Failed Co", -9.0, "2026-06-01", status="failed"),
        ]
        n, newest = mercury.ingest(conn, rows, reg)
        self.assertEqual(n, 1)
        self.assertTrue(newest.startswith("2026-06-01"))
        n2, _ = mercury.ingest(conn, rows, reg)                    # re-run
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM charges").fetchone()["c"], 1)

    def test_card_autopay_not_double_counted(self):
        """Checking->card balance payments (counterparty "Mercury Credit")
        are the same dollars as the individual card charges — skip them."""
        conn = subscriptions.ledger_connect(":memory:")
        n, _ = mercury.ingest(conn, [
            txn("t1", "Mercury Credit", -2801.22, "2026-06-30"),
            txn("t2", "Acme", -10.00, "2026-06-01"),
        ], {"merchants": []})
        self.assertEqual(n, 1)

    def test_ignored_status_excluded_from_kpis(self):
        """status=ignored: real money-out that isn't a subscription (rent,
        payments to other card issuers) stays visible but out of KPIs."""
        conn = subscriptions.ledger_connect(":memory:")
        reg = {"merchants": [
            {"id": "rent-co", "display_name": "Rent Co", "aliases": ["rent co"],
             "cadence_override": None, "status": "ignored"},
            {"id": "saas-co", "display_name": "SaaS Co", "aliases": ["saas co"],
             "cadence_override": None, "status": "active"},
        ]}
        mercury.ingest(conn, [txn("r1", "Rent Co", -1500.0, "2026-06-01"),
                              txn("s1", "SaaS Co", -12.0, "2026-06-01")], reg)
        r = subscriptions.reconcile(conn, registry=reg)
        self.assertEqual(r["kpis"]["monthly_run_rate"], 1.0)   # 12/12 only

    def test_charges_stored_positive(self):
        conn = subscriptions.ledger_connect(":memory:")
        mercury.ingest(conn, [txn("t1", "Acme", -12.34, "2026-06-01")],
                       {"merchants": []})
        row = conn.execute("SELECT amount FROM charges").fetchone()
        self.assertEqual(row["amount"], 12.34)


def _chg(amount, iso, note=""):
    return {"amount": amount, "posted_at": iso + "T12:00:00Z",
            "mercury_note": note}


class PendingChangeVerification(unittest.TestCase):
    """Verify-on-date: a recorded cancel/downgrade checked against the ledger
    once data passes the effective date. The record lives on the registry
    (curated, backed up), never the regenerable evidence ledger — a page
    confirmation has no email the receipts pass could re-derive."""

    def _view(self, pc, charges, data_through):
        merchant = {"id": "m", "display_name": "M", "aliases": ["m"],
                    "status": "active", "pending_change": pc}
        return subscriptions.merchant_view(
            merchant, charges, date.fromisoformat(data_through))

    def test_downgrade_pending_before_effective_date(self):
        pc = {"kind": "downgrade", "effective_date": "2026-08-14",
              "new_amount": 14.99, "prior_amount": 149.99}
        v = self._view(pc, [_chg(163.11, "2026-06-15"),
                            _chg(163.11, "2026-07-15")], "2026-07-17")
        self.assertEqual(v["pending_change"]["verification"], "pending")
        self.assertIn("change_pending", v["flags"])
        self.assertNotIn("change_not_applied", v["flags"])

    def test_downgrade_verified_when_new_price_charged(self):
        pc = {"kind": "downgrade", "effective_date": "2026-08-14",
              "new_amount": 14.99, "prior_amount": 149.99}
        v = self._view(pc, [_chg(163.11, "2026-07-15"),
                            _chg(16.31, "2026-08-14")], "2026-08-16")
        self.assertEqual(v["pending_change"]["verification"], "verified")
        self.assertNotIn("change_pending", v["flags"])
        self.assertNotIn("change_not_applied", v["flags"])

    def test_downgrade_failed_when_old_price_recurs(self):
        pc = {"kind": "downgrade", "effective_date": "2026-08-14",
              "new_amount": 14.99, "prior_amount": 149.99}
        v = self._view(pc, [_chg(163.11, "2026-07-15"),
                            _chg(163.11, "2026-08-15")], "2026-08-16")
        self.assertEqual(v["pending_change"]["verification"], "failed")
        self.assertIn("change_not_applied", v["flags"])

    def test_cancel_verified_by_absence_after_grace(self):
        pc = {"kind": "downgrade", "effective_date": "2026-08-09",
              "new_plan": "Free", "new_amount": 0, "prior_amount": 20.0}
        v = self._view(pc, [_chg(21.79, "2026-06-10"),
                            _chg(21.79, "2026-07-10")], "2026-08-23")
        self.assertEqual(v["pending_change"]["verification"], "verified")
        self.assertNotIn("change_pending", v["flags"])
        self.assertNotIn("change_not_applied", v["flags"])

    def test_cancel_failed_when_still_charged(self):
        pc = {"kind": "cancel", "effective_date": "2026-08-09",
              "new_amount": 0}
        v = self._view(pc, [_chg(21.79, "2026-07-10"),
                            _chg(21.79, "2026-08-10")], "2026-08-13")
        self.assertEqual(v["pending_change"]["verification"], "failed")
        self.assertIn("change_not_applied", v["flags"])

    def test_cancel_pending_before_grace(self):
        pc = {"kind": "cancel", "effective_date": "2026-08-09",
              "new_amount": 0}
        v = self._view(pc, [_chg(21.79, "2026-07-10")], "2026-08-09")
        self.assertEqual(v["pending_change"]["verification"], "pending")
        self.assertIn("change_pending", v["flags"])

    def test_clean_pending_change_validates_and_normalizes(self):
        with self.assertRaises(ValueError):
            subscriptions._clean_pending_change(
                {"kind": "bogus", "effective_date": "2026-08-09"})
        with self.assertRaises(ValueError):
            subscriptions._clean_pending_change(
                {"kind": "cancel", "effective_date": "not-a-date"})
        ok = subscriptions._clean_pending_change(
            {"kind": "downgrade", "effective_date": "2026-08-14",
             "new_amount": "14.99", "prior_amount": 149.99,
             "new_plan": " Plus Tier ", "extra": "dropped"})
        self.assertEqual(ok["new_amount"], 14.99)
        self.assertEqual(ok["new_plan"], "Plus Tier")
        self.assertNotIn("extra", ok)


if __name__ == "__main__":
    unittest.main()
