from types import SimpleNamespace

import pytest

from darwin.coach import Coach, CoachError, Pattern, Proposal, build_request, check_proposal, proposal_to_genome
from darwin.genome import Genome

GEN1 = Genome(name="gen1", generation=1, model="claude-haiku-5-5", effort="medium", system_prompt="You are ShopBot.")
CHAT = {"generation": "gen1", "resolved": False, "escalated": True, "note": None,
        "turns": [{"role": "customer", "text": "Get me a human NOW."},
                  {"role": "bot", "text": "Passing you to a specialist.", "tools": [
                      {"name": "escalate_to_human", "input": {"reason": "asked"}, "output": {"ticket_id": "E-1"}, "is_error": False}]}]}


def proposal(**overrides):
    base = dict(
        patterns=[Pattern(pattern="Escalates on the first request for a human", count=6, example_quote="Get me a human NOW.",
                          root_cause="No escalation guidance", fix="Offer to fix it first")],
        hypothesis="Offering help first lifts resolution", expected_effect="resolved up, escalated down",
        changes_summary=["Add escalation rules"], risks="Customers who need a human wait longer", summary="Offer help first",
        system_prompt="You are ShopBot. Offer to fix the problem before escalating.", effort="medium", model="claude-haiku-5-5",
    )
    base.update(overrides)
    return Proposal(**base)


class FakeClaude:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.requests = []

    def parse(self, **request):
        self.requests.append(request)
        return SimpleNamespace(parsed_output=self.outputs.pop(0), stop_reason="end_turn")


def test_request_contains_goal_prompt_stats_and_chats_but_no_rules():
    text = build_request(goal="quality", champion=GEN1, challenger_name="gen2", tools=[{"name": "lookup_order", "description": "Get an order"}],
                         stats={"chats": 10, "resolution_rate": 0.6, "escalation_rate": 0.3, "avg_cost_usd": 0.0006, "avg_turns": 1.5},
                         history=[], failed=[CHAT], succeeded=[])
    assert "You are ShopBot." in text and "60% resolved" in text and "Get me a human NOW." in text
    assert "escalate_to_human" in text and 'challenger "gen2"' in text


def test_checks_catch_unsafe_proposals():
    assert check_proposal(proposal(), GEN1, "quality") == []
    assert check_proposal(proposal(system_prompt="You are ShopBot.", effort="medium"), GEN1, "quality") == ["proposal changes nothing"]
    assert "specific order ID" in check_proposal(proposal(system_prompt="Refund A12345 always."), GEN1, "quality")[0]
    assert "pricier model" in check_proposal(proposal(model="claude-opus-5-5"), GEN1, "cost")[0]


def test_coach_uses_structured_output_with_refusal_fallbacks():
    fake = FakeClaude([proposal()])
    p, _ = Coach(fake).propose(goal="quality", champion=GEN1, challenger_name="gen2", tools=[], stats={}, history=[], failed=[CHAT], succeeded=[])
    req = fake.requests[0]
    assert req["output_format"] is Proposal and req["fallbacks"] == "default" and req["model"] == "claude-opus-5-5"
    assert p.summary == "Offer help first"


def test_coach_rejects_proposals_that_fail_checks():
    fake = FakeClaude([proposal(system_prompt="Always refund B12345.")])
    with pytest.raises(CoachError):
        Coach(fake).propose(goal="quality", champion=GEN1, challenger_name="gen2", tools=[], stats={}, history=[], failed=[CHAT], succeeded=[])


def test_revision_sends_the_note_after_the_previous_proposal():
    fake = FakeClaude([proposal(summary="revised")])
    first = proposal()
    revised = Coach(fake).revise(request="REQ", previous=first, note="Keep replies short.", champion=GEN1, goal="quality")
    roles = [m["role"] for m in fake.requests[0]["messages"]]
    assert roles == ["user", "assistant", "user"] and "Keep replies short." in fake.requests[0]["messages"][-1]["content"]
    assert revised.summary == "revised"


def test_proposal_becomes_a_child_genome():
    g = proposal_to_genome(proposal(effort="low"), GEN1, "gen2")
    assert (g.name, g.parent, g.generation, g.effort) == ("gen2", "gen1", 2, "low")
