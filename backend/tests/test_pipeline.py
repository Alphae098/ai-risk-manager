"""Tests for the decision path.

Everything here runs against an in-memory database and the offline reviewer, so
the suite needs no network, no API key and no trained model artifact.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from arm.agent.analyst import RiskAnalystAgent
from arm.config import BandConfig
from arm.db import init_db, insert_many
from arm.db import connect
from arm.features.engine import FEATURE_NAMES, FeatureEngine, vectorize
from arm.merchant.portfolio import compute_snapshot
from arm.scoring.router import DecisionPipeline
from arm.scoring.rules import RulesEngine, build_context

START = datetime(2026, 5, 1, 12, 0, 0)


@pytest.fixture()
def conn():
    connection = connect(":memory:")
    init_db(connection)
    insert_many(connection, "merchants", [{
        "id": "mch_1", "name": "Gaming Store", "category": "gaming",
        "onboarded_at": (START - timedelta(days=30)).isoformat(),
        "monthly_volume": 500000.0, "risk_tier": "elevated",
        "reserve_pct": 0.0, "txn_limit": 200000.0, "frozen": 0,
    }])
    insert_many(connection, "customers", [{
        "id": "cus_1", "first_seen": (START - timedelta(days=400)).isoformat(),
        "home_city": "Mumbai", "typical_ticket": 1000.0,
    }])
    connection.commit()
    yield connection
    connection.close()


def txn(index: int, **over) -> dict:
    base = {
        "id": "txn_%d" % index,
        "ts": (START + timedelta(seconds=index * 5)).isoformat(),
        "merchant_id": "mch_1", "customer_id": "cus_1",
        "amount": 1000.0, "method": "card", "device_id": "dev_1",
        "ip": "49.1.2.3", "card_fp": "card_1", "bin_country": "IN",
        "ip_country": "IN", "city": "Mumbai", "is_fraud": 0, "fraud_pattern": None,
    }
    base.update(over)
    return base


def entities(conn):
    merchants = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM merchants")}
    customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
    return merchants, customers


# ------------------------------------------------------------------- features

def test_feature_vector_matches_declared_names(conn):
    engine = FeatureEngine(*entities(conn))
    features = engine.compute(txn(0))
    assert set(features) == set(FEATURE_NAMES)
    assert len(vectorize(features)) == len(FEATURE_NAMES)


def test_velocity_counts_only_look_backwards(conn):
    engine = FeatureEngine(*entities(conn))
    first = engine.compute(txn(0))
    assert first["device_txn_1m"] == 0

    for index in range(1, 6):
        engine.compute(txn(index))
    later = engine.compute(txn(6))
    assert later["device_txn_1m"] == 6


def test_amount_ratio_is_relative_to_the_customer(conn):
    engine = FeatureEngine(*entities(conn))
    features = engine.compute(txn(0, amount=8000.0))
    assert features["amount_over_customer_typical"] == pytest.approx(8.0)


# ---------------------------------------------------------------------- rules

def test_hard_rule_blocks_without_scoring(conn):
    engine = FeatureEngine(*entities(conn))
    rules = RulesEngine()
    pipeline = DecisionPipeline(model=None, rules=rules, features=engine)

    # Six sub-100 attempts inside a minute is the card-testing rule.
    decision = None
    for index in range(7):
        decision = pipeline.decide(txn(index, amount=40.0))
    assert decision.band == "decline"
    assert "R003" in decision.rule_hits
    assert decision.needs_agent is False


def test_frozen_merchant_is_declined(conn):
    conn.execute("UPDATE merchants SET frozen = 1 WHERE id = 'mch_1'")
    conn.commit()
    engine = FeatureEngine(*entities(conn))
    pipeline = DecisionPipeline(model=None, rules=RulesEngine(), features=engine)
    decision = pipeline.decide(txn(0))
    assert "R001" in decision.rule_hits
    assert decision.action == "decline"


def test_a_broken_rule_does_not_break_scoring(tmp_path, conn):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "  - id: BAD\n"
        "    description: references a field that does not exist\n"
        "    when: \"nonexistent_feature > 1\"\n"
        "    action: block\n",
        encoding="utf-8")
    engine = FeatureEngine(*entities(conn))
    verdict = RulesEngine(path).evaluate(
        build_context(engine.compute(txn(0)), txn(0), {}))
    assert verdict.blocked is False


# --------------------------------------------------------------------- bands

def test_band_boundaries_are_inclusive_where_declared():
    bands = BandConfig(approve_below=0.15, decline_at=0.80)
    assert bands.band_of(0.149) == "approve"
    assert bands.band_of(0.15) == "review"
    assert bands.band_of(0.799) == "review"
    assert bands.band_of(0.80) == "decline"


def test_review_band_requests_an_agent_but_still_approves(conn):
    class HalfScoreModel:
        def score(self, vector):
            return 0.5

        def top_contributors(self, features, k=5):
            return []

    engine = FeatureEngine(*entities(conn))
    pipeline = DecisionPipeline(model=HalfScoreModel(), rules=RulesEngine(), features=engine)
    decision = pipeline.decide(txn(0))
    assert decision.band == "review"
    assert decision.needs_agent is True
    # The payment is not blocked while the agent investigates.
    assert decision.action == "approve_provisional"


# --------------------------------------------------------------------- agent

def test_agent_escalates_when_evidence_is_thin(conn):
    insert_many(conn, "transactions", [txn(0)])
    conn.execute(
        "INSERT INTO decisions (txn_id, ts, rule_hits, rule_boost, model_score, final_score,"
        " band, action, latency_ms, features) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("txn_0", txn(0)["ts"], "[]", 0.0, 0.5, 0.5, "review", "approve_provisional", 4.0, "{}"))
    conn.commit()

    case = RiskAnalystAgent(conn).review("txn_0")
    assert case.verdict in {"legit", "insufficient_evidence"}
    assert case.tool_calls, "the agent must gather evidence before deciding"


def test_agent_calls_fraud_on_a_shared_device_ring(conn):
    """Many customers on one device inside a day is the ring signature."""
    rows = [txn(index, id="txn_ring_%d" % index, customer_id="cus_1", amount=4000.0)
            for index in range(8)]
    for index, row in enumerate(rows):
        row["customer_id"] = "cus_1"
    insert_many(conn, "customers", [
        {"id": "cus_%d" % i, "first_seen": (START - timedelta(days=200)).isoformat(),
         "home_city": "Mumbai", "typical_ticket": 1000.0} for i in range(2, 10)])
    for index, row in enumerate(rows[1:], start=2):
        row["customer_id"] = "cus_%d" % index
    insert_many(conn, "transactions", rows)
    conn.execute(
        "INSERT INTO decisions (txn_id, ts, rule_hits, rule_boost, model_score, final_score,"
        " band, action, latency_ms, features) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("txn_ring_0", rows[0]["ts"], "[]", 0.0, 0.5, 0.5, "review",
         "approve_provisional", 4.0,
         '{"device_distinct_customers_24h": 8, "amount_over_customer_typical": 4.0}'))
    conn.commit()

    case = RiskAnalystAgent(conn).review("txn_ring_0")
    assert case.verdict == "fraud"
    assert any("device" in item["fact"].lower() for item in case.evidence)


def test_agent_case_is_persisted(conn):
    insert_many(conn, "transactions", [txn(0)])
    conn.commit()
    case = RiskAnalystAgent(conn).review("txn_0")
    stored = conn.execute("SELECT * FROM agent_cases WHERE id = ?", (case.id,)).fetchone()
    assert stored is not None
    assert stored["verdict"] == case.verdict


# ------------------------------------------------------------------ merchant

def test_merchant_snapshot_reflects_chargebacks(conn):
    rows = [txn(index, id="txn_m_%d" % index, amount=5000.0,
                ts=(START + timedelta(hours=index)).isoformat()) for index in range(20)]
    insert_many(conn, "transactions", rows)
    insert_many(conn, "outcomes", [
        {"txn_id": row["id"],
         "kind": "chargeback" if index < 6 else "clean",
         "arrived_at": (START + timedelta(days=10)).isoformat(),
         "analyst_override": None, "analyst_note": None}
        for index, row in enumerate(rows)])
    conn.commit()

    snapshot = compute_snapshot(conn, "mch_1", START + timedelta(days=2))
    assert snapshot is not None
    assert snapshot.cb_ratio == pytest.approx(0.30, abs=0.01)
    assert snapshot.risk_score >= 0.6
    assert snapshot.recommended_reserve_pct >= 0.10
    assert "chargeback ratio" in snapshot.memo


def test_quiet_merchant_needs_no_action(conn):
    rows = [txn(index, id="txn_q_%d" % index, amount=1200.0,
                ts=(START + timedelta(hours=index)).isoformat()) for index in range(30)]
    insert_many(conn, "transactions", rows)
    insert_many(conn, "outcomes", [
        {"txn_id": row["id"], "kind": "clean",
         "arrived_at": (START + timedelta(days=10)).isoformat(),
         "analyst_override": None, "analyst_note": None} for row in rows])
    conn.commit()

    snapshot = compute_snapshot(conn, "mch_1", START + timedelta(days=2))
    assert snapshot.cb_ratio == 0.0
    assert snapshot.recommended_reserve_pct == 0.0
    assert "no action" in snapshot.memo.lower()
