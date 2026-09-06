"""HTTP and WebSocket API for the analyst dashboard.

Run: uvicorn arm.api.main:app --reload --port 8000

The live stream replays stored transactions through the real pipeline at an
accelerated clock. Nothing is pre-computed for the demo: every card the
dashboard shows came out of the same code path the evaluation measured.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ..agent.analyst import RiskAnalystAgent
from ..config import BANDS, DATA_DIR, LLM
from ..db import connect, init_db, jload
from ..features.engine import FeatureEngine
from ..merchant.portfolio import run_daily_rollup
from ..scoring.model import FraudModel
from ..scoring.router import DecisionPipeline
from ..scoring.rules import RulesEngine

state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    init_db(conn)
    merchants = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM merchants")}
    customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
    try:
        model = FraudModel.load()
    except FileNotFoundError:
        model = None  # the API still serves history; scoring endpoints will say so
    state.update({
        "conn": conn,
        "pipeline": DecisionPipeline(model=model, rules=RulesEngine(),
                                     features=FeatureEngine(merchants, customers)),
        "agent": RiskAnalystAgent(conn),
        "merchants": merchants,
        "customers": customers,
    })
    yield
    conn.close()


app = FastAPI(title="AI Risk Manager", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"], allow_headers=["*"],
)


def db():
    return state["conn"]


def rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in db().execute(sql, params)]


# --------------------------------------------------------------------- models

class OverrideRequest(BaseModel):
    action: str = Field(description="confirm_fraud | confirm_legit")
    note: str = ""


class ScoreRequest(BaseModel):
    merchant_id: str
    customer_id: str
    amount: float
    method: str = "card"
    device_id: str = "dev_adhoc"
    ip: str = "49.1.1.1"
    card_fp: str = "card_adhoc"
    bin_country: str = "IN"
    ip_country: str = "IN"
    city: str = "Mumbai"


# ---------------------------------------------------------------- system view

@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model_loaded": state["pipeline"].model is not None,
        "llm_mode": "mock (offline heuristic)" if LLM.use_mock else LLM.model,
        "bands": {"approve_below": BANDS.approve_below, "decline_at": BANDS.decline_at},
        "rules_loaded": len(state["pipeline"].rules.rules),
    }


@app.get("/api/metrics/overview")
def overview() -> dict[str, Any]:
    totals = rows(
        "SELECT band, COUNT(*) n, SUM(t.is_fraud) fraud FROM decisions d"
        "  JOIN transactions t ON t.id = d.txn_id GROUP BY band")
    volume = db().execute("SELECT COUNT(*) n, COALESCE(SUM(amount),0) v FROM transactions").fetchone()
    cases = db().execute(
        "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,"
        "       SUM(CASE WHEN verdict='fraud' THEN 1 ELSE 0 END) fraud_calls,"
        "       SUM(CASE WHEN status='escalated' THEN 1 ELSE 0 END) escalated"
        "  FROM agent_cases").fetchone()
    chargebacks = db().execute(
        "SELECT COUNT(*) n, COALESCE(SUM(t.amount),0) v FROM outcomes o"
        "  JOIN transactions t ON t.id = o.txn_id WHERE o.kind='chargeback'").fetchone()
    total_txns = max(1, int(volume["n"]))
    return {
        "transactions": int(volume["n"]),
        "gross_value_inr": round(float(volume["v"]), 2),
        "bands": {r["band"]: {"count": r["n"], "fraud": r["fraud"] or 0} for r in totals},
        "review_rate": round(
            next((r["n"] for r in totals if r["band"] == "review"), 0) / total_txns, 5),
        "agent": {
            "cases": int(cases["n"] or 0),
            "fraud_calls": int(cases["fraud_calls"] or 0),
            "escalated": int(cases["escalated"] or 0),
            "total_cost_usd": round(float(cases["cost"] or 0), 4),
            "cost_per_1k_txn_usd": round(float(cases["cost"] or 0) / total_txns * 1000, 4),
        },
        "chargebacks": {"count": int(chargebacks["n"] or 0),
                        "value_inr": round(float(chargebacks["v"] or 0), 2)},
    }


@app.get("/api/evaluation")
def evaluation() -> dict[str, Any]:
    path = DATA_DIR / "evaluation.json"
    if not path.exists():
        raise HTTPException(404, "Run `python -m eval.harness` first.")
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------- transactions

@app.get("/api/transactions")
def transactions(band: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    clause = " WHERE d.band = ?" if band else ""
    params: tuple = (band, limit, offset) if band else (limit, offset)
    return rows(
        "SELECT t.id, t.ts, t.amount, t.method, t.city, t.merchant_id, t.customer_id,"
        "       t.is_fraud, d.model_score, d.final_score, d.band, d.action, d.rule_hits,"
        "       m.name AS merchant_name, o.kind AS outcome,"
        "       c.id AS case_id, c.verdict, c.confidence"
        "  FROM decisions d"
        "  JOIN transactions t ON t.id = d.txn_id"
        "  JOIN merchants m ON m.id = t.merchant_id"
        "  LEFT JOIN outcomes o ON o.txn_id = t.id"
        "  LEFT JOIN agent_cases c ON c.txn_id = t.id"
        + clause + " ORDER BY t.ts DESC LIMIT ? OFFSET ?", params)


@app.post("/api/score")
def score_adhoc(request: ScoreRequest) -> dict[str, Any]:
    """Score a hypothetical payment. Used by the dashboard's what-if panel."""
    pipeline: DecisionPipeline = state["pipeline"]
    if pipeline.model is None:
        raise HTTPException(503, "Model not trained. Run `python -m arm.scoring.train`.")
    txn = request.model_dump()
    txn["id"] = "txn_adhoc_" + datetime.now().strftime("%H%M%S%f")
    txn["ts"] = datetime.now().isoformat()
    decision = pipeline.decide(txn)
    return {
        "txn_id": decision.txn_id,
        "model_score": round(decision.model_score, 4),
        "rule_boost": decision.rule_boost,
        "final_score": round(decision.final_score, 4),
        "band": decision.band,
        "action": decision.action,
        "rule_hits": decision.rule_details,
        "latency_ms": round(decision.latency_ms, 2),
        "top_contributors": decision.top_contributors,
    }


