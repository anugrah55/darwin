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


def is_valid_history(messages) -> bool:
    """Every tool_use is answered by a tool_result in the next message."""
    for i, m in enumerate(messages):
        if m["role"] != "assistant":
            continue
        ids = {b.id for b in m["content"] if getattr(b, "type", "") == "tool_use"}
        if not ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        answered = {b["tool_use_id"] for b in (nxt["content"] if nxt and isinstance(nxt["content"], list) else [])
                    if isinstance(b, dict) and b.get("type") == "tool_result"}
        if ids - answered:
            return False
    return True


class CutOffClaude:
    """Starts a tool call, then runs out of max_tokens in the middle of it."""

    def create(self, **request):
        usage = SimpleNamespace(input_tokens=100, output_tokens=2048, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        block = SimpleNamespace(type="tool_use", id="t9", name="lookup_order", input={})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="Let me check."), block], stop_reason="max_tokens",
                               usage=usage)


def test_a_reply_cut_off_inside_a_tool_call_leaves_a_valid_conversation():
    messages = [{"role": "user", "content": "Where is A2001?"}]
    turn = run_turn(CutOffClaude(), GEN1, messages, store())
    assert turn.stop_reason == "max_tokens" and turn.tool_calls == [] and turn.reply.startswith("Sorry")
    assert is_valid_history(messages) and messages[-1]["content"][0]["is_error"] is True


class BudgetOnSecondCall(FakeClaude):
    def create(self, **request):
        from darwin.llm import BudgetExceeded
        if self.calls:
            raise BudgetExceeded("cap reached")
        return super().create(**request)


def test_hitting_the_budget_mid_turn_rolls_the_turn_back_but_keeps_its_cost(tmp_path):
    from dataclasses import replace

    import pytest
    from fastapi import HTTPException

    from darwin.config import Settings
    from shopbot.app import ShopBot, StartBody

    settings = replace(Settings.load(), data_dir=tmp_path, flagsmith_server_key=None)
    bot = ShopBot(settings)
    bot.claude = BudgetOnSecondCall()
    session = bot.start(StartBody(user_id="u1", channel="test"))
    with pytest.raises(HTTPException) as err:
        bot.message(session, "Where is A2001?")
    assert err.value.status_code == 402
    assert session.messages == [] and session.turns == [] and session.cost_usd > 0


def test_the_chat_page_starts_a_session_on_the_first_message_not_on_load():
    from pathlib import Path

    page = (Path(__file__).parent.parent / "shopbot" / "static" / "chat.html").read_text()
    script = page.split("<script>")[1].split("</script>")[0]
    assert script.count('fetch("/chat/start"') == 1
    start = script.index("async function startSession()")
    assert start < script.index('fetch("/chat/start"') < script.index("async function send(")
    assert "if (!sid) await startSession();" in script
    assert script.rstrip().endswith("reset();")  # page load only resets the UI
