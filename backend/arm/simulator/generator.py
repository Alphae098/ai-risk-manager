"""Synthetic payment-stream generator.

Produces a Razorpay-shaped dataset: merchants, customers, transactions across
UPI / card / netbanking / wallet, plus delayed outcomes (chargebacks, refunds).

The generator deliberately produces *overlapping* distributions. Legitimate
traffic contains sale-day spikes, travelling customers and first-time
high-ticket buyers, so a model cannot separate the classes cleanly. That keeps
the evaluation numbers honest.
"""
from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..db import init_db, insert_many, session

CATEGORIES = [
    "electronics", "fashion", "grocery", "travel", "gaming",
    "edtech", "digital_goods", "food_delivery", "jewellery", "crypto_adjacent",
]
# Categories that genuinely carry more fraud in the real world.
HIGH_RISK = {"gaming", "digital_goods", "crypto_adjacent", "jewellery", "travel"}
CITIES = ["Mumbai", "Bengaluru", "Delhi", "Hyderabad", "Pune", "Chennai", "Kolkata", "Jaipur"]
METHODS = ["upi", "card", "netbanking", "wallet"]
METHOD_WEIGHTS = [0.55, 0.28, 0.09, 0.08]

FRAUD_PATTERNS = [
    "card_testing", "account_takeover", "merchant_bustout",
    "refund_abuse", "velocity_ring", "friendly_fraud",
]


