/* Live decisions.
 *
 * The spine on the left is the router made visible: the vertical axis is the
 * fraud score, the shaded middle is the uncertain band, and every payment
 * docks at its own score. Widen the band in config and this picture changes
 * shape, which is the point - the thresholds are a product decision, not a
 * constant buried in the code.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { api, clock, inr, openStream, pct } from "../api";
import CaseCard from "../components/CaseCard";
import { BAND_COLOR, Band, Empty, Panel, Stat } from "../components/primitives";

const SPINE_HEIGHT = 520;
const MAX_ROWS = 60;

function BandSpine({ rows, bands }) {
  const declineTop = 0;
  const declineHeight = (1 - bands.decline_at) * SPINE_HEIGHT;
  const reviewHeight = (bands.decline_at - bands.approve_below) * SPINE_HEIGHT;
  const approveHeight = bands.approve_below * SPINE_HEIGHT;

  return (
    <div className="spine" style={{ height: SPINE_HEIGHT }}>
      <div className="spine-zone decline" style={{ top: declineTop, height: declineHeight }} />
      <div className="spine-zone review" style={{ top: declineHeight, height: reviewHeight }} />
      <div className="spine-zone approve"
           style={{ top: declineHeight + reviewHeight, height: approveHeight }} />

      <div className="spine-line" style={{ top: declineHeight }} />
      <div className="spine-line" style={{ top: declineHeight + reviewHeight }} />

      <span className="spine-label" style={{ top: declineHeight }}>
        {bands.decline_at.toFixed(2)}
      </span>
      <span className="spine-label" style={{ top: declineHeight + reviewHeight }}>
        {bands.approve_below.toFixed(2)}
      </span>

      <span className="spine-zone-label" style={{ top: declineHeight / 2 }}>decline</span>
      <span className="spine-zone-label" style={{ top: declineHeight + reviewHeight / 2 }}>
        review
      </span>
      <span className="spine-zone-label"
            style={{ top: declineHeight + reviewHeight + approveHeight / 2 }}>
        approve
      </span>

      {rows.map((row, index) => {
        const score = Number(row.final_score || 0);
        const x = 10 + (index / Math.max(1, MAX_ROWS - 1)) * 62;
        return (
          <span
            key={row.id || index}
            className="spine-dot"
            style={{
              top: (1 - score) * SPINE_HEIGHT,
              left: x,
              background: BAND_COLOR[row.band] || "var(--muted)",
            }}
          />
        );
      })}
    </div>
  );
}

export default function LiveView({ bands }) {
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(null);
  const [caseRecord, setCaseRecord] = useState(null);
  const [live, setLive] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const socketRef = useRef(null);

  useEffect(() => {
    api.transactions({ limit: MAX_ROWS }).then((data) => setRows(data.reverse()));
  }, []);

  useEffect(() => {
    if (!live) {
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
      return undefined;
    }
    const socket = openStream((message) => {
      if (message.type === "transaction") {
        setRows((current) => {
          const next = [...current, { ...message.txn, ...message, id: message.txn.id }];
          return next.slice(-MAX_ROWS);
        });
      }
      if (message.type === "case") {
        setCaseRecord((current) =>
          current && current.txn_id !== message.txn_id ? current : message);
      }
    });
    socketRef.current = socket;
    return () => socket.close();
  }, [live]);

  const counts = useMemo(() => {
    const tally = { approve: 0, review: 0, decline: 0 };
    rows.forEach((row) => { tally[row.band] = (tally[row.band] || 0) + 1; });
    return tally;
  }, [rows]);

  async function inspect(row) {
    setSelected(row.id);
    setCaseRecord(null);
    const existing = await api.cases({ limit: 200 });
    const match = existing.find((item) => item.txn_id === row.id);
    if (match) {
      setCaseRecord(match);
      return;
    }
    if (row.band === "review") {
      setReviewing(true);
      try {
        const fresh = await api.reviewNow(row.id);
        setCaseRecord({ ...fresh, txn_id: row.id, amount: row.amount,
                        merchant_name: row.merchant_name });
      } finally {
        setReviewing(false);
      }
    }
  }

  const total = rows.length || 1;

  return (
    <>
      <div className="page-head">
        <div className="eyebrow">Live</div>
        <h2>Decisions as they happen</h2>
        <p>
          Each payment is scored, routed, and - only in the shaded band - handed to the
          analyst agent. Select any row to see what the system concluded and why.
        </p>
      </div>

      <div className="stat-row">
        <Stat label="In view" value={rows.length} sub="most recent decisions" />
        <Stat label="Approved" value={pct(counts.approve / total, 0)}
              sub={counts.approve + " payments"} />
        <Stat label="Sent to review" value={pct(counts.review / total, 1)}
              sub={counts.review + " agent cases"} />
        <Stat label="Declined" value={pct(counts.decline / total, 1)}
              sub={counts.decline + " payments"} />
      </div>

      <div className="grid stream">
        <Panel
          title="Score spine"
          hint={
            <button className="btn" onClick={() => setLive((value) => !value)}>
              {live ? "Pause stream" : "Start stream"}
            </button>
          }
          bodyless
        >
          <div className="spine-wrap">
            <BandSpine rows={rows} bands={bands} />
            <div className="stream">
              {rows.length === 0 ? (
                <Empty>No decisions yet. Run the backfill, then start the stream.</Empty>
              ) : (
                [...rows].reverse().map((row) => (
                  <button
                    key={row.id}
                    className="stream-row"
                    aria-pressed={selected === row.id}
                    onClick={() => inspect(row)}
                  >
                    <span className="time">{clock(row.ts)}</span>
                    <span className="who">
                      <strong>{row.merchant_name || row.merchant_id}</strong>
                      <span>{row.method} · {row.city || "-"} · {row.id}</span>
                    </span>
                    <span className="amount">{inr(row.amount)}</span>
                    <span className="score">{Number(row.final_score).toFixed(3)}</span>
                    <Band band={row.band} />
                  </button>
                ))
              )}
            </div>
          </div>
        </Panel>

        <Panel title="Review" hint={reviewing ? "investigating…" : null}>
          <CaseCard record={caseRecord} />
        </Panel>
      </div>
    </>
  );
}
