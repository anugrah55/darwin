"""Simulated customers for ShopBot.

Each scenario is a realistic support situation with a hidden goal and a deterministic success rule over
what ShopBot actually did (its tool calls) and said. The rules are never shown to the Coach, and every
case is re-randomised (names, order IDs, dates, amounts, phrasing), so a better prompt has to generalise
instead of learning the test.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

FIRST_NAMES = ["Maya", "Arjun", "Lena", "Tomás", "Aisha", "Wei", "Priya", "Jonas", "Chloe", "Ravi", "Sofia", "Kenji",
               "Nadia", "Omar", "Grace", "Mateo", "Ines", "Dev", "Hana", "Leo"]
DOMAINS = ["example.com", "mail.example.org", "inbox.example.net"]

ITEMS = {
    "apparel": [("Trail running jacket", 89), ("Merino base layer", 64), ("Rain shell", 129), ("Hiking socks 3-pack", 24)],
    "outdoor": [("Two-person tent", 189), ("Trekking poles", 79), ("Camp stove", 59), ("Down sleeping bag", 229)],
    "home": [("Ceramic pour-over set", 42), ("Wool throw blanket", 98), ("Cast-iron skillet", 54)],
    "electronics": [("Noise-cancelling headphones", 249), ("GPS watch", 329), ("4K action camera", 649), ("Solar power bank", 89)],
}
HIGH_VALUE = [("Carbon gravel bike", 1290), ("Pro camera drone", 899), ("4K action camera bundle", 749), ("Ultralight tent system", 619)]
CARRIERS = ["UPS", "FedEx", "USPS", "DHL"]


@dataclass
class Case:
    scenario: str
    name: str
    email: str
    orders: list[dict]
    opening: str
    order_id: str | None  # the order the customer is talking about (None: they don't know it)
    knows_order_id: bool = True
    preference: str = "Yes, please go ahead."  # what they say when offered a choice
    replies: list[tuple[str, str]] = field(default_factory=list)  # (regex on ShopBot's reply, customer answer)
    max_turns: int = 4
    check: Callable[[list[dict], list[str]], tuple[bool, str]] = lambda calls, replies: (False, "no rule")
    facts: dict = field(default_factory=dict)


def _calls(calls: list[dict], name: str, **match) -> list[dict]:
    out = []
    for c in calls:
        if c["name"] != name or c.get("is_error"):
            continue
        if all(str(c["input"].get(k, "")).upper() == str(v).upper() for k, v in match.items()):
            out.append(c)
    return out


def _said(replies: list[str], pattern: str) -> bool:
    return any(re.search(pattern, r, re.I) for r in replies)


class Factory:
    def __init__(self, rng: random.Random, today: date):
        self.rng = rng
        self.today = today

    def day(self, ago: int) -> str:
        return (self.today - timedelta(days=ago)).isoformat()

    def person(self) -> tuple[str, str]:
        name = self.rng.choice(FIRST_NAMES)
        tag = "".join(self.rng.choices(string.digits, k=3))
        return name, f"{name.lower().replace('á', 'a')}.{tag}@{self.rng.choice(DOMAINS)}"

    def order_id(self) -> str:
        return self.rng.choice("ABCDEFGH") + str(self.rng.randint(10000, 99999))

    def tracking(self) -> str:
        return "1Z" + "".join(self.rng.choices(string.ascii_uppercase + string.digits, k=14))

    def item(self, category: str | None = None, final_sale: bool = False) -> dict:
        category = category or self.rng.choice(["apparel", "outdoor", "home"])
        name, price = self.rng.choice(ITEMS[category])
        if final_sale:
            name = f"Clearance {name.lower()} (final sale)"
            price = round(price * 0.6)
        sku = ("CL" if final_sale else category[:2].upper()) + f"-{self.rng.randint(100, 999)}"
        return {"sku": sku, "name": name, "price": float(price), "category": category, "final_sale": final_sale}

    def delivered(self, email: str, name: str, *, ago: int, items: list[dict], late: int = 0) -> dict:
        ordered = ago + self.rng.randint(3, 6)
        delivered_on = self.day(ago)
        promised = self.day(ago + late) if late else self.day(ago + self.rng.randint(0, 2))
        return {"order_id": self.order_id(), "email": email, "customer_name": name, "items": items, "status": "delivered",
                "ordered_on": self.day(ordered), "promised_by": promised, "shipped_on": self.day(ago + 2),
                "delivered_on": delivered_on, "carrier": self.rng.choice(CARRIERS), "tracking_number": self.tracking()}

    def in_transit(self, email: str, name: str, *, status: str, days_late: int, items: list[dict]) -> dict:
        promised_ago = days_late
        return {"order_id": self.order_id(), "email": email, "customer_name": name, "items": items, "status": status,
                "ordered_on": self.day(promised_ago + 7), "promised_by": self.day(promised_ago),
                "shipped_on": self.day(promised_ago + 4) if status == "shipped" else None, "delivered_on": None,
                "carrier": self.rng.choice(CARRIERS) if status == "shipped" else None,
                "tracking_number": self.tracking() if status == "shipped" else None}

    def pick(self, *options: str) -> str:
        return self.rng.choice(options)


# ---------------------------------------------------------------- scenarios


def standard_refund(f: Factory) -> Case:
    name, email = f.person()
    o = f.delivered(email, name, ago=f.rng.randint(4, 24), items=[f.item()])
    item = o["items"][0]["name"].lower()
    opening = f.pick(
        f"Hi, I'd like to return the {item} from order {o['order_id']} and get my money back. It just isn't for me.",
        f"order {o['order_id']} - want to send the {item} back for a refund pls",
        f"Hello! The {item} I bought (order {o['order_id']}) doesn't fit. Can I get a refund?",
    )

    def check(calls, replies):
        if _calls(calls, "escalate_to_human"):
            return False, "escalated a routine refund"
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="refund"):
            return True, "refund created"
        return False, "no refund created"

    return Case("standard_refund", name, email, [o], opening, o["order_id"], preference="A refund, please.", check=check)


def late_window_return(f: Factory) -> Case:
    name, email = f.person()
    ago = f.rng.randint(32, 44)
    o = f.delivered(email, name, ago=ago, items=[f.item()])
    item = o["items"][0]["name"].lower()
    opening = f.pick(
        f"Hi, I want to return the {item} (order {o['order_id']}) for a refund. I've been busy and only just got round to it.",
        f"Can I still return order {o['order_id']}? Never used the {item}. I'd like a refund.",
    )

    def check(calls, replies):
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="refund"):
            return False, f"refunded on day {ago} (policy: store credit only after day 30)"
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="store_credit"):
            return True, "store-credit return created"
        if _calls(calls, "escalate_to_human"):
            return False, "escalated instead of offering store credit"
        return False, "no store-credit return created"

    return Case("late_window_return", name, email, [o], opening, o["order_id"], preference="Store credit is fine, go ahead.",
                replies=[(r"store credit", "Okay, store credit works. Please set that up.")], check=check, facts={"days": ago})


def expired_return(f: Factory) -> Case:
    name, email = f.person()
    ago = f.rng.randint(55, 120)
    o = f.delivered(email, name, ago=ago, items=[f.item(f.rng.choice(["apparel", "home", "outdoor"]))])
    item = o["items"][0]["name"].lower()
    opening = f.pick(
        f"I'd like to return the {item} from order {o['order_id']}. Haven't really used it.",
        f"Return request for order {o['order_id']} please, the {item}.",
    )

    def check(calls, replies):
        if _calls(calls, "create_return") or _calls(calls, "issue_credit"):
            return False, f"accepted a return on day {ago} (policy: no returns after 45 days)"
        if _calls(calls, "escalate_to_human"):
            return False, "escalated a simple policy answer"
        if _said(replies, r"\b45\b"):
            return True, "declined and explained the 45-day limit"
        return False, "declined without explaining the policy"

    return Case("expired_return", name, email, [o], opening, o["order_id"], preference="Hmm, okay.",
                replies=[(r"store credit|exchange", "Sure, if that's possible.")], check=check, facts={"days": ago})


def missing_order_id(f: Factory) -> Case:
    name, email = f.person()
    o = f.delivered(email, name, ago=f.rng.randint(3, 20), items=[f.item()])
    item = o["items"][0]["name"].lower()
    opening = f.pick(
        f"Hi, I want to return the {item} I bought recently and get a refund. I don't have the order number, sorry!",
        f"need to return a {item}, can't find my order confirmation anywhere",
    )

    def check(calls, replies):
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="refund"):
            return True, "found the order by email and refunded"
        if _calls(calls, "escalate_to_human"):
            return False, "escalated instead of looking the order up by email"
        if not _calls(calls, "find_orders"):
            return False, "never looked the order up by email"
        return False, "no refund created"

    return Case("missing_order_id", name, email, [o], opening, o["order_id"], knows_order_id=False,
                preference="A refund, please.", check=check)


def two_requests(f: Factory) -> Case:
    name, email = f.person()
    a = f.in_transit(email, name, status="processing", days_late=-3, items=[f.item()])
    b = f.in_transit(email, name, status="shipped", days_late=0, items=[f.item()])
    opening = f.pick(
        f"Two things: please cancel order {a['order_id']}, I ordered it by mistake. And where is order {b['order_id']}?",
        f"Hi - can you cancel {a['order_id']}? Also what's the tracking for {b['order_id']}? Thanks",
    )

    def check(calls, replies):
        cancelled = bool(_calls(calls, "cancel_order", order_id=a["order_id"]))
        tracked = _said(replies, re.escape(b["tracking_number"]))
        if cancelled and tracked:
            return True, "cancelled one order and gave tracking for the other"
        return False, ("cancelled but no tracking number" if cancelled else
                       "gave tracking but didn't cancel" if tracked else "handled neither request")

    return Case("two_requests", name, email, [a, b], opening, a["order_id"], check=check)


def damaged_item(f: Factory) -> Case:
    name, email = f.person()
    o = f.delivered(email, name, ago=f.rng.randint(1, 9), items=[f.item(f.rng.choice(["home", "outdoor"]))])
    item = o["items"][0]["name"].lower()
    opening = f.pick(
        f"My {item} (order {o['order_id']}) arrived broken, the box was crushed. Can you send me a new one?",
        f"order {o['order_id']} came damaged :( the {item} is cracked. I'd like a replacement",
    )

    def check(calls, replies):
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="replacement"):
            return True, "replacement created"
        if _said(replies, r"photo|picture|image|e-?mail (us|our|the)"):
            return False, "asked for photos or an email instead of replacing it"
        if _calls(calls, "escalate_to_human"):
            return False, "escalated a damaged item"
        return False, "no replacement created"

    return Case("damaged_item", name, email, [o], opening, o["order_id"], preference="A replacement, please.",
                replies=[(r"photo|picture|image", "I can't take photos right now, but it's clearly broken. Can you just send a new one?")],
                check=check)


def angry_late(f: Factory) -> Case:
    name, email = f.person()
    late = f.rng.choice([6, 7, 8, 9, 12, 14])
    o = f.in_transit(email, name, status="shipped", days_late=late, items=[f.item("outdoor")])
    credit = 10 if late <= 10 else 25
    opening = f.pick(
        f"This is ridiculous. Order {o['order_id']} was supposed to arrive over a week ago. Get me a human NOW.",
        f"I want to speak to a real person. My order {o['order_id']} is super late and nobody tells me anything.",
    )

    def check(calls, replies):
        if _calls(calls, "escalate_to_human"):
            return False, "escalated on the first request for a human (policy: offer to help first)"
        credits = _calls(calls, "issue_credit", order_id=o["order_id"])
        if not credits:
            return False, f"no late-delivery credit (policy: ${credit} at {late} days late)"
        amount = float(credits[-1]["input"].get("amount_usd", 0))
        if abs(amount - credit) > 0.01:
            return False, f"issued ${amount:g} instead of ${credit}"
        return True, f"issued the ${credit} credit without escalating"

    return Case("angry_late", name, email, [o], opening, o["order_id"], preference="Fine. Yes, do that.",
                replies=[(r"human|specialist|agent|transfer|colleague", "Okay, fine, if you can actually fix it then do it.")],
                check=check, facts={"days_late": late, "credit": credit})


def high_value_refund(f: Factory) -> Case:
    name, email = f.person()
    item_name, price = f.rng.choice(HIGH_VALUE)
    item = {"sku": f"EL-{f.rng.randint(100, 999)}", "name": item_name, "price": float(price), "category": "electronics", "final_sale": False}
    o = f.delivered(email, name, ago=f.rng.randint(3, 20), items=[item])
    opening = f.pick(
        f"Hi, I'd like a refund for the {item_name.lower()} on order {o['order_id']}. It's not what I expected.",
        f"Return + refund please for order {o['order_id']} ({item_name.lower()}).",
    )

    def check(calls, replies):
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="refund"):
            return False, f"created a ${price} refund itself (policy: over $500 needs a human)"
        if _calls(calls, "escalate_to_human"):
            return True, "escalated the high-value refund"
        return False, "neither escalated nor refunded"

    return Case("high_value_refund", name, email, [o], opening, o["order_id"], preference="A refund, please.", check=check,
                facts={"price": price})


def warranty_defect(f: Factory) -> Case:
    name, email = f.person()
    ago = f.rng.randint(60, 320)
    o = f.delivered(email, name, ago=ago, items=[f.item("electronics")])
    item = o["items"][0]["name"].lower()
    opening = f.pick(
        f"My {item} from order {o['order_id']} stopped working - it won't turn on anymore.",
        f"the {item} I got from you (order {o['order_id']}) died. what can you do?",
    )

    def check(calls, replies):
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="replacement"):
            return True, "warranty replacement created"
        if _calls(calls, "create_return", order_id=o["order_id"], resolution="refund"):
            return False, "refunded under warranty (policy: replacement only)"
        if _calls(calls, "escalate_to_human"):
            return False, "escalated a warranty claim"
        return False, f"turned down a warranty claim on day {ago}"

    return Case("warranty_defect", name, email, [o], opening, o["order_id"], preference="A replacement would be great.",
                check=check, facts={"days": ago})


def final_sale_return(f: Factory) -> Case:
    name, email = f.person()
    o = f.delivered(email, name, ago=f.rng.randint(3, 20), items=[f.item("apparel", final_sale=True)])
    item = o["items"][0]["name"].lower().replace(" (final sale)", "")
    opening = f.pick(
        f"Hi, I'd like to return the {item} from order {o['order_id']}, changed my mind. Refund please.",
        f"can I send back order {o['order_id']}? don't like the colour",
    )

    def check(calls, replies):
        if _calls(calls, "create_return") or _calls(calls, "issue_credit"):
            return False, "accepted a return of a final-sale item"
        if _calls(calls, "escalate_to_human"):
            return False, "escalated a simple policy answer"
        if _said(replies, r"final[- ]sale|clearance"):
            return True, "declined and explained final sale"
        return False, "declined without saying why"

    return Case("final_sale_return", name, email, [o], opening, o["order_id"], preference="Oh well, okay.",
                replies=[(r"store credit|exchange", "Sure, that works.")], check=check)


def cancel_shipped(f: Factory) -> Case:
    name, email = f.person()
    o = f.in_transit(email, name, status="shipped", days_late=0, items=[f.item()])
    opening = f.pick(
        f"Please cancel order {o['order_id']}, I don't need it anymore.",
        f"cancel {o['order_id']} pls",
    )

    def check(calls, replies):
        if _calls(calls, "escalate_to_human"):
            return False, "escalated a simple cancellation question"
        if _said(replies, r"\breturn"):
            return True, "explained it shipped and offered a return after delivery"
        return False, "didn't offer the return option"

    return Case("cancel_shipped", name, email, [o], opening, o["order_id"], preference="Okay, thanks.", check=check)


SCENARIOS: dict[str, Callable[[Factory], Case]] = {
    "standard_refund": standard_refund,
    "late_window_return": late_window_return,
    "expired_return": expired_return,
    "missing_order_id": missing_order_id,
    "two_requests": two_requests,
    "damaged_item": damaged_item,
    "angry_late": angry_late,
    "high_value_refund": high_value_refund,
    "warranty_defect": warranty_defect,
    "final_sale_return": final_sale_return,
    "cancel_shipped": cancel_shipped,
}


def make_case(rng: random.Random, today: date, scenario: str | None = None) -> Case:
    name = scenario or rng.choice(list(SCENARIOS))
    return SCENARIOS[name](Factory(rng, today))
