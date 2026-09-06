"""Latency benchmark for the authorization path.

The backfill scores in batches, so the latency it records covers features and
rules only. That number would flatter the system. This benchmark runs a sample
of transactions through the real single-transaction pipeline, model inference
included, which is what production would actually pay.

Run: python -m eval.latency
"""
from __future__ import annotations

import json
import time

from arm.db import session
from arm.features.engine import FeatureEngine
from arm.scoring.model import FraudModel
from arm.scoring.router import DecisionPipeline
from arm.scoring.rules import RulesEngine

WARMUP = 50


def measure(sample_size: int = 1000) -> dict[str, float]:
    with session() as conn:
        merchants = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM merchants")}
        customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions ORDER BY ts ASC LIMIT ?", (sample_size + WARMUP,))]

    pipeline = DecisionPipeline(model=FraudModel.load(), rules=RulesEngine(),
                               features=FeatureEngine(merchants, customers))
    timings: list[float] = []
    for index, txn in enumerate(rows):
        started = time.perf_counter()
        pipeline.decide(txn)
        elapsed = (time.perf_counter() - started) * 1000
        if index >= WARMUP:
            timings.append(elapsed)

    timings.sort()

    def percentile(p: float) -> float:
        return round(timings[min(len(timings) - 1, int(len(timings) * p))], 3)

    return {
        "samples": len(timings),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": round(timings[-1], 3),
        "mean_ms": round(sum(timings) / len(timings), 3),
    }


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2))
