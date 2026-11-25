"""ShopBot's tools: Claude tool definitions (strict schemas) and their implementations on the Store.

The tools are deliberately honest back-office APIs: they do what they're asked and don't enforce the
support policy. Applying the policy is the agent's job — that is what Darwin evolves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .store import POLICY_TOPICS, Store


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }


TOOLS = [
    _tool("lookup_order", "Get an order by its ID (like A12345): items, prices, status, dates, tracking, "
          "delivered_days_ago and days_late.", {"order_id": {"type": "string"}}, ["order_id"]),
    _tool("find_orders", "List a customer's orders by the email address on their account.",
          {"email": {"type": "string"}}, ["email"]),
    _tool("get_policy", "Read the store's support policy on a topic.",
          {"topic": {"type": "string", "enum": list(POLICY_TOPICS)}}, ["topic"]),
    _tool("create_return", "Create a return for a delivered order and choose how the customer is made whole.",
          {"order_id": {"type": "string"},
           "reason": {"type": "string", "enum": ["changed_mind", "damaged", "defective", "warranty", "wrong_item", "other"]},
           "resolution": {"type": "string", "enum": ["refund", "store_credit", "replacement"]}},
          ["order_id", "reason", "resolution"]),
    _tool("cancel_order", "Cancel an order.", {"order_id": {"type": "string"}}, ["order_id"]),
    _tool("issue_credit", "Give the customer store credit tied to an order.",
          {"order_id": {"type": "string"}, "amount_usd": {"type": "number"}, "reason": {"type": "string"}},
          ["order_id", "amount_usd", "reason"]),
    _tool("escalate_to_human", "Hand the conversation to a human support specialist.",
          {"reason": {"type": "string"}, "summary": {"type": "string"}}, ["reason", "summary"]),
]
TOOL_NAMES = [t["name"] for t in TOOLS]


@dataclass
class ToolResult:
    output: dict
    is_error: bool = False

    def content(self) -> str:
        return json.dumps(self.output, ensure_ascii=False)


def execute(store: Store, name: str, args: dict) -> ToolResult:
    def err(message: str) -> ToolResult:
        return ToolResult({"error": message}, is_error=True)

    if name == "get_policy":
        topic = args.get("topic", "")
        text = store.policy.get(topic)
        return ToolResult({"topic": topic, "policy": text}) if text else err(f"Unknown topic. Topics: {', '.join(POLICY_TOPICS)}")

    if name == "find_orders":
        orders = store.by_email(args.get("email", ""))
        return ToolResult({"orders": [
            {"order_id": o.order_id, "ordered_on": o.ordered_on, "status": o.status, "order_total": o.total,
             "items": [i.name for i in o.items]} for o in orders
        ]} if orders else {"orders": [], "note": "No orders found for that email."})

    if name == "escalate_to_human":
        ref = store.record(None, {"type": "escalation", **args})
        return ToolResult({"ticket_id": ref, "message": "A specialist will reply by email within 24 hours."})

    order = store.get(args.get("order_id", ""))
    if order is None:
        return err(f"No order with ID {args.get('order_id')!r}. Order IDs look like A12345.")

    if name == "lookup_order":
        return ToolResult(order.view(store.today))

    if name == "create_return":
        if order.status != "delivered":
            return err(f"Order {order.order_id} hasn't been delivered yet (status: {order.status}).")
        ref = store.record(order, {"type": "return", **args})
        steps = {"refund": "Refund to the original payment method within 5 business days of the item arriving back.",
                 "store_credit": "Store credit is added as soon as the item is scanned at the carrier.",
                 "replacement": "A replacement ships within 2 business days."}[args["resolution"]]
        return ToolResult({"return_id": ref, "resolution": args["resolution"], "next_steps": steps})

    if name == "cancel_order":
        if order.status != "processing":
            return err(f"Order {order.order_id} has already {order.status} and can't be cancelled.")
        ref = store.record(order, {"type": "cancellation"})
        order.status = "cancelled"
        return ToolResult({"cancellation_id": ref, "order_id": order.order_id, "status": "cancelled"})

    if name == "issue_credit":
        amount = float(args.get("amount_usd", 0))
        if amount <= 0 or amount > 100:
            return err("Credit amount must be between $0 and $100.")
        ref = store.record(order, {"type": "credit", **args})
        return ToolResult({"credit_id": ref, "amount_usd": amount})

    return err(f"Unknown tool {name}")
