/* One agent case: what it decided, on what evidence, and what a human can do
 * about it. The evidence list is the point of the screen - a verdict without
 * cited evidence is not reviewable. */
import React, { useState } from "react";
import { api, inr } from "../api";
import { Empty } from "./primitives";

const VERDICT_TONE = {
  fraud: "var(--decline)",
  legit: "var(--approve)",
  insufficient_evidence: "var(--review)",
};

const VERDICT_LABEL = {
  fraud: "Fraud",
  legit: "Legitimate",
  insufficient_evidence: "Needs a human",
};

export default function CaseCard({ record, onResolved }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  if (!record) {
    return <Empty>Select a transaction to see how it was reviewed.</Empty>;
  }

  const evidence = record.evidence || [];
  const toolCalls = record.tool_calls || [];

  async function record_outcome(action) {
    setBusy(true);
    setMessage("");
    try {
      await api.override(record.txn_id, action);
      setMessage(action === "confirm_fraud" ? "Recorded as fraud." : "Recorded as legitimate.");
      if (onResolved) onResolved(record.txn_id, action);
    } catch (error) {
      setMessage("Could not record that: " + error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="case">
      <div className="case-verdict" style={{ color: VERDICT_TONE[record.verdict] }}>
        {VERDICT_LABEL[record.verdict] || "Under review"}
        {record.confidence != null ? (
          <span className="hint" style={{ fontFamily: "var(--mono)", color: "var(--muted)" }}>
            confidence {Number(record.confidence).toFixed(2)}
          </span>
        ) : null}
      </div>

      {record.amount != null ? (
        <div className="hint" style={{ fontFamily: "var(--mono)", color: "var(--muted)" }}>
          {inr(record.amount)} &middot; {record.merchant_name || record.merchant_id}
        </div>
      ) : null}

      <p className="rationale">{record.rationale}</p>

      <div>
        <div className="eyebrow" style={{ marginBottom: 6 }}>Evidence</div>
        {evidence.length ? (
          <ul className="evidence">
            {evidence.map((item, index) => (
              <li key={index}>
                <div>{item.fact}</div>
                <div className="src">
                  {item.source}
                  {item.weight ? (
                    <span className={"weight " + item.weight}> &middot; {item.weight}</span>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>No evidence was cited.</Empty>
        )}
      </div>

      {toolCalls.length ? (
        <div>
          <div className="eyebrow" style={{ marginBottom: 6 }}>Investigation trail</div>
          <div>
            {toolCalls.map((call, index) => (
              <span className="tag" key={index}>{call.name}</span>
            ))}
          </div>
        </div>
      ) : null}

      {record.recommended_action ? (
        <div>
          <div className="eyebrow" style={{ marginBottom: 4 }}>Recommended</div>
          <div style={{ fontFamily: "var(--mono)" }}>{record.recommended_action}</div>
        </div>
      ) : null}

      {record.suggested_rule ? (
        <div>
          <div className="eyebrow" style={{ marginBottom: 4 }}>Drafted rule, awaiting approval</div>
          <div className="memo">
            {record.suggested_rule.description}
            <div style={{ fontFamily: "var(--mono)", fontSize: 12, marginTop: 4 }}>
              {record.suggested_rule.when}
            </div>
          </div>
        </div>
      ) : null}

      <div className="actions">
        <button className="btn fraud" disabled={busy}
                onClick={() => record_outcome("confirm_fraud")}>
          Confirm fraud
        </button>
        <button className="btn legit" disabled={busy}
                onClick={() => record_outcome("confirm_legit")}>
          Confirm legitimate
        </button>
      </div>
      {message ? <div className="hint">{message}</div> : null}
      <div className="hint" style={{ color: "var(--muted)" }}>
        Reviewed by {record.llm_source || record.source || "heuristic"}
        {record.cost_usd ? " · $" + Number(record.cost_usd).toFixed(4) : ""}
      </div>
    </div>
  );
}
