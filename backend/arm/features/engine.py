"""Streaming feature engine.

One implementation serves both training and serving. Training replays the
historical stream through this engine in timestamp order; the API keeps a warm
instance and feeds it live transactions. Because both paths run the same code,
there is no train/serve skew to debug later.

State is a set of bounded deques keyed by entity (customer, device, IP, card,
merchant). Every deque is trimmed to the longest window any feature needs, so
memory stays proportional to recent traffic rather than total history.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Deque, Iterable

MAX_WINDOW = timedelta(days=7)

FEATURE_NAMES = [
    "amount",
    "log_amount",
    "hour",
    "is_night",
    "amount_over_customer_typical",
    "customer_txn_1h",
    "customer_txn_24h",
    "customer_seconds_since_last",
    "customer_distinct_devices_7d",
    "customer_distinct_cities_7d",
    "customer_is_new_city",
    "customer_age_days",
    "device_txn_1m",
    "device_txn_1h",
    "device_txn_24h",
    "device_distinct_customers_24h",
    "device_distinct_cards_24h",
    "device_is_new",
    "ip_txn_1h",
    "ip_txn_24h",
    "ip_distinct_customers_24h",
    "subnet_distinct_customers_24h",
    "card_txn_24h",
    "card_distinct_merchants_24h",
    "merchant_txn_1h",
    "merchant_txn_24h",
    "merchant_amount_zscore",
    "merchant_volume_ratio_7d",
    "merchant_age_days",
    "merchant_is_high_risk_category",
    "geo_bin_ip_mismatch",
    "geo_ip_offshore",
    "method_upi",
    "method_card",
    "method_netbanking",
    "method_wallet",
]

HIGH_RISK_CATEGORIES = {"gaming", "digital_goods", "crypto_adjacent", "jewellery", "travel"}


@dataclass
class _Event:
    ts: datetime
    amount: float
    customer_id: str
    device_id: str
    card_fp: str
    merchant_id: str
    ip: str
    city: str


def _subnet(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else ip


def _count_within(events: Deque[_Event], now: datetime, window: timedelta) -> int:
    cutoff = now - window
    return sum(1 for e in events if e.ts >= cutoff)


def _distinct_within(events: Deque[_Event], now: datetime, window: timedelta, attr: str) -> int:
    cutoff = now - window
    return len({getattr(e, attr) for e in events if e.ts >= cutoff})


class FeatureEngine:
    """Computes features for a transaction, then absorbs it into rolling state."""

    def __init__(self, merchants: dict[str, dict] | None = None,
                 customers: dict[str, dict] | None = None):
        self.merchants = merchants or {}
        self.customers = customers or {}
        self._by_customer: dict[str, Deque[_Event]] = defaultdict(deque)
        self._by_device: dict[str, Deque[_Event]] = defaultdict(deque)
        self._by_ip: dict[str, Deque[_Event]] = defaultdict(deque)
        self._by_subnet: dict[str, Deque[_Event]] = defaultdict(deque)
        self._by_card: dict[str, Deque[_Event]] = defaultdict(deque)
        self._by_merchant: dict[str, Deque[_Event]] = defaultdict(deque)
        # Running mean/variance of merchant ticket size, for the z-score feature.
        self._merchant_stats: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    # ------------------------------------------------------------------ state

    def _trim(self, events: Deque[_Event], now: datetime) -> None:
        cutoff = now - MAX_WINDOW
        while events and events[0].ts < cutoff:
            events.popleft()

    def _absorb(self, event: _Event) -> None:
        for store, key in (
            (self._by_customer, event.customer_id),
            (self._by_device, event.device_id),
            (self._by_ip, event.ip),
            (self._by_subnet, _subnet(event.ip)),
            (self._by_card, event.card_fp),
            (self._by_merchant, event.merchant_id),
        ):
            bucket = store[key]
            bucket.append(event)
            self._trim(bucket, event.ts)
        stats = self._merchant_stats[event.merchant_id]
        stats[0] += 1
        delta = event.amount - stats[1]
        stats[1] += delta / stats[0]
        stats[2] += delta * (event.amount - stats[1])

    # --------------------------------------------------------------- features

    def compute(self, txn: dict[str, Any], absorb: bool = True) -> dict[str, float]:
        ts = txn["ts"] if isinstance(txn["ts"], datetime) else datetime.fromisoformat(txn["ts"])
        amount = float(txn["amount"])
        customer = self.customers.get(txn["customer_id"], {})
        merchant = self.merchants.get(txn["merchant_id"], {})

        cust_events = self._by_customer[txn["customer_id"]]
        dev_events = self._by_device[txn["device_id"]]
        ip_events = self._by_ip[txn["ip"]]
        subnet_events = self._by_subnet[_subnet(txn["ip"])]
        card_events = self._by_card[txn.get("card_fp") or "none"]
        mch_events = self._by_merchant[txn["merchant_id"]]

        typical = float(customer.get("typical_ticket") or amount or 1.0)
        seconds_since_last = (
            (ts - cust_events[-1].ts).total_seconds() if cust_events else 86400.0 * 30
        )
        recent_cities = {e.city for e in cust_events if e.ts >= ts - timedelta(days=7)}
        home_city = customer.get("home_city")
        is_new_city = float(
            bool(txn.get("city"))
            and txn["city"] != home_city
            and txn["city"] not in recent_cities
        )

        count, mean, m2 = self._merchant_stats[txn["merchant_id"]]
        std = (m2 / count) ** 0.5 if count > 1 else 0.0
        merchant_z = (amount - mean) / std if std > 1e-6 else 0.0

        this_week = _count_within(mch_events, ts, timedelta(days=1))
        prior_week = max(1, (_count_within(mch_events, ts, timedelta(days=7)) - this_week) / 6.0)
        volume_ratio = this_week / prior_week

        first_seen = customer.get("first_seen")
        customer_age = (
            (ts - datetime.fromisoformat(first_seen)).days if first_seen else 0.0
        )
        onboarded = merchant.get("onboarded_at")
        merchant_age = (ts - datetime.fromisoformat(onboarded)).days if onboarded else 0.0

        method = txn.get("method", "")
        features = {
            "amount": amount,
            "log_amount": float(math.log1p(amount)),
            "hour": float(ts.hour),
            "is_night": float(ts.hour < 6 or ts.hour >= 23),
            "amount_over_customer_typical": amount / typical,
            "customer_txn_1h": float(_count_within(cust_events, ts, timedelta(hours=1))),
            "customer_txn_24h": float(_count_within(cust_events, ts, timedelta(days=1))),
            "customer_seconds_since_last": float(min(seconds_since_last, 86400.0 * 30)),
            "customer_distinct_devices_7d": float(
                _distinct_within(cust_events, ts, timedelta(days=7), "device_id")),
            "customer_distinct_cities_7d": float(len(recent_cities)),
            "customer_is_new_city": is_new_city,
            "customer_age_days": float(customer_age),
            "device_txn_1m": float(_count_within(dev_events, ts, timedelta(minutes=1))),
            "device_txn_1h": float(_count_within(dev_events, ts, timedelta(hours=1))),
            "device_txn_24h": float(_count_within(dev_events, ts, timedelta(days=1))),
            "device_distinct_customers_24h": float(
                _distinct_within(dev_events, ts, timedelta(days=1), "customer_id")),
            "device_distinct_cards_24h": float(
                _distinct_within(dev_events, ts, timedelta(days=1), "card_fp")),
            "device_is_new": float(not dev_events),
            "ip_txn_1h": float(_count_within(ip_events, ts, timedelta(hours=1))),
            "ip_txn_24h": float(_count_within(ip_events, ts, timedelta(days=1))),
            "ip_distinct_customers_24h": float(
                _distinct_within(ip_events, ts, timedelta(days=1), "customer_id")),
            "subnet_distinct_customers_24h": float(
                _distinct_within(subnet_events, ts, timedelta(days=1), "customer_id")),
            "card_txn_24h": float(_count_within(card_events, ts, timedelta(days=1))),
            "card_distinct_merchants_24h": float(
                _distinct_within(card_events, ts, timedelta(days=1), "merchant_id")),
            "merchant_txn_1h": float(_count_within(mch_events, ts, timedelta(hours=1))),
            "merchant_txn_24h": float(this_week),
            "merchant_amount_zscore": float(merchant_z),
            "merchant_volume_ratio_7d": float(volume_ratio),
            "merchant_age_days": float(merchant_age),
            "merchant_is_high_risk_category": float(
                merchant.get("category") in HIGH_RISK_CATEGORIES),
            "geo_bin_ip_mismatch": float(
                bool(txn.get("bin_country")) and txn.get("bin_country") != txn.get("ip_country")),
            "geo_ip_offshore": float(txn.get("ip_country") not in (None, "IN")),
            "method_upi": float(method == "upi"),
            "method_card": float(method == "card"),
            "method_netbanking": float(method == "netbanking"),
            "method_wallet": float(method == "wallet"),
        }

        if absorb:
            self._absorb(_Event(
                ts=ts, amount=amount,
                customer_id=txn["customer_id"], device_id=txn["device_id"],
                card_fp=txn.get("card_fp") or "none", merchant_id=txn["merchant_id"],
                ip=txn["ip"], city=txn.get("city") or "",
            ))
        return features

    def replay(self, transactions: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
        """Compute features for a time-ordered stream, absorbing as it goes."""
        return [self.compute(txn) for txn in transactions]


def vectorize(features: dict[str, float]) -> list[float]:
    """Fixed-order feature vector. The order is the model's contract."""
    return [features[name] for name in FEATURE_NAMES]
