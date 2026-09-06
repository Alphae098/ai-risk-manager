"""Deterministic rules layer.

Rules live in YAML so risk operators can read and change them without touching
Python. Each rule is compiled once into a code object and evaluated against a
flat context of features plus raw transaction fields.

Every rule carries hit statistics. A mature rules engine rots because nobody
knows which rules still fire and which ones only produce false positives; the
`rule_stats` table is what makes that visible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..config import RULES_PATH

# Only these names are reachable from a rule condition.
_SAFE_BUILTINS: dict[str, Any] = {"abs": abs, "min": min, "max": max, "len": len}


@dataclass
class Rule:
    id: str
    description: str
    when: str
    action: str
    boost: float = 0.0
    owner: str = "unassigned"
    _compiled: Any = field(default=None, repr=False, compare=False)

    def compile(self) -> None:
        self._compiled = compile(self.when, "<rule:" + self.id + ">", "eval")

    def matches(self, context: dict[str, Any]) -> bool:
        try:
            return bool(eval(self._compiled, {"__builtins__": _SAFE_BUILTINS}, context))
        except Exception:
            # A rule referencing a field that is absent must not break scoring.
            return False


@dataclass
class RuleHit:
    rule_id: str
    description: str
    action: str
    boost: float


@dataclass
class RulesVerdict:
    hits: list[RuleHit]
    blocked: bool
    boost: float

    @property
    def hit_ids(self) -> list[str]:
        return [h.rule_id for h in self.hits]


class RulesEngine:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or RULES_PATH)
        self.rules: list[Rule] = []
        self.loaded_at: datetime | None = None
        self.load()

    def load(self) -> None:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        rules = []
        for item in raw.get("rules", []):
            rule = Rule(
                id=item["id"],
                description=item["description"],
                when=item["when"],
                action=item["action"],
                boost=float(item.get("boost", 0.0)),
                owner=item.get("owner", "unassigned"),
            )
            rule.compile()
            rules.append(rule)
        self.rules = rules
        self.loaded_at = datetime.now()

    def evaluate(self, context: dict[str, Any]) -> RulesVerdict:
        hits: list[RuleHit] = []
        blocked = False
        boost = 0.0
        for rule in self.rules:
            if not rule.matches(context):
                continue
            hits.append(RuleHit(rule.id, rule.description, rule.action, rule.boost))
            if rule.action == "block":
                blocked = True
            elif rule.action == "score_boost":
                boost += rule.boost
        return RulesVerdict(hits=hits, blocked=blocked, boost=min(boost, 0.6))


def build_context(features: dict[str, float], txn: dict[str, Any],
                  merchant: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flat evaluation context: features first, then raw fields rules may need."""
    merchant = merchant or {}
    context: dict[str, Any] = dict(features)
    context.update({
        "method": txn.get("method"),
        "ip_country": txn.get("ip_country"),
        "bin_country": txn.get("bin_country"),
        "merchant_category": merchant.get("category"),
        "merchant_frozen": int(merchant.get("frozen", 0)),
        "merchant_txn_limit": float(merchant.get("txn_limit", 1e12)),
        "merchant_risk_tier": merchant.get("risk_tier", "standard"),
    })
    return context
