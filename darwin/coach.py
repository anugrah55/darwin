"""The Coach: reads what went wrong in real conversations and writes a challenger genome.

One structured call to Claude Opus 5.5 does the diagnosis (failure patterns with counts and quotes) and
the mutation (a complete new system prompt, plus effort). A rejected proposal can be revised with the
human's note. The Coach only proposes — Flagsmith's experiment decides, and a human approves.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from .genome import ALLOWED_MODELS, Genome
from .llm import Claude, price_of
from .transcripts import render

MAX_COACH_PROMPT_CHARS = 8_000

GOALS = {
    "quality": "Raise the share of chats ShopBot resolves correctly by itself. Don't let cost per chat rise by more than 30%.",
    "cost": "Cut the cost per chat by at least 30% without lowering the resolution rate or raising escalations to humans.",
}

SYSTEM = """You are the Coach in Darwin, a system that improves an AI customer-support agent through controlled experiments.

You read evidence from real conversations, find the behaviours that make the agent fail, and write a challenger version \
of its configuration: its system prompt and, optionally, its effort setting. A Flagsmith A/B test then compares your \
challenger with the current champion on live traffic, and a human approves every step. Your challenger only survives \
if it measurably wins, so aim for changes that will actually move the metrics.

How to work:
1. Diagnose. Group the failed conversations into patterns. For each pattern give an honest count (how many of the \
failed chats shown to you show it), a short verbatim quote from one of them, the root cause in the agent's current \
instructions (what they say, or fail to say, that leads to this behaviour), and the fix. Report only what you can see \
in the chats. Successful chats are included for contrast: don't break what already works.
2. Write the challenger's system prompt, in full. Fix the behaviours with general rules the agent can apply to new \
customers. Never mention specific customers, order IDs, amounts or dates from these chats. The support policy lives \
behind the get_policy tool; when the chats show the agent getting a rule wrong, you may state that rule in the prompt, \
quoting the policy text that appears in the chats. Keep the prompt clear and compact (under 5,000 characters): every \
token in it is paid for in every conversation.
3. Settings. Effort (low, medium or high) trades reasoning for cost: higher effort costs more per chat. Change the \
model only if the request says model changes are allowed and the goal requires it.
4. State a falsifiable hypothesis and the effect you expect on the experiment's metrics.

What the metrics mean: `resolved` — ShopBot handled the customer's issue correctly by itself (handing off to a human \
counts only when policy requires a human); `escalated` — the chat was handed to a human, which is expensive; \
`cost_microusd` — what the chat cost in model usage."""


class Pattern(BaseModel):
    pattern: str = Field(description="The failure behaviour, in one sentence.")
    count: int = Field(description="How many of the failed chats shown here show this behaviour.")
    example_quote: str = Field(description="A short verbatim quote from one of the chats that shows it.")
    root_cause: str = Field(description="What in the current instructions leads to this behaviour.")
    fix: str = Field(description="The instruction change that fixes it.")


