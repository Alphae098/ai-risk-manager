"""Train the fraud model by replaying the historical stream.

Run: python -m arm.scoring.train

The replay is strictly time-ordered, so every feature for a transaction is
computed from transactions that genuinely preceded it. The feature matrix is
cached to disk because the evaluation harness and the backfill both need it.
"""
from __future__ import annotations

import json

import numpy as np

from ..config import DATA_DIR, MODEL_DIR
from ..db import as_dicts, session
from ..features.engine import FEATURE_NAMES, FeatureEngine, vectorize
from .model import FraudModel, compute_medians

FEATURE_CACHE = DATA_DIR / "features.npz"
REPORT_PATH = MODEL_DIR / "train_report.json"


def load_entities(conn):
    merchants = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM merchants")}
    customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
    return merchants, customers


def build_matrix(conn) -> tuple[np.ndarray, np.ndarray, list[str]]:
    merchants, customers = load_entities(conn)
    engine = FeatureEngine(merchants, customers)
    rows, labels, ids = [], [], []
    cursor = conn.execute("SELECT * FROM transactions ORDER BY ts ASC")
    for record in cursor:
        txn = dict(record)
        feats = engine.compute(txn)
        rows.append(vectorize(feats))
        labels.append(int(txn["is_fraud"]))
        ids.append(txn["id"])
    return np.asarray(rows, dtype=float), np.asarray(labels, dtype=int), ids


def main() -> dict:
    with session() as conn:
        X, y, ids = build_matrix(conn)

    # Time-based split: the model is trained on the first 70% of the period and
    # measured on the last 30%, which is how it would actually be deployed.
    split_at = int(len(y) * 0.70)
    model, report = FraudModel.train(X, y, split_at)
    model.medians = compute_medians(X[:split_at], FEATURE_NAMES)
    model.save()

    np.savez_compressed(FEATURE_CACHE, X=X, y=y, ids=np.asarray(ids), split_at=split_at)
    payload = report.as_dict() | {"split_at": split_at, "n_features": len(FEATURE_NAMES)}
    REPORT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    main()
