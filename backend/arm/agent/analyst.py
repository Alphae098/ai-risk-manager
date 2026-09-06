"""The risk-analyst agent.

Runs only on transactions the router placed in the uncertain band. The loop is
bounded: at most `max_tool_calls` investigations, then a structured verdict that
must validate against the schema below or the case is marked for a human.

`insufficient_evidence` is a first-class verdict. An agent that always produces
an answer is an agent that invents one, and in risk review a confident wrong
answer is more expensive than an escalation.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..config import LLM
from ..db import jdump
from .llm import LLMError, LLMResponse, OpenRouterClient, parse_json_object
from .tools import TOOL_SCHEMAS, ToolBox

VERDICTS = {"fraud", "legit", "insufficient_evidence"}
ACTIONS = {"reverse", "step_up", "monitor", "allow", "freeze_merchant"}

SYSTEM_PROMPT = """You are a payment risk analyst at a payment aggregator in India.

A transaction has been scored in the uncertain band: the model is neither
confident it is fraud nor confident it is legitimate. The payment has already
been provisionally approved, so you are not blocking anything. Your job is to
investigate and recommend.

Use the tools to gather evidence before you decide. Start with
get_transaction_context. Check the customer's own history before calling their
behaviour unusual, and check the merchant profile before treating merchant
volume as normal. Look for linked entities when a device, IP or card appears
shared.

Ground every claim in something a tool returned. Do not speculate about
information you were not given, and do not assume fraud from a single weak
signal such as a large amount alone.

Legitimate traffic in this portfolio includes sale-day volume spikes,
travelling customers transacting from a new city on their usual device, and
first-time high-value purchases. Do not flag those as fraud on their own.

When you have enough evidence, reply with ONLY a JSON object:

{
  "verdict": "fraud" | "legit" | "insufficient_evidence",
  "confidence": 0.0 to 1.0,
  "rationale": "two or three sentences citing the specific evidence",
  "evidence": [{"source": "tool_name", "fact": "what it showed", "weight": "high|medium|low"}],
  "recommended_action": "reverse" | "step_up" | "monitor" | "allow" | "freeze_merchant",
  "suggested_rule": null or {"description": "...", "when": "a boolean expression over feature names"}
}

