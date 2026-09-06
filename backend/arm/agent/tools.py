"""Investigation tools available to the risk-analyst agent.

Each tool answers one question an analyst would ask, returns compact JSON, and
reads only from the decision record and history. Nothing here mutates state and
nothing exposes the ground-truth fraud label: the agent has to reason from the
same evidence a human reviewer would see.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from ..db import jload

MAX_ROWS = 25


class ToolBox:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._tools: dict[str, Callable[..., dict[str, Any]]] = {
            "get_transaction_context": self.get_transaction_context,
            "get_customer_history": self.get_customer_history,
            "get_merchant_profile": self.get_merchant_profile,
            "find_linked_entities": self.find_linked_entities,
            "get_similar_past_cases": self.get_similar_past_cases,
        }

    # --------------------------------------------------------------- dispatch

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": "unknown tool: " + name}
        try:
            return tool(**arguments)
        except TypeError as exc:
            return {"error": "bad arguments for " + name + ": " + str(exc)}
        except Exception as exc:  # a failing tool must not kill the case
            return {"error": name + " failed: " + str(exc)}

    # ------------------------------------------------------------------ tools

    def get_transaction_context(self, txn_id: str) -> dict[str, Any]:
        """The transaction itself, its decision record, and the rules that fired."""
        row = self.conn.execute(
            "SELECT t.id, t.ts, t.amount, t.method, t.city, t.ip_country, t.bin_country,"
            "       t.merchant_id, t.customer_id, t.device_id,"
            "       d.model_score, d.final_score, d.band, d.action, d.rule_hits, d.features"
            "  FROM transactions t LEFT JOIN decisions d ON d.txn_id = t.id"
            " WHERE t.id = ?", (txn_id,)).fetchone()
        if row is None:
            return {"error": "transaction not found"}
        record = dict(row)
        features = jload(record.pop("features", None), {}) or {}
        record["rule_hits"] = jload(record.get("rule_hits"), [])
        # Only the features an analyst would actually read.
        record["key_features"] = {
            k: round(float(v), 3) for k, v in features.items()
            if k in {
                "amount_over_customer_typical", "customer_txn_24h", "device_txn_1m",
                "device_txn_24h", "device_distinct_customers_24h", "device_distinct_cards_24h",
                "device_is_new", "customer_is_new_city", "subnet_distinct_customers_24h",
                "card_distinct_merchants_24h", "merchant_volume_ratio_7d",
                "merchant_amount_zscore", "geo_bin_ip_mismatch", "geo_ip_offshore",
            }
        }
        return record

    def get_customer_history(self, customer_id: str, limit: int = 15) -> dict[str, Any]:
        """Recent payments, device and city spread, and any past disputes."""
        customer = self.conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if customer is None:
            return {"error": "customer not found"}
        rows = self.conn.execute(
            "SELECT t.id, t.ts, t.amount, t.method, t.city, t.device_id, t.merchant_id,"
            "       o.kind AS outcome"
            "  FROM transactions t LEFT JOIN outcomes o ON o.txn_id = t.id"
            " WHERE t.customer_id = ? ORDER BY t.ts DESC LIMIT ?",
            (customer_id, min(limit, MAX_ROWS))).fetchall()
        history = [dict(r) for r in rows]
        chargebacks = sum(1 for h in history if h.get("outcome") == "chargeback")
        return {
            "customer_id": customer_id,
            "home_city": customer["home_city"],
            "first_seen": customer["first_seen"],
            "typical_ticket": customer["typical_ticket"],
            "recent_transactions": history,
            "distinct_devices_in_window": len({h["device_id"] for h in history}),
            "distinct_cities_in_window": len({h["city"] for h in history}),
            "chargebacks_in_window": chargebacks,
        }

    def get_merchant_profile(self, merchant_id: str) -> dict[str, Any]:
        """Merchant standing plus its most recent portfolio risk snapshot.

        This is the link between transaction review and the merchant layer: a
        payment that looks ordinary on its own reads differently against a
        merchant whose volume tripled last week.
        """
        merchant = self.conn.execute(
            "SELECT * FROM merchants WHERE id = ?", (merchant_id,)).fetchone()
        if merchant is None:
            return {"error": "merchant not found"}
        snapshots = self.conn.execute(
            "SELECT date, risk_score, cb_ratio, volume, volume_delta, fraud_rate,"
            "       recommended_reserve_pct, recommended_txn_limit, memo"
            "  FROM merchant_risk_daily WHERE merchant_id = ?"
            " ORDER BY date DESC LIMIT 7", (merchant_id,)).fetchall()
        return {
            "merchant": dict(merchant),
            "risk_snapshots": [dict(s) for s in snapshots],
            "trend": _describe_trend([dict(s) for s in snapshots]),
        }

    def find_linked_entities(self, txn_id: str, hours: int = 24) -> dict[str, Any]:
        """Other accounts sharing this transaction's device, IP subnet or card.

        The window is symmetric around the transaction. That is deliberate and
        it is the agent's structural advantage: review happens after the payment
        was provisionally approved, so the agent can see activity the real-time
        scorer could not, including the rest of a ring that transacted minutes
        later. The first member of a fraud ring looks ordinary in real time and
        obvious an hour afterwards.
        """
        txn = self.conn.execute(
            "SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if txn is None:
            return {"error": "transaction not found"}
        centre = datetime.fromisoformat(txn["ts"])
        since = (centre - timedelta(hours=hours)).isoformat()
        until = (centre + timedelta(hours=hours)).isoformat()
        subnet = ".".join(txn["ip"].split(".")[:3]) + "."

        by_device = self.conn.execute(
            "SELECT DISTINCT customer_id FROM transactions"
            " WHERE device_id = ? AND ts >= ? AND ts <= ? LIMIT ?",
            (txn["device_id"], since, until, MAX_ROWS)).fetchall()
        by_subnet = self.conn.execute(
            "SELECT DISTINCT customer_id FROM transactions"
            " WHERE ip LIKE ? AND ts >= ? AND ts <= ? LIMIT ?",
            (subnet + "%", since, until, MAX_ROWS)).fetchall()
        by_card = self.conn.execute(
            "SELECT DISTINCT merchant_id FROM transactions"
            " WHERE card_fp = ? AND ts >= ? AND ts <= ? LIMIT ?",
            (txn["card_fp"], since, until, MAX_ROWS)).fetchall()
        return {
            "window_hours": hours,
            "window": "symmetric: " + since + " to " + until,
            "customers_on_same_device": [r["customer_id"] for r in by_device],
            "customers_on_same_subnet": [r["customer_id"] for r in by_subnet],
            "merchants_on_same_card": [r["merchant_id"] for r in by_card],
        }

    def get_similar_past_cases(self, txn_id: str, limit: int = 5) -> dict[str, Any]:
        """Previously resolved cases on the same merchant, with their outcomes.

        Resolved means an outcome actually arrived: a chargeback, a refund, or a
        clean settlement. That is the only label the agent is allowed to see.
        """
        txn = self.conn.execute(
            "SELECT merchant_id, ts FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if txn is None:
            return {"error": "transaction not found"}
        rows = self.conn.execute(
            "SELECT c.txn_id, c.verdict, c.confidence, c.rationale, o.kind AS outcome"
            "  FROM agent_cases c"
            "  JOIN transactions t ON t.id = c.txn_id"
            "  LEFT JOIN outcomes o ON o.txn_id = c.txn_id"
            " WHERE t.merchant_id = ? AND t.ts < ? AND c.verdict IS NOT NULL"
            " ORDER BY t.ts DESC LIMIT ?",
            (txn["merchant_id"], txn["ts"], min(limit, MAX_ROWS))).fetchall()
        return {"cases": [dict(r) for r in rows]}


def _describe_trend(snapshots: list[dict[str, Any]]) -> str:
    if len(snapshots) < 2:
        return "insufficient history"
    newest, oldest = snapshots[0], snapshots[-1]
    delta = newest["risk_score"] - oldest["risk_score"]
    direction = "rising" if delta > 0.05 else "falling" if delta < -0.05 else "flat"
    return "%s (%.2f over %d days)" % (direction, delta, len(snapshots))


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_transaction_context",
            "description": "The transaction under review, its score, and which rules fired.",
            "parameters": {
                "type": "object",
                "properties": {"txn_id": {"type": "string"}},
                "required": ["txn_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_history",
            "description": "Recent payments for a customer, their device and city spread, and past disputes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max rows, default 15."},
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_merchant_profile",
            "description": "Merchant standing, chargeback ratio trend, and recent risk snapshots.",
            "parameters": {
                "type": "object",
                "properties": {"merchant_id": {"type": "string"}},
                "required": ["merchant_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_linked_entities",
            "description": "Other customers or merchants sharing this transaction's device, IP subnet or card.",
            "parameters": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "hours": {"type": "integer", "description": "Lookback window, default 24."},
                },
                "required": ["txn_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_similar_past_cases",
            "description": "Earlier reviewed cases on the same merchant and how they resolved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["txn_id"],
            },
        },
    },
]
