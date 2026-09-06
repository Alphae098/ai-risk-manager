/* Rules and their receipts.
 *
 * A rules engine rots when nobody can see which rules still fire and which
 * only produce false positives. Precision here is measured against arrived
 * outcomes, so a rule that has stopped earning its place says so.
 */
import React, { useEffect, useState } from "react";
import { api, pct } from "../api";
import { Empty, Panel } from "../components/primitives";

export default function RulesView() {
  const [rules, setRules] = useState([]);
  const [proposed, setProposed] = useState([]);

  useEffect(() => {
    api.rules().then(setRules);
    api.proposedRules().then(setProposed);
  }, []);

  async function decide(id, approve) {
    await api.decideRule(id, approve);
    setProposed((current) =>
      current.map((item) =>
        item.id === id ? { ...item, status: approve ? "approved" : "rejected" } : item));
  }

  return (
    <>
      <div className="page-head">
        <div className="eyebrow">Deterministic layer</div>
        <h2>Rules</h2>
        <p>
          Hard rules decline outright and never reach the model or the agent. Boost rules
          add to the score. Precision is measured against outcomes that actually arrived.
        </p>
      </div>

      <Panel title="Active rules" hint={rules.length + " loaded from rules.yaml"} bodyless>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Rule</th>
                <th>Condition</th>
                <th>Action</th>
                <th className="num">Hits</th>
                <th className="num">Precision</th>
                <th>Owner</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => (
                <tr key={rule.id}>
                  <td>
                    <span style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{rule.id}</span>
                    <div>{rule.description}</div>
                  </td>
                  <td style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--ink-soft)" }}>
                    {rule.when}
                  </td>
                  <td>
                    {rule.action === "block" ? (
                      <span className="band decline">block</span>
                    ) : rule.action === "score_boost" ? (
                      <span className="band review">+{rule.boost}</span>
                    ) : (
                      <span className="tag">flag</span>
                    )}
                  </td>
                  <td className="num">{rule.hits}</td>
                  <td className="num" style={{
                    color: rule.precision == null
                      ? "var(--muted)"
                      : rule.precision >= 0.5 ? "var(--approve)" : "var(--decline)",
                  }}>
                    {rule.precision == null ? "never fired" : pct(rule.precision, 1)}
                  </td>
                  <td className="hint">{rule.owner}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <div style={{ height: 16 }} />

      <Panel title="Drafted by the agent" hint="nothing goes live without a person editing rules.yaml">
        {proposed.length === 0 ? (
          <Empty>The agent has not drafted any rules yet.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Pattern</th>
                <th>Condition</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {proposed.map((item) => (
                <tr key={item.id}>
                  <td>{item.description}</td>
                  <td style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{item.condition}</td>
                  <td>{item.status}</td>
                  <td>
                    {item.status === "pending" ? (
                      <div className="actions">
                        <button className="btn legit" onClick={() => decide(item.id, true)}>
                          Approve
                        </button>
                        <button className="btn" onClick={() => decide(item.id, false)}>
                          Reject
                        </button>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </>
  );
}