Set suggested_rule only when you have identified a repeatable pattern that the
existing rules missed. It will be reviewed by a human before it goes live."""


@dataclass
class AgentCase:
    id: str
    txn_id: str
    created_at: str
    status: str = "open"
    verdict: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    recommended_action: str | None = None
    suggested_rule: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_source: str = "mock"

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["evidence"] = jdump(self.evidence)
        row["suggested_rule"] = jdump(self.suggested_rule) if self.suggested_rule else None
        row["tool_calls"] = jdump(self.tool_calls)
        return row


class RiskAnalystAgent:
    def __init__(self, conn: sqlite3.Connection, client: OpenRouterClient | None = None,
                 config=LLM):
        self.conn = conn
        self.tools = ToolBox(conn)
        self.client = client or OpenRouterClient(config)
        self.config = config

    # ------------------------------------------------------------------ entry

    def review(self, txn_id: str) -> AgentCase:
        case = AgentCase(id="case_" + uuid.uuid4().hex[:12], txn_id=txn_id,
                         created_at=datetime.now().isoformat())
        try:
            if self.client.is_mock:
                self._review_with_heuristic(case)
            else:
                self._review_with_llm(case)
        except LLMError as exc:
            # A provider outage must not lose the case: fall back and say so.
            case.rationale = "LLM unavailable (" + str(exc) + "); heuristic review applied."
            self._review_with_heuristic(case, preserve_rationale=True)
        self._persist(case)
        return case

    # -------------------------------------------------------------- llm path

    def _review_with_llm(self, case: AgentCase) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Review transaction " + case.txn_id + "."},
        ]
        for _ in range(self.config.max_tool_calls):
            response: LLMResponse = self.client.chat(messages, TOOL_SCHEMAS)
            case.prompt_tokens += response.prompt_tokens
            case.completion_tokens += response.completion_tokens
            case.cost_usd += response.cost_usd
            case.llm_source = response.source

            if not response.tool_calls:
                parsed = parse_json_object(response.content)
                if parsed:
                    self._apply_verdict(case, parsed)
                    return
                # One corrective nudge, then give up and escalate to a human.
                messages.append({"role": "assistant", "content": response.content or ""})
                messages.append({"role": "user",
                                 "content": "Reply with only the JSON object described above."})
                continue

            messages.append({"role": "assistant", "content": response.content,
                             "tool_calls": response.tool_calls})
            for call in response.tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self.tools.call(name, args)
                case.tool_calls.append({"name": name, "arguments": args})
                messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                 "content": json.dumps(result, default=str)[:6000]})

        if case.verdict is None:
            case.verdict = "insufficient_evidence"
            case.confidence = 0.3
            case.rationale = ("Investigation budget exhausted without a conclusion. "
                              "Escalated for human review.")
            case.recommended_action = "monitor"
            case.status = "escalated"

    def _apply_verdict(self, case: AgentCase, parsed: dict[str, Any]) -> None:
        verdict = str(parsed.get("verdict", "")).lower()
        case.verdict = verdict if verdict in VERDICTS else "insufficient_evidence"
        try:
            case.confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.5))))
        except (TypeError, ValueError):
            case.confidence = 0.5
        case.rationale = str(parsed.get("rationale") or "")[:2000]
        evidence = parsed.get("evidence")
        case.evidence = evidence if isinstance(evidence, list) else []
        action = str(parsed.get("recommended_action", "")).lower()
        case.recommended_action = action if action in ACTIONS else "monitor"
        rule = parsed.get("suggested_rule")
        case.suggested_rule = rule if isinstance(rule, dict) else None
        case.status = "resolved" if case.verdict != "insufficient_evidence" else "escalated"

    # --------------------------------------------------------- heuristic path

    def _review_with_heuristic(self, case: AgentCase, preserve_rationale: bool = False) -> None:
        """Deterministic offline reviewer.

        Runs the same tools and weighs the same evidence the prompt describes.
        It exists so the demo, the tests and the evaluation harness all work
        without a network call, and so LLM output can be compared against a
        fixed baseline rather than against nothing.
        """
        context = self.tools.get_transaction_context(case.txn_id)
        case.tool_calls.append({"name": "get_transaction_context", "arguments": {"txn_id": case.txn_id}})
        if "error" in context:
            case.verdict = "insufficient_evidence"
            case.confidence = 0.2
            case.rationale = "Transaction record not found."
            case.recommended_action = "monitor"
            case.status = "escalated"
            return

        features = context.get("key_features", {})
        history = self.tools.get_customer_history(context["customer_id"])
        case.tool_calls.append({"name": "get_customer_history",
                                "arguments": {"customer_id": context["customer_id"]}})
        profile = self.tools.get_merchant_profile(context["merchant_id"])
        case.tool_calls.append({"name": "get_merchant_profile",
                                "arguments": {"merchant_id": context["merchant_id"]}})
        linked = self.tools.find_linked_entities(case.txn_id)
        case.tool_calls.append({"name": "find_linked_entities", "arguments": {"txn_id": case.txn_id}})

        evidence: list[dict[str, Any]] = []
        score = 0.0

        shared_device = len(linked.get("customers_on_same_device", []))
        if shared_device >= 4:
            score += 0.40
            evidence.append({"source": "find_linked_entities", "weight": "high",
                             "fact": str(shared_device) + " distinct customers used this device in 24h"})

        shared_subnet = len(linked.get("customers_on_same_subnet", []))
        if shared_subnet >= 8:
            score += 0.15
            evidence.append({"source": "find_linked_entities", "weight": "medium",
                             "fact": str(shared_subnet) + " customers transacted from this IP subnet in 24h"})

        if features.get("device_txn_1m", 0) >= 4:
            score += 0.25
            evidence.append({"source": "get_transaction_context", "weight": "high",
                             "fact": "Device attempted %.0f payments in one minute"
                                     % features["device_txn_1m"]})

        ratio = features.get("amount_over_customer_typical", 1.0)
        if ratio >= 5:
            # Only unusual if this customer has an established baseline.
            if history.get("chargebacks_in_window", 0) or len(history.get("recent_transactions", [])) >= 5:
                score += 0.20
                evidence.append({"source": "get_customer_history", "weight": "medium",
                                 "fact": "Amount is %.1fx the customer's typical ticket" % ratio})
            else:
                evidence.append({"source": "get_customer_history", "weight": "low",
                                 "fact": "Large ticket but the customer has little history to compare against"})

        if features.get("geo_bin_ip_mismatch") and features.get("geo_ip_offshore"):
            score += 0.20
            evidence.append({"source": "get_transaction_context", "weight": "high",
                             "fact": "Card issued in " + str(context.get("bin_country"))
                                     + " used from " + str(context.get("ip_country"))})

        if features.get("device_is_new") and features.get("customer_is_new_city"):
            if history.get("distinct_cities_in_window", 0) <= 1:
                score += 0.15
                evidence.append({"source": "get_customer_history", "weight": "medium",
                                 "fact": "New device and a city the customer has never used"})
            else:
                evidence.append({"source": "get_customer_history", "weight": "low",
                                 "fact": "New city, but this customer transacts from several cities"})

        merchant = profile.get("merchant", {})
        snapshots = profile.get("risk_snapshots", [])
        if snapshots:
            latest = snapshots[0]
            # A merchant already past the network monitoring threshold is the
            # strongest single signal available at review time, and it is the
            # one signal the transaction-level model structurally cannot see.
            if latest.get("risk_score", 0) >= 0.6:
                score += 0.45
                evidence.append({"source": "get_merchant_profile", "weight": "high",
                                 "fact": "Merchant risk score %.2f, chargeback ratio %.2f%%, trend %s"
                                         % (latest["risk_score"], latest["cb_ratio"] * 100,
                                            profile.get("trend", "unknown"))})
            elif latest.get("volume_delta", 0) >= 2.0:
                score += 0.10
                evidence.append({"source": "get_merchant_profile", "weight": "medium",
                                 "fact": "Merchant volume is %.1fx its recent baseline"
                                         % latest["volume_delta"]})
        elif merchant.get("risk_tier") == "elevated":
            evidence.append({"source": "get_merchant_profile", "weight": "low",
                             "fact": "Merchant sits in an elevated-risk category"})

        if score >= 0.55:
            verdict, action = "fraud", ("freeze_merchant" if score >= 0.9 else "reverse")
        elif score <= 0.20:
            verdict, action = "legit", "allow"
        else:
            verdict, action = "insufficient_evidence", "step_up"

        case.verdict = verdict
        case.confidence = round(min(0.95, 0.5 + abs(score - 0.375)), 2)
        case.evidence = evidence
        case.recommended_action = action
        case.status = "resolved" if verdict != "insufficient_evidence" else "escalated"
        case.llm_source = "heuristic"
        summary = _summarize(verdict, evidence)
        case.rationale = (case.rationale + " " + summary) if preserve_rationale and case.rationale else summary

        if verdict == "fraud" and shared_device >= 4 and not context.get("rule_hits"):
            case.suggested_rule = {
                "description": "Device shared by many customers within a day",
                "when": "device_distinct_customers_24h >= " + str(shared_device),
            }

    # ------------------------------------------------------------- persistence

    def _persist(self, case: AgentCase) -> None:
        row = case.to_row()
        cols = list(row.keys())
        self.conn.execute(
            "INSERT OR REPLACE INTO agent_cases (" + ",".join(cols) + ") VALUES ("
            + ",".join("?" for _ in cols) + ")",
            tuple(row[c] for c in cols))
        if case.suggested_rule:
            self.conn.execute(
                "INSERT INTO proposed_rules (created_at, source_case_id, description, condition, status)"
                " VALUES (?,?,?,?, 'pending')",
                (case.created_at, case.id, case.suggested_rule.get("description", ""),
                 case.suggested_rule.get("when", "")))
        self.conn.commit()


def _summarize(verdict: str, evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "No corroborating signal found in the available evidence."
    strong = [e["fact"] for e in evidence if e.get("weight") == "high"]
    facts = strong or [e["fact"] for e in evidence]
    lead = {
        "fraud": "Assessed as fraud. ",
        "legit": "Assessed as legitimate. ",
        "insufficient_evidence": "Evidence is mixed. ",
    }[verdict]
    return lead + "Basis: " + "; ".join(facts[:3]) + "."
