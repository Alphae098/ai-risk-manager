/* Small shared pieces. Colour appears here and nowhere else by accident:
 * a band chip, a sparkline, a stat. Everything else stays neutral. */
import React from "react";

export const BAND_COLOR = {
  approve: "var(--approve)",
  review: "var(--review)",
  decline: "var(--decline)",
};

export function Band({ band }) {
  if (!band) return null;
  return <span className={"band " + band}>{band}</span>;
}

export function Stat({ label, value, sub }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

export function Panel({ title, hint, children, bodyless }) {
  return (
    <section className="panel">
      {title ? (
        <div className="panel-head">
          <h3>{title}</h3>
          {hint ? <span className="hint">{hint}</span> : null}
        </div>
      ) : null}
      {bodyless ? children : <div className="panel-body">{children}</div>}
    </section>
  );
}

/** Risk score over time for one merchant. No axes: the shape is the message. */
export function Sparkline({ points, width = 160, height = 34 }) {
  if (!points || points.length < 2) {
    return <span className="hint">not enough history</span>;
  }
  const values = points.map(Number);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  const path = values
    .map((value, index) => {
      const x = index * step;
      const y = height - ((value - min) / span) * (height - 4) - 2;
      return (index === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
    })
    .join(" ");
  const last = values[values.length - 1];
  const tone = last >= 0.6 ? "decline" : last >= 0.4 ? "review" : "approve";
  return (
    <svg className="sparkline" width={width} height={height} role="img"
         aria-label={"Risk trend, latest " + last.toFixed(2)}>
      <path d={path} fill="none" stroke={BAND_COLOR[tone]} strokeWidth="1.5" />
    </svg>
  );
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}
