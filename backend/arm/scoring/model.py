"""Supervised fraud model.

Gradient boosting over the engineered features, wrapped in isotonic calibration
so the output is a usable probability rather than a ranking score. Calibration
is not cosmetic here: the band router thresholds directly on this number, so a
score of 0.30 has to mean roughly a 30% chance of fraud.

Training and evaluation use a time-based split. Random splits leak future
information through the velocity features and flatter the model badly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import joblib
import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from ..config import MODEL_DIR
from ..features.engine import FEATURE_NAMES

MODEL_PATH = MODEL_DIR / "fraud_model.joblib"


@dataclass
class TrainReport:
    n_train: int
    n_test: int
    fraud_rate_train: float
    fraud_rate_test: float
    roc_auc: float
    average_precision: float
    feature_names: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_train": self.n_train,
            "n_test": self.n_test,
            "fraud_rate_train": round(self.fraud_rate_train, 5),
            "fraud_rate_test": round(self.fraud_rate_test, 5),
            "roc_auc": round(self.roc_auc, 4),
            "average_precision": round(self.average_precision, 4),
        }


class FraudModel:
    def __init__(self, estimator: Any = None, feature_names: Sequence[str] | None = None):
        self.estimator = estimator
        self.feature_names = list(feature_names or FEATURE_NAMES)

    # ------------------------------------------------------------------ train

    @classmethod
    def train(cls, X: np.ndarray, y: np.ndarray, split_at: int) -> tuple["FraudModel", TrainReport]:
        X_train, y_train = X[:split_at], y[:split_at]
        X_test, y_test = X[split_at:], y[split_at:]

        base = HistGradientBoostingClassifier(
            max_iter=300,
            learning_rate=0.08,
            max_leaf_nodes=31,
            min_samples_leaf=40,
            l2_regularization=1.0,
            early_stopping=True,
            validation_fraction=0.15,
            random_state=7,
        )
        # Isotonic calibration on a held-out slice of the training period.
        calibrated = CalibratedClassifierCV(base, method="isotonic", cv=3)
        calibrated.fit(X_train, y_train)

        scores = calibrated.predict_proba(X_test)[:, 1]
        report = TrainReport(
            n_train=len(y_train),
            n_test=len(y_test),
            fraud_rate_train=float(y_train.mean()),
            fraud_rate_test=float(y_test.mean()),
            roc_auc=float(roc_auc_score(y_test, scores)),
            average_precision=float(average_precision_score(y_test, scores)),
            feature_names=list(FEATURE_NAMES),
        )
        return cls(calibrated), report

    # ---------------------------------------------------------------- predict

    def score(self, vector: Sequence[float]) -> float:
        return float(self.estimator.predict_proba(np.asarray([vector], dtype=float))[0, 1])

    def score_batch(self, matrix: np.ndarray) -> np.ndarray:
        return self.estimator.predict_proba(matrix)[:, 1]

    def top_contributors(self, features: dict[str, float], k: int = 5) -> list[dict[str, Any]]:
        """Cheap, honest attribution: which features are most abnormal here.

        This is not SHAP. It reports the features whose values deviate furthest
        from the training median, which is what an analyst actually wants to see
        and what the agent cites. The label says so, so nobody mistakes it for
        a true attribution.
        """
        medians = getattr(self, "medians", None)
        if medians is None:
            return []
        scored = []
        for name in self.feature_names:
            med, iqr = medians.get(name, (0.0, 1.0))
            value = features.get(name, 0.0)
            deviation = abs(value - med) / (iqr if iqr > 1e-9 else 1.0)
            scored.append({"feature": name, "value": round(value, 4),
                           "median": round(med, 4), "deviation": round(deviation, 2)})
        scored.sort(key=lambda d: d["deviation"], reverse=True)
        return scored[:k]

    # ------------------------------------------------------------------- i/o

    def save(self, path: Path | str = MODEL_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"estimator": self.estimator,
                     "feature_names": self.feature_names,
                     "medians": getattr(self, "medians", {})}, path)
        return path

    @classmethod
    def load(cls, path: Path | str = MODEL_PATH) -> "FraudModel":
        blob = joblib.load(Path(path))
        model = cls(blob["estimator"], blob["feature_names"])
        model.medians = blob.get("medians", {})
        return model


def compute_medians(X: np.ndarray, feature_names: Sequence[str]) -> dict[str, tuple[float, float]]:
    """Median and interquartile range per feature, used for the deviation view."""
    out: dict[str, tuple[float, float]] = {}
    q1 = np.percentile(X, 25, axis=0)
    q3 = np.percentile(X, 75, axis=0)
    med = np.median(X, axis=0)
    for i, name in enumerate(feature_names):
        out[name] = (float(med[i]), float(q3[i] - q1[i]))
    return out
