"""A tiny in-memory store: orders, plus a record of every action ShopBot takes on them."""

from __future__ import annotations

import itertools
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("policy.md")
POLICY_TOPICS = ("returns", "damaged_items", "late_delivery", "warranty", "cancellations", "final_sale",
                 "refunds_over_500", "escalation")


def load_policy() -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    for line in POLICY_PATH.read_text().splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    return {k: v.strip() for k, v in sections.items()}


@dataclass
class Item:
    sku: str
    name: str
    price: float
    category: str  # electronics | apparel | home | outdoor
    final_sale: bool = False


@dataclass
class Order:
    order_id: str
    email: str
    customer_name: str
    items: list[Item]
    status: str  # processing | shipped | delivered
    ordered_on: str
    promised_by: str
    shipped_on: str | None = None
    delivered_on: str | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    actions: list[dict] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(i.price for i in self.items), 2)

    def view(self, today: date) -> dict:
        d = asdict(self)
        d.pop("actions")
        d["order_total"] = self.total
        if self.delivered_on:
            delivered = date.fromisoformat(self.delivered_on)
            d["delivered_days_ago"] = (today - delivered).days
            d["days_late"] = max(0, (delivered - date.fromisoformat(self.promised_by)).days)
        else:
            d["days_late"] = max(0, (today - date.fromisoformat(self.promised_by)).days)
        return d


ORDER_ID = re.compile(r"\b[A-Z]\d{4,6}\b")


class Store:
    def __init__(self, today: date | None = None):
        self._orders: dict[str, Order] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1001)
        self.today = today or date.today()
        self.policy = load_policy()

    def add(self, order: Order) -> None:
        with self._lock:
            self._orders[order.order_id.upper()] = order

    def add_dict(self, raw: dict) -> Order:
        raw = dict(raw)
        raw["items"] = [Item(**i) for i in raw["items"]]
        raw.pop("actions", None)
        order = Order(**raw)
        self.add(order)
        return order

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id.strip().upper().lstrip("#"))

    def by_email(self, email: str) -> list[Order]:
        e = email.strip().lower()
        return [o for o in self._orders.values() if o.email.lower() == e]

    def record(self, order: Order | None, action: dict) -> str:
        with self._lock:
            ref = f"{action['type'][:1].upper()}-{next(self._ids)}"
            if order is not None:
                order.actions.append({**action, "ref": ref})
        return ref

    def __len__(self) -> int:
        return len(self._orders)


def demo_orders(today: date) -> list[Order]:
    """Orders for the human chat page, each showing a different policy situation."""
    d = lambda days: (today - timedelta(days=days)).isoformat()
    return [
        Order("A1001", "maya@example.com", "Maya", [Item("AP-204", "Trail running jacket", 89.0, "apparel")],
              "delivered", d(16), d(11), d(14), d(12), "UPS", "1Z999AA10123456784"),
        Order("A1002", "maya@example.com", "Maya", [Item("EL-310", "Noise-cancelling headphones", 249.0, "electronics")],
              "delivered", d(140), d(133), d(136), d(134), "FedEx", "7712 3456 7890"),
        Order("A1003", "sam@example.com", "Sam", [Item("HM-118", "Ceramic pour-over set", 42.0, "home")],
              "delivered", d(9), d(5), d(6), d(4), "USPS", "9400 1000 0000 0000 0001"),
        Order("A1004", "sam@example.com", "Sam", [Item("OD-501", "Two-person tent", 189.0, "outdoor")],
              "shipped", d(14), d(6), d(10), None, "UPS", "1Z999AA10123456791"),
        Order("A1005", "lee@example.com", "Lee", [Item("EL-990", "4K action camera", 649.0, "electronics")],
              "delivered", d(8), d(4), d(5), d(3), "FedEx", "7712 3456 0001"),
        Order("A1006", "lee@example.com", "Lee", [Item("CL-077", "Clearance fleece (final sale)", 25.0, "apparel", True)],
              "delivered", d(12), d(8), d(9), d(7), "USPS", "9400 1000 0000 0000 0077"),
    ]
