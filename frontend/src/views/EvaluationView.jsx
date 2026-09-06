/* Evidence.
 *
 * The claim this project makes is that routing by confidence beats both
 * baselines while keeping LLM cost flat. This screen is where that claim is
 * either supported or not, on the held-out period only.
 */
import React, { useEffect, useState } from "react";
import { api, inr, pct } from "../api";
import { Empty, Panel, Stat } from "../components/primitives";

function SweepChart({ points }) {
  if (!points || points.length === 0) return null;
  const width = 620;
  const height = 220;
  const padding = { top: 16, right: 46, bottom: 34, left: 46 };
  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;

  const reviewRates = points.map((point) => point.review_rate);
  const recalls = points.map((point) => point.recall);
  const maxReview = Math.max(...reviewRates) * 1.1 || 0.01;
  const minRecall = Math.min(...recalls) - 0.02;
  const maxRecall = Math.max(...recalls) + 0.02;

  const x = (value) => padding.left + (value / maxReview) * innerWidth;
  const y = (value) =>
    padding.top + innerHeight - ((value - minRecall) / (maxRecall - minRecall)) * innerHeight;

  const path = points
    .map((point, index) =>
      (index === 0 ? "M" : "L") + x(point.review_rate).toFixed(1) + " " + y(point.recall).toFixed(1))
    .join(" ");

  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} role="img"
         aria-label="Recall against review rate as the uncertain band widens">
      <line x1={padding.left} y1={padding.top + innerHeight} x2={width - padding.right}
            y2={padding.top + innerHeight} stroke="var(--rule-strong)" />
      <line x1={padding.left} y1={padding.top} x2={padding.left}
            y2={padding.top + innerHeight} stroke="var(--rule-strong)" />
      <path d={path} fill="none" stroke="var(--ink)" strokeWidth="1.5" />
      {points.map((point, index) => (
        <g key={index}>
          <circle cx={x(point.review_rate)} cy={y(point.recall)} r="4" fill="var(--review)" />
          <text x={x(point.review_rate)} y={y(point.recall) - 10} textAnchor="middle"
                fontFamily="var(--mono)" fontSize="10" fill="var(--muted)">
            {point.band[0].toFixed(2)}
          </text>
        </g>
      ))}
      <text x={padding.left} y={height - 10} fontFamily="var(--cond)" fontSize="11"
            fill="var(--muted)" letterSpacing="0.1em">REVIEW RATE →</text>
      <text x={10} y={padding.top + 4} fontFamily="var(--cond)" fontSize="11"
            fill="var(--muted)" letterSpacing="0.1em">RECALL ↑</text>
    </svg>
  );
}

export default function EvaluationView() {
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.evaluation().then(setReport).catch((problem) => setError(problem.message));
  }, []);

  if (error) {
    return (
      <Panel title="No evaluation yet">
        <Empty>Run <code>python -m eval.harness</code> to produce the report.</Empty>
      </Panel>
    );
  }
  if (!report) return <Empty>Loading the report…</Empty>;

  const hybrid = report.strategies.find((item) => item.strategy.startsWith("hybrid"));
  const rulesOnly = report.strategies.find((item) => item.strategy === "rules only");
  const gain = hybrid && rulesOnly ? hybrid.recall - rulesOnly.recall : 0;

  return (
    <>
      <div className="page-head">
        <div className="eyebrow">Held-out period</div>
        <h2>Does the routing earn its keep?</h2>
        <p>
          {report.holdout_transactions.toLocaleString("en-IN")} transactions the model never
          saw during training, {report.holdout_fraud} of them fraudulent.
        </p>
      </div>

      <div className="stat-row">
        <Stat label="Recall, hybrid" value={pct(hybrid.recall)}
              sub={"+" + (gain * 100).toFixed(0) + " points over rules alone"} />
        <Stat label="False positive rate" value={pct(hybrid.false_positive_rate, 3)}
              sub="share of legitimate payments declined" />
        <Stat label="Sent to the agent" value={pct(hybrid.review_rate, 2)}
              sub="the only traffic that costs LLM tokens" />
        <Stat label="LLM cost / 1k txn"
              value={"$" + (report.band_sweep.find(
                (point) => point.band[0] === report.current_band[0])?.llm_cost_per_1k_txn_usd ?? 0
              ).toFixed(4)}
              sub={report.agent.cost_per_case_usd === 0
                ? "offline heuristic reviewer"
                : "$" + report.agent.cost_per_case_usd.toFixed(4) + " per case"} />
      </div>

      <div className="grid two">
        <Panel title="Strategy comparison" hint="matched decline budget" bodyless>
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th className="num">Precision</th>
                <th className="num">Recall</th>
                <th className="num">FP rate</th>
                <th className="num">Net</th>
              </tr>
            </thead>
            <tbody>
              {report.strategies.map((strategy) => (
                <tr key={strategy.strategy}>
                  <td>{strategy.strategy}</td>
                  <td className="num">{strategy.precision.toFixed(3)}</td>
                  <td className="num">{strategy.recall.toFixed(3)}</td>
                  <td className="num">{strategy.false_positive_rate.toFixed(4)}</td>
                  <td className="num">{inr(strategy.net_inr)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Widening the band" hint="each point is one threshold setting">
          <SweepChart points={report.band_sweep} />
          <p className="hint" style={{ color: "var(--muted)" }}>
            Recall keeps climbing as more traffic reaches the agent, and review rate is what
            you pay for it. The shipped setting is {report.current_band[0]}–{report.current_band[1]}.
          </p>
        </Panel>
      </div>

      <div style={{ height: 16 }} />

      <div className="grid two">
        <Panel title="Agent behaviour">
          <table>
            <tbody>
              <tr><td>Cases resolved</td><td className="num">{report.agent.cases}</td></tr>
              <tr><td>Catch rate on review-band fraud</td>
                  <td className="num">{pct(report.agent.catch_rate)}</td></tr>
              <tr><td>Precision when it calls fraud</td>
                  <td className="num">{pct(report.agent.precision)}</td></tr>
              <tr><td>Escalated to a human</td>
                  <td className="num">{pct(report.agent.escalation_rate)}</td></tr>
            </tbody>
          </table>
        </Panel>

        <Panel title="Authorization latency" hint="features, rules and model on one transaction">
          <table>
            <tbody>
              {Object.entries(report.latency).map(([key, value]) => (
                <tr key={key}>
                  <td>{key.replace("_ms", "").replace("_", " ")}</td>
                  <td className="num">{typeof value === "number" ? value.toFixed(2) : value} ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </>
  );
}
