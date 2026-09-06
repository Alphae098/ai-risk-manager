const base = "";

async function request(path, options) {
  const response = await fetch(base + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || response.statusText);
  }
  return response.json();
}

export const api = {
  health: () => request("/api/health"),
  overview: () => request("/api/metrics/overview"),
  evaluation: () => request("/api/evaluation"),
  transactions: (params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request("/api/transactions" + (query ? "?" + query : ""));
  },
  cases: (params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request("/api/cases" + (query ? "?" + query : ""));
  },
  reviewNow: (txnId) => request(`/api/cases/${txnId}/review`, { method: "POST" }),
  override: (txnId, action, note = "") =>
    request(`/api/cases/${txnId}/override`, {
      method: "POST",
      body: JSON.stringify({ action, note }),
    }),
  merchants: (params = {}) => {
    const query = new URLSearchParams(params).toString();
    return request("/api/merchants" + (query ? "?" + query : ""));
  },
  merchant: (id) => request(`/api/merchants/${id}`),
  freeze: (id, frozen) =>
    request(`/api/merchants/${id}/freeze?frozen=${frozen}`, { method: "POST" }),
  rules: () => request("/api/rules"),
  proposedRules: () => request("/api/proposed-rules"),
  decideRule: (id, approve) =>
    request(`/api/proposed-rules/${id}/decide?approve=${approve}`, { method: "POST" }),
  score: (payload) =>
    request("/api/score", { method: "POST", body: JSON.stringify(payload) }),
};

export function openStream(onMessage) {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/stream`);
  socket.onmessage = (event) => onMessage(JSON.parse(event.data));
  return socket;
}

export const inr = (value) =>
  "\u20b9" + Math.round(Number(value || 0)).toLocaleString("en-IN");

export const pct = (value, digits = 1) =>
  (Number(value || 0) * 100).toFixed(digits) + "%";

export const clock = (iso) =>
  new Date(iso).toLocaleTimeString("en-IN", { hour12: false });

export const day = (iso) =>
  new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
