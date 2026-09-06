/* Merchant portfolio.
 *
 * The slower clock. A payment that reads as ordinary on its own reads
 * differently against a merchant whose volume tripled last week, so the memo
 * on the right is the same text the agent sees while reviewing that merchant's
 * transactions.
 */
import React, { useEffect, useState } from "react";
import { api, day, inr, pct } from "../api";
import { Empty, Panel, Sparkline } from "../components/primitives";

function tierTone(score) {
  if (score >= 0.6) return "var(--decline)";
  if (score >= 0.4) return "var(--review)";
  return "var(--approve)";
}

export default function MerchantsView() {
  const [merchants, setMerchants] = useState([]);
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.merchants({ limit: 60, sort: "risk" }).then(setMerchants);
  }, []);

  async function open(id) {
    setDetail(null);
    setDetail(await api.merchant(id));
  }

  async function toggleFreeze() {
    if (!detail) return;
    setBusy(true);
    try {
      const next = !detail.merchant.frozen;
      await api.freeze(detail.merchant.id, next);
      setDetail({ ...detail, merchant: { ...detail.merchant, frozen: next ? 1 : 0 } });
    } finally {
      setBusy(false);
    }
  }

  const snapshots = detail ? [...detail.snapshots].reverse() : [];
  const latest = detail && detail.snapshots.length ? detail.snapshots[0] : null;

  return (
    <>
      <div className="page-head">
        <div className="eyebrow">Portfolio</div>
        <h2>Merchant risk</h2>
        <p>
          Ranked by rolling risk score. Chargeback ratio is measured against the 0.90%
          network monitoring threshold, which is the line that carries real penalties.
        </p>
      </div>

      <div className="grid stream">
        <Panel title="Merchants by risk" hint={merchants.length + " with activity"} bodyless>
          <div style={{ maxHeight: 560, overflowY: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Merchant</th>
                  <th className="num">Risk</th>
                  <th className="num">CB ratio</th>
                  <th className="num">Volume vs prior</th>
                  <th className="num">Reserve</th>
                </tr>
              </thead>
              <tbody>
                {merchants.map((merchant) => (
                  <tr key={merchant.id} className="clickable" onClick={() => open(merchant.id)}>
                    <td>
                      <strong>{merchant.name}</strong>
                      <div className="hint" style={{ color: "var(--muted)" }}>
                        {merchant.category}
                        {merchant.frozen ? " · frozen" : ""}
                      </div>
                    </td>
                    <td className="num" style={{ color: tierTone(merchant.risk_score || 0) }}>
                      {merchant.risk_score != null ? Number(merchant.risk_score).toFixed(2) : "—"}
                    </td>
                    <td className="num">{pct(merchant.cb_ratio, 2)}</td>
                    <td className="num">
                      {merchant.volume_delta != null
                        ? Number(merchant.volume_delta).toFixed(2) + "x"
                        : "—"}
                    </td>
                    <td className="num">{pct(merchant.recommended_reserve_pct, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="Underwriting memo">
          {!detail ? (
            <Empty>Select a merchant to read its memo and recent activity.</Empty>
          ) : (
            <div className="case">
              <div>
                <div className="eyebrow">{detail.merchant.category}</div>
                <div style={{ fontFamily: "var(--cond)", fontSize: 18, fontWeight: 700 }}>
                  {detail.merchant.name}
                </div>
              </div>

              <Sparkline points={snapshots.map((snapshot) => snapshot.risk_score)} width={280} />

              {latest ? (
                <div className="hint" style={{ fontFamily: "var(--mono)", color: "var(--muted)" }}>
                  risk {Number(latest.risk_score).toFixed(2)} · cb {pct(latest.cb_ratio, 2)} ·
                  {" "}30-day volume {inr(latest.volume)}
                </div>
              ) : null}

              <p className="memo">{latest ? latest.memo : "No snapshot yet."}</p>

              <div className="actions">
                <button className="btn" onClick={toggleFreeze} disabled={busy}>
                  {detail.merchant.frozen ? "Unfreeze merchant" : "Freeze merchant"}
                </button>
              </div>

              <div>
                <div className="eyebrow" style={{ marginBottom: 6 }}>Recent payments</div>
                <table>
                  <tbody>
                    {detail.recent_transactions.slice(0, 10).map((txn) => (
                      <tr key={txn.id}>
                        <td style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{day(txn.ts)}</td>
                        <td className="num">{inr(txn.amount)}</td>
                        <td className="num">{Number(txn.final_score || 0).toFixed(2)}</td>
                        <td>{txn.outcome || "pending"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
