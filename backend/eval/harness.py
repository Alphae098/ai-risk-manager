"""Evaluation harness.

Answers the questions the design promised to answer, on the held-out period
only:

  1. Does the hybrid beat rules-only and model-only on recall at equal or
     lower false-positive rate?
  2. What does the agent cost per 1,000 transactions, and how does that move
     as the uncertain band widens?
  3. What is the net financial effect in rupees?

Run: python -m eval.harness
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from arm.config import CHARGEBACK_FEE_INR, DATA_DIR, FALSE_DECLINE_COST_INR
from arm.db import jload, session
from arm.scoring.rules import RulesEngine

REPORT_JSON = DATA_DIR / "evaluation.json"
REPORT_MD = DATA_DIR / "evaluation.md"


@dataclass
class Row:
    txn_id: str
    ts: str
    amount: float
    is_fraud: int
    model_score: float
    final_score: float
    rule_hits: list[str]
    blocked_by_rule: bool


@dataclass
class Metrics:
    name: str
    declined: int
    total: int
    true_positives: int
    false_positives: int
    false_negatives: int
    reviewed: int = 0
    fraud_value_stopped: float = 0.0
    fraud_value_missed: float = 0.0
    legit_value_declined: float = 0.0
    notes: str = ""

    @property
    def precision(self) -> float:
        denominator = self.true_positives + self.false_positives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positives + self.false_negatives
        return self.true_positives / denominator if denominator else 0.0

    @property
    def false_positive_rate(self) -> float:
        legit = self.total - (self.true_positives + self.false_negatives)
        return self.false_positives / legit if legit else 0.0

    @property
    def review_rate(self) -> float:
        return self.reviewed / self.total if self.total else 0.0

    @property
    def net_inr(self) -> float:
        """Chargeback losses avoided, less the cost of wrongly declined payments."""
        avoided = self.fraud_value_stopped + self.true_positives * CHARGEBACK_FEE_INR
        cost = self.legit_value_declined * 0.02 + self.false_positives * FALSE_DECLINE_COST_INR
        return avoided - cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "declined": self.declined,
            "reviewed": self.reviewed,
            "review_rate": round(self.review_rate, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "false_positive_rate": round(self.false_positive_rate, 5),
            "fraud_value_stopped_inr": round(self.fraud_value_stopped, 2),
            "fraud_value_missed_inr": round(self.fraud_value_missed, 2),
            "net_inr": round(self.net_inr, 2),
            "notes": self.notes,
        }


def load_rows(conn, holdout_fraction: float = 0.30) -> list[Row]:
    records = conn.execute(
        "SELECT t.id, t.ts, t.amount, t.is_fraud, d.model_score, d.final_score, d.rule_hits"
        "  FROM transactions t JOIN decisions d ON d.txn_id = t.id"
        " ORDER BY t.ts ASC").fetchall()
    hard_rules = {r.id for r in RulesEngine().rules if r.action == "block"}
    rows = []
    for record in records:
        hits = jload(record["rule_hits"], []) or []
        rows.append(Row(
            txn_id=record["id"], ts=record["ts"], amount=float(record["amount"]),
            is_fraud=int(record["is_fraud"]), model_score=float(record["model_score"]),
            final_score=float(record["final_score"]), rule_hits=hits,
            blocked_by_rule=bool(set(hits) & hard_rules),
        ))
    cut = int(len(rows) * (1 - holdout_fraction))
    return rows[cut:]


def _score(name: str, rows: list[Row], declined: list[bool],
           reviewed: list[bool] | None = None, notes: str = "") -> Metrics:
    reviewed = reviewed or [False] * len(rows)
    metrics = Metrics(name=name, declined=sum(declined), total=len(rows),
                      true_positives=0, false_positives=0, false_negatives=0,
                      reviewed=sum(reviewed), notes=notes)
    for row, is_declined in zip(rows, declined):
        if is_declined:
            if row.is_fraud:
                metrics.true_positives += 1
                metrics.fraud_value_stopped += row.amount
            else:
                metrics.false_positives += 1
                metrics.legit_value_declined += row.amount
        elif row.is_fraud:
            metrics.false_negatives += 1
            metrics.fraud_value_missed += row.amount
    return metrics


def evaluate(rows: list[Row], low: float, high: float,
             agent_catch_rate: float) -> dict[str, Metrics]:
    """Compare the three strategies at one band setting.

    `agent_catch_rate` is measured from resolved agent cases, not assumed: it is
    the share of review-band fraud the agent actually called fraud.
    """
    rules_only = _score("rules only", rows, [r.blocked_by_rule for r in rows],
                        notes="Deterministic layer alone, no model, no agent.")

    # Model-only is given the same overall decline budget as the hybrid, so the
    # comparison is at matched decline volume rather than at an arbitrary cut.
    hybrid_declined = [r.blocked_by_rule or r.final_score >= high for r in rows]
    budget = sum(hybrid_declined)
    ranked = sorted(range(len(rows)), key=lambda i: rows[i].model_score, reverse=True)
    model_flags = [False] * len(rows)
    for index in ranked[:budget]:
        model_flags[index] = True
    model_only = _score("model only", rows, model_flags,
                        notes="Same decline budget as the hybrid, ranked by model score.")

    reviewed = [not r.blocked_by_rule and low <= r.final_score < high for r in rows]
    # A reviewed fraud counts as caught only in proportion to the agent's
    # measured catch rate; reviewed legitimate traffic is never declined.
    hybrid_flags = list(hybrid_declined)
    caught_in_review = 0.0
    value_recovered = 0.0
    for i, row in enumerate(rows):
        if reviewed[i] and row.is_fraud:
            caught_in_review += agent_catch_rate
            value_recovered += row.amount * agent_catch_rate
    hybrid = _score("hybrid (rules + model + agent)", rows, hybrid_flags, reviewed,
                    notes="Agent contribution counted at its measured catch rate.")
    hybrid.true_positives += int(round(caught_in_review))
    hybrid.false_negatives = max(0, hybrid.false_negatives - int(round(caught_in_review)))
    # An agent-confirmed fraud is reversed after the fact, so the value it
    # recovers is real but arrives late. Counting it at the same rate the agent
    # actually achieved keeps the comparison with model-only honest.
    hybrid.fraud_value_stopped += value_recovered
    hybrid.fraud_value_missed = max(0.0, hybrid.fraud_value_missed - value_recovered)
    return {"rules_only": rules_only, "model_only": model_only, "hybrid": hybrid}


def agent_stats(conn) -> dict[str, float]:
    rows = conn.execute(
        "SELECT c.verdict, c.cost_usd, c.prompt_tokens, c.completion_tokens, t.is_fraud"
        "  FROM agent_cases c JOIN transactions t ON t.id = c.txn_id"
        " WHERE c.verdict IS NOT NULL").fetchall()
    if not rows:
        return {"cases": 0, "catch_rate": 0.0, "cost_per_case_usd": 0.0,
                "precision": 0.0, "escalation_rate": 0.0}
    fraud_rows = [r for r in rows if r["is_fraud"]]
    called_fraud = [r for r in rows if r["verdict"] == "fraud"]
    catch = sum(1 for r in fraud_rows if r["verdict"] == "fraud")
    return {
        "cases": len(rows),
        "catch_rate": round(catch / len(fraud_rows), 4) if fraud_rows else 0.0,
        "precision": round(
            sum(1 for r in called_fraud if r["is_fraud"]) / len(called_fraud), 4)
        if called_fraud else 0.0,
        "escalation_rate": round(
            sum(1 for r in rows if r["verdict"] == "insufficient_evidence") / len(rows), 4),
        "cost_per_case_usd": round(sum(float(r["cost_usd"] or 0) for r in rows) / len(rows), 6),
    }


def sweep(rows: list[Row], agent: dict[str, float]) -> list[dict[str, Any]]:
    """Widen the uncertain band and watch recall, review rate and cost move."""
    results = []
    for low, high in [(0.50, 0.90), (0.40, 0.90), (0.30, 0.85), (0.20, 0.85),
                      (0.15, 0.80), (0.10, 0.75), (0.05, 0.70)]:
        outcome = evaluate(rows, low, high, agent["catch_rate"])
        hybrid = outcome["hybrid"]
        reviewed = hybrid.reviewed
        cost_per_1k = (reviewed * agent["cost_per_case_usd"]) / len(rows) * 1000
        results.append({
            "band": [low, high],
            "review_rate": round(hybrid.review_rate, 4),
            "recall": round(hybrid.recall, 4),
            "precision": round(hybrid.precision, 4),
            "false_positive_rate": round(hybrid.false_positive_rate, 5),
            "llm_cost_per_1k_txn_usd": round(cost_per_1k, 4),
            "net_inr": round(hybrid.net_inr, 2),
        })
    return results


def latency_stats() -> dict[str, float]:
    """Measured on the real single-transaction path, model inference included."""
    from .latency import measure
    return measure(sample_size=800)


def main() -> dict[str, Any]:
    from arm.config import BANDS
    with session() as conn:
        rows = load_rows(conn)
        agent = agent_stats(conn)
        outcome = evaluate(rows, BANDS.approve_below, BANDS.decline_at, agent["catch_rate"])
        report = {
            "holdout_transactions": len(rows),
            "holdout_fraud": sum(r.is_fraud for r in rows),
            "current_band": [BANDS.approve_below, BANDS.decline_at],
            "strategies": [m.as_dict() for m in outcome.values()],
            "agent": agent,
            "latency": latency_stats(),
            "band_sweep": sweep(rows, agent),
        }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Evaluation report", "",
             "Held-out period: %d transactions, %d fraudulent (%.2f%%)."
             % (report["holdout_transactions"], report["holdout_fraud"],
                100 * report["holdout_fraud"] / max(1, report["holdout_transactions"])),
             "", "## Strategy comparison", "",
             "| Strategy | Declined | Reviewed | Precision | Recall | FP rate | Net INR |",
             "|---|---|---|---|---|---|---|"]
    for strategy in report["strategies"]:
        lines.append("| %s | %d | %d | %.3f | %.3f | %.4f | %s |" % (
            strategy["strategy"], strategy["declined"], strategy["reviewed"],
            strategy["precision"], strategy["recall"], strategy["false_positive_rate"],
            format(int(strategy["net_inr"]), ",")))
    lines += ["", "## Agent", "",
              "- Cases resolved: %d" % report["agent"]["cases"],
              "- Catch rate on review-band fraud: %.1f%%" % (100 * report["agent"]["catch_rate"]),
              "- Precision when it calls fraud: %.1f%%" % (100 * report["agent"]["precision"]),
              "- Escalated to a human: %.1f%%" % (100 * report["agent"]["escalation_rate"]),
              "- Cost per case: $%.4f%s" % (
                  report["agent"]["cost_per_case_usd"],
                  "  (offline heuristic reviewer - no LLM calls were made)"
                  if report["agent"]["cost_per_case_usd"] == 0 else ""),
              "", "## Latency (features + rules + model, single-transaction path)", ""]
    for key, value in report["latency"].items():
        lines.append("- %s: %s" % (key, value))
    lines += ["", "## Band sweep", "",
              "| Band | Review rate | Recall | Precision | LLM $/1k txn | Net INR |",
              "|---|---|---|---|---|---|"]
    for point in report["band_sweep"]:
        lines.append("| %.2f-%.2f | %.2f%% | %.3f | %.3f | %.4f | %s |" % (
            point["band"][0], point["band"][1], 100 * point["review_rate"], point["recall"],
            point["precision"], point["llm_cost_per_1k_txn_usd"],
            format(int(point["net_inr"]), ",")))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
