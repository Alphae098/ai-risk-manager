import React, { useEffect, useState } from "react";
import { api } from "./api";
import LiveView from "./views/LiveView";
import CasesView from "./views/CasesView";
import MerchantsView from "./views/MerchantsView";
import RulesView from "./views/RulesView";
import EvaluationView from "./views/EvaluationView";

const VIEWS = [
  { key: "live", label: "Live" },
  { key: "cases", label: "Cases" },
  { key: "merchants", label: "Merchants" },
  { key: "rules", label: "Rules" },
  { key: "evaluation", label: "Evidence" },
];

export default function App() {
  const [view, setView] = useState("live");
  const [health, setHealth] = useState(null);
  const [overview, setOverview] = useState(null);
  const [offline, setOffline] = useState(false);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setOffline(true));
    api.overview().then(setOverview).catch(() => {});
  }, []);

  const bands = health ? health.bands : { approve_below: 0.15, decline_at: 0.8 };
  const openCases = overview ? overview.agent.escalated : null;

  return (
    <div className="shell">
      <nav className="rail">
        <div className="brand">
          <h1>Risk Console</h1>
          <p>payment risk operations</p>
        </div>

        <div className="nav">
          {VIEWS.map((item) => (
            <button
              key={item.key}
              onClick={() => setView(item.key)}
              aria-current={view === item.key ? "page" : undefined}
            >
              {item.label}
              {item.key === "cases" && openCases ? (
                <span className="count">{openCases}</span>
              ) : null}
            </button>
          ))}
        </div>

        <div className="rail-foot">
          {offline ? (
            <span>API unreachable. Start uvicorn on port 8000.</span>
          ) : health ? (
            <>
              <span>band {bands.approve_below}–{bands.decline_at}</span>
              <span>reviewer: {health.llm_mode}</span>
              <span>{health.rules_loaded} rules loaded</span>
              <span>{health.model_loaded ? "model ready" : "model not trained"}</span>
            </>
          ) : (
            <span>connecting…</span>
          )}
        </div>
      </nav>

      <main className="main">
        {view === "live" ? <LiveView bands={bands} /> : null}
        {view === "cases" ? <CasesView /> : null}
        {view === "merchants" ? <MerchantsView /> : null}
        {view === "rules" ? <RulesView /> : null}
        {view === "evaluation" ? <EvaluationView /> : null}
      </main>
    </div>
  );
}
