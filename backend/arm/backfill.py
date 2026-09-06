"""Replay the historical stream through the full pipeline.

Run: python -m arm.backfill

This populates `decisions` for every transaction, builds merchant risk
snapshots, and runs the analyst agent over a sample of review-band cases so the
dashboard and the evaluation harness have something real to read. The agent
sample is capped because agent review is the one expensive step.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

import numpy as np

from .agent.analyst import RiskAnalystAgent
from .config import BANDS
from .db import jdump, session
from .features.engine import FeatureEngine, vectorize
from .merchant.portfolio import run_daily_rollup
from .scoring.model import FraudModel
from .scoring.rules import RulesEngine, build_context


def score_all(conn) -> dict[str, int]:
    """Score the whole history.

    The live pipeline scores one transaction at a time, which costs a few
    milliseconds of model inference each. Over the full history that dominates
    the run, so the backfill splits the work: features and rules stream in
    order (they must, they depend on what came before), then the model scores
    every surviving row in one batched call. The arithmetic is identical to
    DecisionPipeline.decide; only the batching differs.
    """
    merchants = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM merchants")}
    customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
    model = FraudModel.load()
    rules = RulesEngine()
    engine = FeatureEngine(merchants, customers)

    conn.execute("DELETE FROM decisions")
    blocked_rows: list[tuple] = []
    pending: list[dict] = []
    vectors: list[list[float]] = []

    for record in conn.execute("SELECT * FROM transactions ORDER BY ts ASC"):
        txn = dict(record)
        started = time.perf_counter()
        feats = engine.compute(txn)
        merchant = merchants.get(txn["merchant_id"], {})
        verdict = rules.evaluate(build_context(feats, txn, merchant))
        latency = (time.perf_counter() - started) * 1000

        if verdict.blocked:
            blocked_rows.append((txn["id"], txn["ts"], jdump(verdict.hit_ids), verdict.boost,
                                 0.0, 1.0, "decline", "decline", latency, jdump(feats)))
            continue
        pending.append({"txn": txn, "features": feats, "verdict": verdict, "latency": latency})
        vectors.append(vectorize(feats))

    scores = model.score_batch(np.asarray(vectors, dtype=float)) if vectors else []

    bands = {"approve": 0, "review": 0, "decline": len(blocked_rows)}
    rows = list(blocked_rows)
    for item, model_score in zip(pending, scores):
        final = min(1.0, float(model_score) + item["verdict"].boost)
        band = BANDS.band_of(final)
        action = {"approve": "approve", "review": "approve_provisional",
                  "decline": "decline"}[band]
        bands[band] += 1
        rows.append((item["txn"]["id"], item["txn"]["ts"], jdump(item["verdict"].hit_ids),
                     item["verdict"].boost, float(model_score), final, band, action,
                     item["latency"], jdump(item["features"])))

    written = 0
    for i in range(0, len(rows), 2000):
        written += _flush(conn, rows[i:i + 2000])
    conn.commit()
    return {"scored": written, **bands}


def _flush(conn, batch) -> int:
    if not batch:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO decisions"
        " (txn_id, ts, rule_hits, rule_boost, model_score, final_score, band, action,"
        "  latency_ms, features) VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    return len(batch)


def run_agent_sample(conn, limit: int) -> dict[str, int]:
    """Review the highest-scoring slice of the uncertain band."""
    agent = RiskAnalystAgent(conn)
    rows = conn.execute(
        "SELECT txn_id FROM decisions WHERE band = 'review'"
        " ORDER BY final_score DESC LIMIT ?", (limit,)).fetchall()
    verdicts: dict[str, int] = {}
    for row in rows:
        case = agent.review(row["txn_id"])
        verdicts[case.verdict or "none"] = verdicts.get(case.verdict or "none", 0) + 1
    return {"reviewed": len(rows), **verdicts}


def update_rule_stats(conn) -> int:
    """Attribute every rule hit to an outcome, so dead rules become visible."""
    conn.execute("DELETE FROM rule_stats")
    stats: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT d.rule_hits, d.ts, t.is_fraud FROM decisions d"
        "  JOIN transactions t ON t.id = d.txn_id WHERE d.rule_hits != '[]'"
    ):
        for rule_id in json.loads(row["rule_hits"]):
            entry = stats.setdefault(rule_id, {"hits": 0, "tp": 0, "fp": 0, "last": ""})
            entry["hits"] += 1
            entry["tp" if row["is_fraud"] else "fp"] += 1
            entry["last"] = max(entry["last"], row["ts"])
    for rule_id, entry in stats.items():
        conn.execute(
            "INSERT OR REPLACE INTO rule_stats"
            " (rule_id, hits, true_positives, false_positives, last_hit_at) VALUES (?,?,?,?,?)",
            (rule_id, entry["hits"], entry["tp"], entry["fp"], entry["last"]))
    conn.commit()
    return len(stats)


def main() -> dict:
    parser = argparse.ArgumentParser(description="Replay the stream through the pipeline.")
    parser.add_argument("--agent-cases", type=int, default=150,
                        help="How many review-band transactions the agent should investigate.")
    parser.add_argument("--apply-merchant-actions", action="store_true",
                        help="Write recommended reserves and limits back onto merchants.")
    args = parser.parse_args()

    with session() as conn:
        scoring = score_all(conn)
        rules = update_rule_stats(conn)
        snapshots = run_daily_rollup(conn, apply_recommendations=args.apply_merchant_actions)
        agent = run_agent_sample(conn, args.agent_cases)

    summary = {
        "scoring": scoring,
        "rules_with_hits": rules,
        "merchant_snapshots": len(snapshots),
        "agent": agent,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
