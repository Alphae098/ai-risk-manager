"""Merchant portfolio risk.

Transaction scoring answers "is this payment fraudulent". This layer answers
the slower and often more expensive question: "is this merchant becoming a
liability". It rolls outcomes into a daily risk score per merchant, recommends
a rolling reserve and a per-transaction limit, and writes an underwriting memo.

Card networks place a merchant into a monitoring program at roughly a 0.9%
chargeback ratio, so that threshold anchors the scoring rather than an
arbitrary number.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

CB_MONITORING_THRESHOLD = 0.009   # card-network monitoring programme entry point
LOOKBACK_DAYS = 30


@dataclass
class MerchantSnapshot:
    merchant_id: str
    date: str
    risk_score: float
    cb_ratio: float
    volume: float
    volume_delta: float
    fraud_rate: float
    recommended_reserve_pct: float
    recommended_txn_limit: float
    memo: str

    def as_row(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _window_stats(conn: sqlite3.Connection, merchant_id: str,
                  start: str, end: str) -> dict[str, float]:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(t.amount), 0) AS value,"
        "       COALESCE(SUM(CASE WHEN o.kind = 'chargeback' THEN 1 ELSE 0 END), 0) AS cbs,"
        "       COALESCE(SUM(CASE WHEN o.kind = 'chargeback' THEN t.amount ELSE 0 END), 0) AS cb_value,"
        "       COALESCE(SUM(CASE WHEN o.kind = 'refund' THEN 1 ELSE 0 END), 0) AS refunds"
        "  FROM transactions t LEFT JOIN outcomes o ON o.txn_id = t.id"
        " WHERE t.merchant_id = ? AND t.ts >= ? AND t.ts < ?",
        (merchant_id, start, end)).fetchone()
    return {k: float(row[k]) for k in row.keys()}


def _declined_rate(conn: sqlite3.Connection, merchant_id: str,
                   start: str, end: str) -> float:
    row = conn.execute(
        "SELECT COUNT(*) AS n,"
        "       SUM(CASE WHEN d.band = 'decline' THEN 1 ELSE 0 END) AS declines"
        "  FROM decisions d JOIN transactions t ON t.id = d.txn_id"
        " WHERE t.merchant_id = ? AND t.ts >= ? AND t.ts < ?",
        (merchant_id, start, end)).fetchone()
    total = float(row["n"] or 0)
    return float(row["declines"] or 0) / total if total else 0.0


def compute_snapshot(conn: sqlite3.Connection, merchant_id: str,
                     as_of: datetime) -> MerchantSnapshot | None:
    end = as_of.isoformat()
    start = (as_of - timedelta(days=LOOKBACK_DAYS)).isoformat()
    prior_start = (as_of - timedelta(days=2 * LOOKBACK_DAYS)).isoformat()

    current = _window_stats(conn, merchant_id, start, end)
    if current["n"] == 0:
        return None
    prior = _window_stats(conn, merchant_id, prior_start, start)

    cb_ratio = current["cb_value"] / current["value"] if current["value"] else 0.0
    volume_delta = current["value"] / prior["value"] if prior["value"] > 0 else 1.0
    fraud_rate = _declined_rate(conn, merchant_id, start, end)
    refund_rate = current["refunds"] / current["n"] if current["n"] else 0.0

    merchant = conn.execute("SELECT * FROM merchants WHERE id = ?", (merchant_id,)).fetchone()
    age_days = (as_of - datetime.fromisoformat(merchant["onboarded_at"])).days

    # Weighted composite. Chargeback ratio dominates because it is the one that
    # carries network penalties; the rest are leading indicators.
    risk_score = min(1.0, (
        0.45 * min(cb_ratio / CB_MONITORING_THRESHOLD, 2.0) / 2.0
        + 0.20 * min(max(volume_delta - 1.0, 0.0) / 3.0, 1.0)
        + 0.15 * min(fraud_rate / 0.05, 1.0)
        + 0.10 * min(refund_rate / 0.10, 1.0)
        + 0.10 * (1.0 if age_days < 90 else 0.0)
    ))

    # The composite weights several leading indicators, which means a merchant
    # can sit mid-scale while its chargeback ratio is already multiples of the
    # threshold that triggers network penalties. Past that point the ratio is
    # not one signal among several - it is the finding - so it sets a floor.
    if cb_ratio >= 2 * CB_MONITORING_THRESHOLD:
        risk_score = max(risk_score, 0.85)
    elif cb_ratio >= CB_MONITORING_THRESHOLD:
        risk_score = max(risk_score, 0.65)

    reserve = _recommend_reserve(risk_score, cb_ratio)
    limit = _recommend_limit(merchant["txn_limit"], risk_score, current)
    memo = write_memo(merchant, risk_score, cb_ratio, volume_delta, fraud_rate,
                      refund_rate, age_days, reserve, limit)

    return MerchantSnapshot(
        merchant_id=merchant_id,
        date=as_of.date().isoformat(),
        risk_score=round(risk_score, 4),
        cb_ratio=round(cb_ratio, 5),
        volume=round(current["value"], 2),
        volume_delta=round(volume_delta, 3),
        fraud_rate=round(fraud_rate, 4),
        recommended_reserve_pct=reserve,
        recommended_txn_limit=limit,
        memo=memo,
    )


def _recommend_reserve(risk_score: float, cb_ratio: float) -> float:
    """Rolling reserve sized to cover expected disputes, not to punish."""
    if cb_ratio >= CB_MONITORING_THRESHOLD * 2 or risk_score >= 0.8:
        return 0.15
    if cb_ratio >= CB_MONITORING_THRESHOLD or risk_score >= 0.6:
        return 0.10
    if risk_score >= 0.4:
        return 0.05
    return 0.0


def _recommend_limit(current_limit: float, risk_score: float, stats: dict[str, float]) -> float:
    average_ticket = stats["value"] / stats["n"] if stats["n"] else current_limit
    if risk_score >= 0.8:
        return round(max(average_ticket * 2, 5000.0), 2)
    if risk_score >= 0.6:
        return round(max(average_ticket * 5, 25000.0), 2)
    return round(current_limit, 2)


def write_memo(merchant: sqlite3.Row, risk_score: float, cb_ratio: float,
               volume_delta: float, fraud_rate: float, refund_rate: float,
               age_days: int, reserve: float, limit: float) -> str:
    """Plain-language underwriting memo.

    Deterministic by design. The agent reads this memo as evidence when it
    reviews a transaction on this merchant, so it has to say the same thing
    every time it is generated from the same numbers.
    """
    lines = [
        merchant["name"] + " (" + merchant["category"] + "), onboarded "
        + str(age_days) + " days ago.",
        "Rolling 30-day chargeback ratio is %.2f%% against a %.2f%% monitoring threshold."
        % (cb_ratio * 100, CB_MONITORING_THRESHOLD * 100),
    ]
    if volume_delta >= 2.0:
        lines.append("Volume is %.1fx the previous 30-day period, which is the pattern a "
                     "bust-out produces and also the pattern a successful campaign produces. "
                     "Treat it as a question, not a conclusion." % volume_delta)
    elif volume_delta <= 0.5:
        lines.append("Volume has fallen to %.0f%% of the previous period." % (volume_delta * 100))

    if fraud_rate > 0.03:
        lines.append("The risk engine declined %.1f%% of attempted payments here, "
                     "well above portfolio norm." % (fraud_rate * 100))
    if refund_rate > 0.08:
        lines.append("Refund rate of %.1f%% is high enough to suggest either "
                     "fulfilment problems or refund abuse." % (refund_rate * 100))

    if risk_score >= 0.8:
        lines.append("Recommendation: hold settlement pending review, apply a %.0f%% rolling "
                     "reserve and cut the per-transaction limit to %s."
                     % (reserve * 100, _inr(limit)))
    elif risk_score >= 0.6:
        lines.append("Recommendation: apply a %.0f%% rolling reserve, reduce the per-transaction "
                     "limit to %s, and re-review in seven days." % (reserve * 100, _inr(limit)))
    elif risk_score >= 0.4:
        lines.append("Recommendation: a %.0f%% rolling reserve is warranted; no limit change."
                     % (reserve * 100))
    else:
        lines.append("Recommendation: no action. Merchant is within normal parameters.")
    return " ".join(lines)


def _inr(value: float) -> str:
    return "INR " + format(int(round(value)), ",")


def run_daily_rollup(conn: sqlite3.Connection, as_of: datetime | None = None,
                     apply_recommendations: bool = False) -> list[MerchantSnapshot]:
    """Score every merchant with recent activity and store the snapshots."""
    if as_of is None:
        row = conn.execute("SELECT MAX(ts) AS m FROM transactions").fetchone()
        as_of = datetime.fromisoformat(row["m"]) if row["m"] else datetime.now()

    snapshots: list[MerchantSnapshot] = []
    for row in conn.execute("SELECT id FROM merchants"):
        snapshot = compute_snapshot(conn, row["id"], as_of)
        if snapshot is None:
            continue
        snapshots.append(snapshot)
        conn.execute(
            "INSERT OR REPLACE INTO merchant_risk_daily"
            " (merchant_id, date, risk_score, cb_ratio, volume, volume_delta, fraud_rate,"
            "  recommended_reserve_pct, recommended_txn_limit, memo)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (snapshot.merchant_id, snapshot.date, snapshot.risk_score, snapshot.cb_ratio,
             snapshot.volume, snapshot.volume_delta, snapshot.fraud_rate,
             snapshot.recommended_reserve_pct, snapshot.recommended_txn_limit, snapshot.memo))
        if apply_recommendations:
            # Reserves and limits move automatically; freezing a merchant does not.
            conn.execute(
                "UPDATE merchants SET reserve_pct = ?, txn_limit = ?, risk_tier = ? WHERE id = ?",
                (snapshot.recommended_reserve_pct, snapshot.recommended_txn_limit,
                 "high" if snapshot.risk_score >= 0.6 else
                 "elevated" if snapshot.risk_score >= 0.4 else "standard",
                 snapshot.merchant_id))
    conn.commit()
    return snapshots