# ---------------------------------------------------------------------- cases

@app.get("/api/cases")
def cases(status: str | None = None, limit: int = 50) -> list[dict]:
    clause = " WHERE c.status = ?" if status else ""
    params: tuple = (status, limit) if status else (limit,)
    records = rows(
        "SELECT c.*, t.amount, t.ts AS txn_ts, t.merchant_id, m.name AS merchant_name,"
        "       t.is_fraud, d.final_score, o.analyst_override"
        "  FROM agent_cases c"
        "  JOIN transactions t ON t.id = c.txn_id"
        "  JOIN merchants m ON m.id = t.merchant_id"
        "  LEFT JOIN decisions d ON d.txn_id = c.txn_id"
        "  LEFT JOIN outcomes o ON o.txn_id = c.txn_id"
        + clause + " ORDER BY c.created_at DESC LIMIT ?", params)
    for record in records:
        record["evidence"] = jload(record.get("evidence"), [])
        record["tool_calls"] = jload(record.get("tool_calls"), [])
        record["suggested_rule"] = jload(record.get("suggested_rule"), None)
    return records


@app.post("/api/cases/{txn_id}/review")
def review_now(txn_id: str) -> dict[str, Any]:
    """Run the analyst agent against one transaction on demand."""
    exists = db().execute("SELECT 1 FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if not exists:
        raise HTTPException(404, "Transaction not found")
    case = state["agent"].review(txn_id)
    return {
        "case_id": case.id, "verdict": case.verdict, "confidence": case.confidence,
        "rationale": case.rationale, "evidence": case.evidence,
        "recommended_action": case.recommended_action,
        "suggested_rule": case.suggested_rule,
        "tool_calls": case.tool_calls, "cost_usd": round(case.cost_usd, 6),
        "source": case.llm_source,
    }


@app.post("/api/cases/{txn_id}/override")
def override(txn_id: str, request: OverrideRequest) -> dict[str, Any]:
    """Record an analyst's decision. This is the label the system learns from."""
    if request.action not in {"confirm_fraud", "confirm_legit"}:
        raise HTTPException(400, "action must be confirm_fraud or confirm_legit")
    kind = "chargeback" if request.action == "confirm_fraud" else "clean"
    db().execute(
        "INSERT INTO outcomes (txn_id, kind, arrived_at, analyst_override, analyst_note)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(txn_id) DO UPDATE SET analyst_override=excluded.analyst_override,"
        "   analyst_note=excluded.analyst_note",
        (txn_id, kind, datetime.now().isoformat(), request.action, request.note))
    db().execute("UPDATE agent_cases SET status='closed' WHERE txn_id=?", (txn_id,))
    db().commit()
    return {"txn_id": txn_id, "recorded": request.action}


# ------------------------------------------------------------------ merchants

@app.get("/api/merchants")
def merchants(limit: int = 50, sort: str = "risk") -> list[dict]:
    order = "s.risk_score DESC" if sort == "risk" else "s.volume DESC"
    return rows(
        "SELECT m.id, m.name, m.category, m.risk_tier, m.reserve_pct, m.txn_limit, m.frozen,"
        "       s.risk_score, s.cb_ratio, s.volume, s.volume_delta, s.fraud_rate,"
        "       s.recommended_reserve_pct, s.recommended_txn_limit, s.date"
        "  FROM merchants m"
        "  LEFT JOIN merchant_risk_daily s ON s.merchant_id = m.id"
        "   AND s.date = (SELECT MAX(date) FROM merchant_risk_daily WHERE merchant_id = m.id)"
        " ORDER BY " + order + " LIMIT ?", (limit,))


@app.get("/api/merchants/{merchant_id}")
def merchant_detail(merchant_id: str) -> dict[str, Any]:
    merchant = db().execute("SELECT * FROM merchants WHERE id = ?", (merchant_id,)).fetchone()
    if merchant is None:
        raise HTTPException(404, "Merchant not found")
    snapshots = rows(
        "SELECT * FROM merchant_risk_daily WHERE merchant_id = ? ORDER BY date DESC LIMIT 30",
        (merchant_id,))
    recent = rows(
        "SELECT t.id, t.ts, t.amount, t.method, d.final_score, d.band, o.kind AS outcome"
        "  FROM transactions t LEFT JOIN decisions d ON d.txn_id = t.id"
        "  LEFT JOIN outcomes o ON o.txn_id = t.id"
        " WHERE t.merchant_id = ? ORDER BY t.ts DESC LIMIT 25", (merchant_id,))
    return {"merchant": dict(merchant), "snapshots": snapshots, "recent_transactions": recent}


@app.post("/api/merchants/rollup")
def rollup() -> dict[str, Any]:
    snapshots = run_daily_rollup(db())
    return {"snapshots": len(snapshots)}


@app.post("/api/merchants/{merchant_id}/freeze")
def freeze(merchant_id: str, frozen: bool = True) -> dict[str, Any]:
    db().execute("UPDATE merchants SET frozen = ? WHERE id = ?", (1 if frozen else 0, merchant_id))
    db().commit()
    state["merchants"][merchant_id]["frozen"] = 1 if frozen else 0
    return {"merchant_id": merchant_id, "frozen": frozen}


# ---------------------------------------------------------------------- rules

@app.get("/api/rules")
def rules() -> list[dict]:
    stats = {r["rule_id"]: dict(r) for r in db().execute("SELECT * FROM rule_stats")}
    output = []
    for rule in state["pipeline"].rules.rules:
        stat = stats.get(rule.id, {})
        hits = int(stat.get("hits", 0))
        tp = int(stat.get("true_positives", 0))
        output.append({
            "id": rule.id, "description": rule.description, "when": rule.when,
            "action": rule.action, "boost": rule.boost, "owner": rule.owner,
            "hits": hits, "true_positives": tp,
            "false_positives": int(stat.get("false_positives", 0)),
            "precision": round(tp / hits, 4) if hits else None,
            "last_hit_at": stat.get("last_hit_at"),
        })
    return output


@app.post("/api/rules/reload")
def reload_rules() -> dict[str, Any]:
    state["pipeline"].rules.load()
    return {"rules_loaded": len(state["pipeline"].rules.rules)}


@app.get("/api/proposed-rules")
def proposed_rules() -> list[dict]:
    return rows("SELECT * FROM proposed_rules ORDER BY created_at DESC LIMIT 50")


@app.post("/api/proposed-rules/{rule_id}/decide")
def decide_rule(rule_id: int, approve: bool) -> dict[str, Any]:
    """Approve or reject an agent-drafted rule.

    Approval marks it for a human to paste into rules.yaml. Nothing the agent
    writes reaches the live engine without a person editing that file.
    """
    db().execute("UPDATE proposed_rules SET status = ? WHERE id = ?",
                 ("approved" if approve else "rejected", rule_id))
    db().commit()
    return {"id": rule_id, "status": "approved" if approve else "rejected"}


# ------------------------------------------------------------------ live feed

@app.websocket("/ws/stream")
async def stream(websocket: WebSocket) -> None:
    """Replay stored transactions through the live pipeline at an accelerated clock."""
    await websocket.accept()
    conn = connect()
    pipeline = DecisionPipeline(model=state["pipeline"].model, rules=RulesEngine(),
                                features=FeatureEngine(state["merchants"], state["customers"]))
    agent = RiskAnalystAgent(conn)
    try:
        cursor = conn.execute(
            "SELECT * FROM transactions ORDER BY ts DESC LIMIT 3000")
        batch = [dict(r) for r in cursor][::-1]
        for txn in batch:
            decision = pipeline.decide(txn)
            payload = {
                "type": "transaction",
                "txn": {k: txn[k] for k in ("id", "ts", "amount", "method", "city",
                                            "merchant_id", "customer_id")},
                "merchant_name": state["merchants"].get(txn["merchant_id"], {}).get("name"),
                "model_score": round(decision.model_score, 4),
                "final_score": round(decision.final_score, 4),
                "band": decision.band,
                "action": decision.action,
                "rule_hits": decision.rule_hits,
                "latency_ms": round(decision.latency_ms, 2),
            }
            await websocket.send_text(json.dumps(payload))

            if decision.needs_agent:
                case = agent.review(txn["id"])
                await websocket.send_text(json.dumps({
                    "type": "case",
                    "txn_id": txn["id"],
                    "case_id": case.id,
                    "verdict": case.verdict,
                    "confidence": case.confidence,
                    "rationale": case.rationale,
                    "evidence": case.evidence,
                    "recommended_action": case.recommended_action,
                    "source": case.llm_source,
                }))
            await asyncio.sleep(0.35)
    except WebSocketDisconnect:
        pass
    finally:
        conn.close()
