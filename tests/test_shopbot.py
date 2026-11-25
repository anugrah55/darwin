from datetime import date, timedelta
from types import SimpleNamespace

from darwin.genome import Genome
from shopbot.agent import run_turn
from shopbot.store import Item, Order, Store
from shopbot.tools import TOOLS, execute

TODAY = date(2026, 11, 26)
GEN1 = Genome(name="gen1", generation=1, model="claude-haiku-5-5", system_prompt="You are ShopBot.")


def d(days):
    return (TODAY - timedelta(days=days)).isoformat()


def store():
    s = Store(today=TODAY)
    s.add(Order("A2001", "kai@example.com", "Kai", [Item("AP-1", "Jacket", 80.0, "apparel")], "delivered", d(20), d(15), d(17), d(15)))
    s.add(Order("A2002", "kai@example.com", "Kai", [Item("OD-1", "Tent", 150.0, "outdoor")], "processing", d(1), d(-5)))
    s.add(Order("A2003", "kai@example.com", "Kai", [Item("OD-2", "Stove", 60.0, "outdoor")], "shipped", d(14), d(8), d(12)))
    return s


def test_tool_schemas_are_strict():
    for tool in TOOLS:
        assert tool["strict"] is True
        assert tool["input_schema"]["additionalProperties"] is False


def test_lookup_reports_delivery_age_and_lateness():
    out = execute(store(), "lookup_order", {"order_id": "a2001"}).output
    assert out["delivered_days_ago"] == 15 and out["days_late"] == 0 and out["order_total"] == 80.0
    late = execute(store(), "lookup_order", {"order_id": "A2003"}).output
    assert late["days_late"] == 8


def test_tools_apply_backend_rules_but_not_support_policy():
    s = store()
    assert execute(s, "cancel_order", {"order_id": "A2003"}).is_error  # shipped can't be cancelled
    assert not execute(s, "cancel_order", {"order_id": "A2002"}).is_error
    # a refund on day 15 is fine, and the tool would also accept a policy-breaking one — that's the agent's job
    assert execute(s, "create_return", {"order_id": "A2001", "reason": "changed_mind", "resolution": "refund"}).output["return_id"]
    assert execute(s, "find_orders", {"email": "KAI@example.com"}).output["orders"]
    assert "30 days" in execute(s, "get_policy", {"topic": "returns"}).output["policy"]


class FakeClaude:
    """Calls lookup_order once, then answers."""

    def __init__(self):
        self.calls = []

    def create(self, **request):
        self.calls.append(request)
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        if len(self.calls) == 1:
            block = SimpleNamespace(type="tool_use", id="t1", name="lookup_order", input={"order_id": "A2001"})
            return SimpleNamespace(content=[block], stop_reason="tool_use", usage=usage)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="It was delivered 15 days ago.")], stop_reason="end_turn", usage=usage)


def test_agent_loop_runs_tools_and_returns_the_reply():
    claude = FakeClaude()
    messages = [{"role": "user", "content": "Where is A2001?"}]
    turn = run_turn(claude, GEN1, messages, store())
    assert turn.reply == "It was delivered 15 days ago."
    assert [c["name"] for c in turn.tool_calls] == ["lookup_order"]
    assert turn.usage["input"] == 200 and turn.usage["output"] == 40
    assert claude.calls[0]["system"] == "You are ShopBot." and claude.calls[0]["output_config"] == {"effort": "medium"}
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]  # append-only history
