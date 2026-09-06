/* The queue. Cases the agent escalated come first, because those are the ones
 * waiting on a person. */
import React, { useEffect, useState } from "react";
import { api, day, inr } from "../api";
import CaseCard from "../components/CaseCard";
import { Empty, Panel } from "../components/primitives";

const FILTERS = [
  { key: "escalated", label: "Waiting on a human" },
  { key: "resolved", label: "Resolved by the agent" },
  { key: "", label: "Everything" },
];

const VERDICT_TONE = {
  fraud: "var(--decline)",
  legit: "var(--approve)",
  insufficient_evidence: "var(--review)",
};

export default function CasesView() {
  const [filter, setFilter] = useState("escalated");
  const [cases, setCases] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const params = filter ? { status: filter, limit: 100 } : { limit: 100 };
    api.cases(params).then((data) => {
      setCases(data);
      setSelected(data[0] || null);
      setLoading(false);
    });
  }, [filter]);

  function handleResolved(txnId, action) {
    setCases((current) =>
      current.map((item) =>
        item.txn_id === txnId ? { ...item, analyst_override: action, status: "closed" } : item));
  }

  return (
    <>
      <div className="page-head">
        <div className="eyebrow">Queue</div>
        <h2>Cases</h2>
        <p>
          Every case here came from the uncertain band. The agent resolved what it could
          and escalated the rest; your decision becomes the label the model retrains on.
        </p>
      </div>

      <div className="actions" style={{ marginBottom: 14 }}>
        {FILTERS.map((option) => (
          <button
            key={option.key}
            className="btn"
            style={filter === option.key ? { borderColor: "var(--ink)" } : undefined}
            onClick={() => setFilter(option.key)}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="grid stream">
        <Panel title="Open cases" hint={cases.length + " shown"} bodyless>
          <div style={{ maxHeight: 560, overflowY: "auto" }}>
            {loading ? (
              <Empty>Loading cases…</Empty>
            ) : cases.length === 0 ? (
              <Empty>Nothing in this queue.</Empty>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Merchant</th>
                    <th className="num">Amount</th>
                    <th className="num">Score</th>
                    <th>Verdict</th>
                  </tr>
                </thead>
                <tbody>
                  {cases.map((item) => (
                    <tr
                      key={item.id}
                      className={"clickable" + (selected && selected.id === item.id ? " selected" : "")}
                      onClick={() => setSelected(item)}
                    >
                      <td style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{day(item.txn_ts)}</td>
                      <td>{item.merchant_name}</td>
                      <td className="num">{inr(item.amount)}</td>
                      <td className="num">{Number(item.final_score || 0).toFixed(3)}</td>
                      <td style={{ color: VERDICT_TONE[item.verdict] }}>
                        {item.verdict === "insufficient_evidence" ? "needs a human" : item.verdict}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </Panel>

        <Panel title="Case detail">
          <CaseCard record={selected} onResolved={handleResolved} />
        </Panel>
      </div>
    </>
  );
}
