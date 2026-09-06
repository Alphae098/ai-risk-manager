"""Decision pipeline: features, rules, model, band routing.

This module never imports the agent. It produces a score and a band; the band
tells a caller whether an agent case is warranted. Keeping the dependency in
that direction is what makes the authorization path deterministic and testable
without a language model in the loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import BANDS
from ..features.engine import FeatureEngine, vectorize
from .model import FraudModel
from .rules import RulesEngine, RulesVerdict, build_context


@dataclass
class Decision:
    txn_id: str
    ts: str
    model_score: float
    rule_boost: float
    final_score: float
    band: str
    action: str
    rule_hits: list[str]
    rule_details: list[dict[str, Any]]
    features: dict[str, float]
    latency_ms: float
    needs_agent: bool = False
    top_contributors: list[dict[str, Any]] = field(default_factory=list)


class DecisionPipeline:
    def __init__(self, model: FraudModel | None = None,
                 rules: RulesEngine | None = None,
                 features: FeatureEngine | None = None,
                 bands=BANDS):
        self.model = model
        self.rules = rules or RulesEngine()
        self.features = features or FeatureEngine()
        self.bands = bands

    def decide(self, txn: dict[str, Any]) -> Decision:
        started = time.perf_counter()
        merchant = self.features.merchants.get(txn["merchant_id"], {})

        feats = self.features.compute(txn)
        context = build_context(feats, txn, merchant)
        verdict: RulesVerdict = self.rules.evaluate(context)

        if verdict.blocked:
            # A hard rule short-circuits the whole pipeline: no model inference,
            # no agent, no cost. This is the cheap path and it stays cheap.
            latency = (time.perf_counter() - started) * 1000
            return Decision(
                txn_id=txn["id"], ts=str(txn["ts"]),
                model_score=0.0, rule_boost=verdict.boost, final_score=1.0,
                band="decline", action="decline",
                rule_hits=verdict.hit_ids,
                rule_details=[h.__dict__ for h in verdict.hits],
                features=feats, latency_ms=latency, needs_agent=False,
            )

        model_score = self.model.score(vectorize(feats)) if self.model else 0.0
        final_score = min(1.0, model_score + verdict.boost)
        band = self.bands.band_of(final_score)
        action = {"approve": "approve", "review": "approve_provisional", "decline": "decline"}[band]
        latency = (time.perf_counter() - started) * 1000

        decision = Decision(
            txn_id=txn["id"], ts=str(txn["ts"]),
            model_score=model_score, rule_boost=verdict.boost, final_score=final_score,
            band=band, action=action,
            rule_hits=verdict.hit_ids,
            rule_details=[h.__dict__ for h in verdict.hits],
            features=feats, latency_ms=latency,
            needs_agent=(band == "review"),
        )
        if decision.needs_agent and self.model is not None:
            decision.top_contributors = self.model.top_contributors(feats)
        return decision
