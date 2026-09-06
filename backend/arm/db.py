"""SQLite access layer.

Plain sqlite3 on purpose: the schema is small, the queries are explicit, and
the DDL ports to Postgres with no rewriting beyond the autoincrement clause.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    onboarded_at TEXT NOT NULL,
    monthly_volume REAL NOT NULL,
    risk_tier TEXT NOT NULL DEFAULT 'standard',
    reserve_pct REAL NOT NULL DEFAULT 0.0,
    txn_limit REAL NOT NULL DEFAULT 200000,
    frozen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    home_city TEXT NOT NULL,
    typical_ticket REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    merchant_id TEXT NOT NULL REFERENCES merchants(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    device_id TEXT NOT NULL,
    ip TEXT NOT NULL,
    card_fp TEXT,
    bin_country TEXT,
    ip_country TEXT,
    city TEXT,
    is_fraud INTEGER NOT NULL,
    fraud_pattern TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_ts ON transactions(ts);
CREATE INDEX IF NOT EXISTS ix_txn_merchant ON transactions(merchant_id, ts);
CREATE INDEX IF NOT EXISTS ix_txn_customer ON transactions(customer_id, ts);
CREATE INDEX IF NOT EXISTS ix_txn_device ON transactions(device_id, ts);
CREATE INDEX IF NOT EXISTS ix_txn_ip ON transactions(ip, ts);

CREATE TABLE IF NOT EXISTS decisions (
    txn_id TEXT PRIMARY KEY REFERENCES transactions(id),
    ts TEXT NOT NULL,
    rule_hits TEXT NOT NULL,
    rule_boost REAL NOT NULL DEFAULT 0.0,
    model_score REAL NOT NULL,
    final_score REAL NOT NULL,
    band TEXT NOT NULL,
    action TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    features TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dec_band ON decisions(band, ts);

CREATE TABLE IF NOT EXISTS agent_cases (
    id TEXT PRIMARY KEY,
    txn_id TEXT NOT NULL REFERENCES transactions(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    verdict TEXT,
    confidence REAL,
    rationale TEXT,
    evidence TEXT,
    recommended_action TEXT,
    suggested_rule TEXT,
    tool_calls TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    llm_source TEXT
);
CREATE INDEX IF NOT EXISTS ix_case_txn ON agent_cases(txn_id);
CREATE INDEX IF NOT EXISTS ix_case_status ON agent_cases(status, created_at);

CREATE TABLE IF NOT EXISTS outcomes (
    txn_id TEXT PRIMARY KEY REFERENCES transactions(id),
    kind TEXT NOT NULL,
    arrived_at TEXT NOT NULL,
    analyst_override TEXT,
    analyst_note TEXT
);

CREATE TABLE IF NOT EXISTS merchant_risk_daily (
    merchant_id TEXT NOT NULL REFERENCES merchants(id),
    date TEXT NOT NULL,
    risk_score REAL NOT NULL,
    cb_ratio REAL NOT NULL,
    volume REAL NOT NULL,
    volume_delta REAL NOT NULL,
    fraud_rate REAL NOT NULL,
    recommended_reserve_pct REAL NOT NULL,
    recommended_txn_limit REAL NOT NULL,
    memo TEXT,
    PRIMARY KEY (merchant_id, date)
);

CREATE TABLE IF NOT EXISTS rule_stats (
    rule_id TEXT PRIMARY KEY,
    hits INTEGER NOT NULL DEFAULT 0,
    true_positives INTEGER NOT NULL DEFAULT 0,
    false_positives INTEGER NOT NULL DEFAULT 0,
    last_hit_at TEXT
);

CREATE TABLE IF NOT EXISTS proposed_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source_case_id TEXT,
    description TEXT NOT NULL,
    condition TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def session(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_many(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def jdump(value: Any) -> str:
    return json.dumps(value, default=str)


def jload(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