def _rid(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex[:12]


@dataclass
class SimConfig:
    days: int = 60
    n_merchants: int = 120
    n_customers: int = 4000
    txns_per_day: int = 900
    fraud_rate: float = 0.018          # share of transactions that are fraudulent
    seed: int = 7
    start: datetime = datetime(2026, 5, 1)


class Simulator:
    def __init__(self, cfg: SimConfig | None = None):
        self.cfg = cfg or SimConfig()
        self.rng = random.Random(self.cfg.seed)
        self.merchants: list[dict] = []
        self.customers: list[dict] = []
        self.transactions: list[dict] = []
        self.outcomes: list[dict] = []
        # Merchants selected to run a bust-out, mapped to the day it starts.
        self.bustout: dict[str, int] = {}

    def _rand_ip(self, first_octet: int) -> str:
        return "%d.%d.%d.%d" % (first_octet, self.rng.randint(0, 255),
                                self.rng.randint(0, 255), self.rng.randint(1, 254))

    # ---------------------------------------------------------------- entities

    def _make_merchants(self) -> None:
        for i in range(self.cfg.n_merchants):
            cat = self.rng.choice(CATEGORIES)
            onboarded = self.cfg.start - timedelta(days=self.rng.randint(5, 900))
            self.merchants.append({
                "id": _rid("mch"),
                "name": cat.replace("_", " ").title() + " Store " + str(i + 1),
                "category": cat,
                "onboarded_at": onboarded.isoformat(),
                "monthly_volume": round(self.rng.lognormvariate(13.0, 1.0), 2),
                "risk_tier": "elevated" if cat in HIGH_RISK else "standard",
                "reserve_pct": 0.0,
                "txn_limit": 200000.0,
                "frozen": 0,
            })
        # A couple of young, high-risk merchants will ramp volume then bust out.
        young = [m for m in self.merchants
                 if m["category"] in HIGH_RISK
                 and (self.cfg.start - datetime.fromisoformat(m["onboarded_at"])).days < 200]
        if not young:
            young = self.merchants[:3]
        for m in self.rng.sample(young, k=min(2, len(young))):
            self.bustout[m["id"]] = self.rng.randint(int(self.cfg.days * 0.35),
                                                     int(self.cfg.days * 0.6))

    def _make_customers(self) -> None:
        for _ in range(self.cfg.n_customers):
            self.customers.append({
                "id": _rid("cus"),
                "first_seen": (self.cfg.start - timedelta(days=self.rng.randint(0, 700))).isoformat(),
                "home_city": self.rng.choice(CITIES),
                "typical_ticket": round(max(80.0, self.rng.lognormvariate(6.6, 0.7)), 2),
            })
        # Stable device, IP and card per customer; fraud is what breaks the pattern.
        self.device_of = {c["id"]: _rid("dev") for c in self.customers}
        self.ip_of = {c["id"]: self._rand_ip(49) for c in self.customers}
        self.card_of = {c["id"]: _rid("card") for c in self.customers}

    # ------------------------------------------------------------ transactions

    def _txn(self, ts: datetime, merchant: dict, customer: dict, **over) -> dict:
        base = {
            "id": _rid("txn"),
            "ts": ts.isoformat(),
            "merchant_id": merchant["id"],
            "customer_id": customer["id"],
            "amount": round(max(20.0, self.rng.gauss(customer["typical_ticket"],
                                                     customer["typical_ticket"] * 0.4)), 2),
            "method": self.rng.choices(METHODS, METHOD_WEIGHTS)[0],
            "device_id": self.device_of[customer["id"]],
            "ip": self.ip_of[customer["id"]],
            "card_fp": self.card_of[customer["id"]],
            "bin_country": "IN",
            "ip_country": "IN",
            "city": customer["home_city"],
            "is_fraud": 0,
            "fraud_pattern": None,
        }
        base.update(over)
        return base

    def _legit_day(self, day: int, date: datetime, count: int) -> None:
        # Sale days lift volume and ticket size: the classic false-positive trap.
        sale_day = day % 17 == 0
        count = int(count * (2.6 if sale_day else 1.0))
        for _ in range(count):
            merchant = self.rng.choice(self.merchants)
            customer = self.rng.choice(self.customers)
            ts = date + timedelta(seconds=self.rng.randint(0, 86399))
            over: dict = {}
            if sale_day:
                over["amount"] = round(max(50.0, self.rng.gauss(
                    customer["typical_ticket"] * 1.8, customer["typical_ticket"] * 0.6)), 2)
            roll = self.rng.random()
            if roll < 0.02:
                # Travelling customer: new city and IP, same device. Resembles ATO.
                over["city"] = self.rng.choice([c for c in CITIES if c != customer["home_city"]])
                over["ip"] = self._rand_ip(103)
            elif roll < 0.03:
                # First-time high-ticket purchase. Resembles a bust-out victim.
                over["amount"] = round(customer["typical_ticket"] * self.rng.uniform(6, 12), 2)
            self.transactions.append(self._txn(ts, merchant, customer, **over))

    # ------------------------------------------------------------ fraud inject

    def _card_testing(self, date: datetime) -> None:
        """Many small attempts from one device against one merchant, ramping up."""
        pool = [m for m in self.merchants if m["category"] in HIGH_RISK] or self.merchants
        merchant = self.rng.choice(pool)
        device = _rid("dev")
        ip = self._rand_ip(185)
        start = date + timedelta(seconds=self.rng.randint(0, 80000))
        for i in range(self.rng.randint(8, 22)):
            customer = self.rng.choice(self.customers)
            self.transactions.append(self._txn(
                start + timedelta(seconds=i * self.rng.randint(4, 25)), merchant, customer,
                amount=round(self.rng.uniform(1, 30) + i * 4, 2),
                method="card", device_id=device, ip=ip, card_fp=_rid("card"),
                bin_country=self.rng.choice(["IN", "US", "SG"]), ip_country="RO",
                is_fraud=1, fraud_pattern="card_testing",
            ))

    def _account_takeover(self, date: datetime) -> None:
        """Established customer, new device and city, ticket far above baseline."""
        customer = self.rng.choice(self.customers)
        merchant = self.rng.choice(self.merchants)
        device = _rid("dev")
        ip = self._rand_ip(45)
        for i in range(self.rng.randint(1, 4)):
            self.transactions.append(self._txn(
                date + timedelta(seconds=self.rng.randint(0, 86000) + i * 400), merchant, customer,
                amount=round(customer["typical_ticket"] * self.rng.uniform(5, 12), 2),
                device_id=device, ip=ip,
                city=self.rng.choice([c for c in CITIES if c != customer["home_city"]]),
                ip_country=self.rng.choice(["IN", "NG", "VN"]),
                is_fraud=1, fraud_pattern="account_takeover",
            ))

    def _velocity_ring(self, date: datetime) -> None:
        """Distinct customers sharing one device fingerprint and IP subnet."""
        merchant = self.rng.choice(self.merchants)
        device = _rid("dev")
        subnet = "91.%d.%d" % (self.rng.randint(0, 255), self.rng.randint(0, 255))
        for _ in range(self.rng.randint(6, 14)):
            customer = self.rng.choice(self.customers)
            self.transactions.append(self._txn(
                date + timedelta(seconds=self.rng.randint(0, 86000)), merchant, customer,
                amount=round(self.rng.uniform(800, 6000), 2),
                device_id=device, ip=subnet + "." + str(self.rng.randint(1, 254)),
                ip_country="IN", is_fraud=1, fraud_pattern="velocity_ring",
            ))

    def _refund_abuse(self, date: datetime) -> None:
        """Buy-then-refund cycles on the same customer and merchant pair."""
        customer = self.rng.choice(self.customers)
        merchant = self.rng.choice(self.merchants)
        for i in range(self.rng.randint(2, 5)):
            txn = self._txn(
                date + timedelta(hours=i * 3), merchant, customer,
                amount=round(self.rng.uniform(3000, 25000), 2),
                is_fraud=1, fraud_pattern="refund_abuse",
            )
            self.transactions.append(txn)
            arrived = datetime.fromisoformat(txn["ts"]) + timedelta(days=self.rng.randint(1, 4))
            self.outcomes.append({
                "txn_id": txn["id"], "kind": "refund", "arrived_at": arrived.isoformat(),
                "analyst_override": None, "analyst_note": None,
            })

    def _friendly_fraud(self, date: datetime) -> None:
        """Indistinguishable at authorization; only the late chargeback reveals it."""
        customer = self.rng.choice(self.customers)
        merchant = self.rng.choice(self.merchants)
        self.transactions.append(self._txn(
            date + timedelta(seconds=self.rng.randint(0, 86000)), merchant, customer,
            amount=round(customer["typical_ticket"] * self.rng.uniform(1.0, 2.2), 2),
            is_fraud=1, fraud_pattern="friendly_fraud",
        ))

    def _bustout_day(self, day: int, date: datetime) -> None:
        """Merchant volume ramps hard, then chargebacks land after settlement.

        Transaction-level features see little here: each payment looks ordinary.
        Only the merchant's trajectory gives it away.
        """
        for mid, start_day in self.bustout.items():
            if day < start_day:
                continue
            merchant = next(m for m in self.merchants if m["id"] == mid)
            ramp = min(1.0 + (day - start_day) * 0.35, 4.0)
            for _ in range(int(2 * ramp)):
                customer = self.rng.choice(self.customers)
                fraudulent = day >= start_day + 2
                self.transactions.append(self._txn(
                    date + timedelta(seconds=self.rng.randint(0, 86000)), merchant, customer,
                    amount=round(self.rng.uniform(1500, 18000), 2),
                    is_fraud=1 if fraudulent else 0,
                    fraud_pattern="merchant_bustout" if fraudulent else None,
                ))

    # -------------------------------------------------------------- outcomes

    def _settle_outcomes(self) -> None:
        """Attach delayed chargebacks to fraud and a thin share of legit traffic."""
        already = {o["txn_id"] for o in self.outcomes}
        for txn in self.transactions:
            if txn["id"] in already:
                continue
            ts = datetime.fromisoformat(txn["ts"])
            if txn["is_fraud"]:
                # Not all fraud is disputed; some is simply absorbed.
                if self.rng.random() < 0.82:
                    delay = (self.rng.randint(20, 45)
                             if txn["fraud_pattern"] == "friendly_fraud"
                             else self.rng.randint(5, 30))
                    kind, arrived = "chargeback", ts + timedelta(days=delay)
                else:
                    kind, arrived = "clean", ts + timedelta(days=45)
            else:
                # A thin band of genuine disputes on legitimate payments.
                if self.rng.random() < 0.0012:
                    kind, arrived = "chargeback", ts + timedelta(days=self.rng.randint(10, 45))
                else:
                    kind, arrived = "clean", ts + timedelta(days=45)
            self.outcomes.append({
                "txn_id": txn["id"], "kind": kind, "arrived_at": arrived.isoformat(),
                "analyst_override": None, "analyst_note": None,
            })

    # ------------------------------------------------------------------- run

    def run(self) -> dict[str, int]:
        self._make_merchants()
        self._make_customers()
        # Fraud is injected event by event until the day hits its target count.
        # Events differ wildly in size (a card-testing burst is ~15 transactions,
        # a friendly-fraud case is one), so counting transactions rather than
        # events is what keeps the overall fraud rate at the configured level.
        daily_target = max(1, int(self.cfg.txns_per_day * self.cfg.fraud_rate))
        injectors = [
            (self._card_testing, 1),
            (self._account_takeover, 3),
            (self._friendly_fraud, 3),
            (self._velocity_ring, 1),
            (self._refund_abuse, 2),
        ]
        weights = [w for _, w in injectors]

        for day in range(self.cfg.days):
            date = self.cfg.start + timedelta(days=day)
            self._legit_day(day, date, self.cfg.txns_per_day)
            self._bustout_day(day, date)
            # Bust-out volume is deliberately excluded from the daily quota.
            # It is structural fraud on a specific merchant, and letting it
            # absorb the quota would silently starve every other pattern out of
            # the later part of the period.
            injected = 0
            guard = 0
            while injected < daily_target and guard < 200:
                guard += 1
                before = len(self.transactions)
                self.rng.choices(injectors, weights)[0][0](date)
                injected += sum(1 for t in self.transactions[before:] if t["is_fraud"])

        self.transactions.sort(key=lambda t: t["ts"])
        self._settle_outcomes()
        return {
            "merchants": len(self.merchants),
            "customers": len(self.customers),
            "transactions": len(self.transactions),
            "fraud": sum(t["is_fraud"] for t in self.transactions),
            "outcomes": len(self.outcomes),
        }

    def persist(self) -> int:
        with session() as conn:
            init_db(conn)
            for table in ("outcomes", "merchant_risk_daily", "agent_cases",
                          "decisions", "transactions", "customers", "merchants"):
                conn.execute("DELETE FROM " + table)
            insert_many(conn, "merchants", self.merchants)
            insert_many(conn, "customers", self.customers)
            for i in range(0, len(self.transactions), 2000):
                insert_many(conn, "transactions", self.transactions[i:i + 2000])
            for i in range(0, len(self.outcomes), 2000):
                insert_many(conn, "outcomes", self.outcomes[i:i + 2000])
        return len(self.transactions)


def generate(cfg: SimConfig | None = None) -> dict[str, int]:
    sim = Simulator(cfg)
    stats = sim.run()
    sim.persist()
    return stats


if __name__ == "__main__":
    print(generate())