class Proposal(BaseModel):
    patterns: list[Pattern]
    hypothesis: str = Field(description="One falsifiable sentence: if we change X, metric Y moves because Z.")
    expected_effect: str = Field(description="Expected effect on resolved, escalated and cost per chat.")
    changes_summary: list[str] = Field(description="The changes made to the prompt and settings, one per item.")
    risks: str = Field(description="What could get worse, and which metric would show it.")
    summary: str = Field(description="Under 70 characters, for the lineage view.")
    system_prompt: str = Field(description="The challenger's complete system prompt.")
    effort: Literal["low", "medium", "high"]
    model: Literal["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]


class CoachError(RuntimeError):
    pass


def _tools_text(tools: list[dict]) -> str:
    return "\n".join(f"- {t['name']}: {t['description']}" for t in tools)


def _stats_text(name: str, stats: dict) -> str:
    if not stats.get("chats"):
        return f"No conversations recorded for {name} yet."
    return (f"{stats['chats']} chats: {stats['resolution_rate']:.0%} resolved, {stats['escalation_rate']:.0%} handed to a human, "
            f"${stats['avg_cost_usd']:.5f} average cost per chat, {stats['avg_turns']:.1f} customer messages per chat.")


def build_request(*, goal: str, champion: Genome, challenger_name: str, tools: list[dict], stats: dict,
                  history: list[str], failed: list[dict], succeeded: list[dict], allow_model_change: bool = False) -> str:
    models = (f"Allowed models: {', '.join(ALLOWED_MODELS)}." if allow_model_change else
              f"Keep the model {champion.model}: model changes are turned off for this run.")
    parts = [
        f"## Goal\n{GOALS.get(goal, goal)}",
        f"## Current champion: {champion.name}\nModel: {champion.model}. Effort: {champion.effort}.\n"
        f"<system_prompt>\n{champion.system_prompt}\n</system_prompt>",
        f"## The agent's tools\n{_tools_text(tools)}",
        f"## Recent performance of {champion.name}\n{_stats_text(champion.name, stats)}",
        "## Earlier generations\n" + ("\n".join(f"- {h}" for h in history) if history else "None yet."),
        f"## Failed conversations ({len(failed)})\n" + ("\n\n".join(render(r, i + 1) for i, r in enumerate(failed)) or "None."),
        f"## Successful conversations, for contrast ({len(succeeded)})\n"
        + ("\n\n".join(render(r, len(failed) + i + 1) for i, r in enumerate(succeeded)) or "None."),
        f"Write the challenger \"{challenger_name}\". {models}",
    ]
    return "\n\n".join(parts)


def check_proposal(p: Proposal, parent: Genome, goal: str, *, allow_model_change: bool = False) -> list[str]:
    """Darwin's guards on any challenger, the Coach's or a human's. A model change can multiply the cost per chat,
    so it is refused unless explicitly allowed (DARWIN_ALLOW_MODEL_CHANGE=1 or --allow-model-change)."""
    problems = []
    if p.model != parent.model and not allow_model_change:
        problems.append(f"it changes the model ({parent.model} → {p.model}), and model changes are off; set "
                        f"DARWIN_ALLOW_MODEL_CHANGE=1 or pass --allow-model-change to allow them")
    if len(p.system_prompt) > MAX_COACH_PROMPT_CHARS:
        problems.append(f"system prompt is {len(p.system_prompt):,} chars (limit {MAX_COACH_PROMPT_CHARS:,})")
    if p.system_prompt.strip() == parent.system_prompt.strip() and p.effort == parent.effort and p.model == parent.model:
        problems.append("proposal changes nothing")
    if re.search(r"\b[A-H]\d{5}\b", p.system_prompt):
        problems.append("system prompt mentions a specific order ID from the chats")
    if goal == "cost" and price_of(p.model)[1] > price_of(parent.model)[1]:
        problems.append(f"a cost goal can't move to a pricier model ({parent.model} → {p.model})")
    return problems


class Coach:
    def __init__(self, claude: Claude, model: str = "claude-opus-5-5", effort: str = "high", allow_model_change: bool = False):
        self.claude = claude
        self.model = model
        self.effort = effort
        self.allow_model_change = allow_model_change

    def _call(self, messages: list[dict]) -> Proposal:
        response = self.claude.parse(
            purpose="coach",
            model=self.model,
            max_tokens=16_000,
            system=SYSTEM,
            messages=messages,
            output_format=Proposal,
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise CoachError("the Coach declined this request")
        if response.parsed_output is None:
            raise CoachError(f"the Coach returned no proposal (stop reason: {response.stop_reason})")
        return response.parsed_output

    def propose(self, *, goal: str, champion: Genome, challenger_name: str, tools: list[dict], stats: dict,
                history: list[str], failed: list[dict], succeeded: list[dict]) -> tuple[Proposal, str]:
        request = build_request(goal=goal, champion=champion, challenger_name=challenger_name, tools=tools, stats=stats,
                                history=history, failed=failed, succeeded=succeeded, allow_model_change=self.allow_model_change)
        proposal = self._call([{"role": "user", "content": request}])
        problems = check_proposal(proposal, champion, goal, allow_model_change=self.allow_model_change)
        if problems:
            raise CoachError("proposal failed Darwin's checks: " + "; ".join(problems))
        return proposal, request

    def revise(self, *, request: str, previous: Proposal, note: str, champion: Genome, goal: str) -> Proposal:
        messages = [
            {"role": "user", "content": request},
            {"role": "assistant", "content": previous.model_dump_json()},
            {"role": "user", "content": f"The human reviewer rejected this proposal with this note:\n\n{note}\n\n"
                                        "Revise the proposal to address the note. Return the complete revised proposal."},
        ]
        proposal = self._call(messages)
        problems = check_proposal(proposal, champion, goal, allow_model_change=self.allow_model_change)
        if problems:
            raise CoachError("revised proposal failed Darwin's checks: " + "; ".join(problems))
        return proposal


def proposal_to_genome(p: Proposal, parent: Genome, name: str) -> Genome:
    return parent.child(name=name, system_prompt=p.system_prompt, model=p.model, effort=p.effort, summary=p.summary)


def dumps(p: Proposal) -> dict:
    return json.loads(p.model_dump_json())
