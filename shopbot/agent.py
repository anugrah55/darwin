"""ShopBot's agent loop: one customer message in, one reply out, tools in between.

The genome (prompt, model, effort) comes from Flagsmith per conversation, so the loop is a plain Messages
API loop over our own tools. Assistant turns are appended unchanged (thinking blocks included) and history
is never edited. A reply cut off by max_tokens can end in a tool_use that never ran; it is closed with an
error tool_result, so the next request in the conversation is still valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from darwin.genome import Genome
from darwin.llm import BudgetExceeded

from .store import Store
from .tools import TOOLS, execute

MAX_STEPS = 6
FALLBACK_REPLY = "Sorry — I couldn't finish that. A teammate will follow up by email."
CUT_OFF = "Not run: the reply hit its length limit before this tool call was complete."


@dataclass
class TurnResult:
    reply: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    stop_reason: str = ""
    escalated: bool = False


def _add_usage(total: dict, usage: Any) -> None:
    total["input"] += getattr(usage, "input_tokens", 0) or 0
    total["output"] += getattr(usage, "output_tokens", 0) or 0
    total["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    total["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def run_turn(claude, genome: Genome, messages: list, store: Store, *, purpose: str = "shopbot") -> TurnResult:
    """`messages` already ends with the customer's new message; it is extended in place."""
    result = TurnResult(reply="")
    try:
        for _ in range(MAX_STEPS):
            response = claude.create(
                purpose=purpose,
                model=genome.model,
                max_tokens=genome.max_tokens,
                system=genome.system_prompt,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": genome.effort},
                cache_control={"type": "ephemeral"},
            )
            _add_usage(result.usage, response.usage)
            messages.append({"role": "assistant", "content": response.content})
            result.stop_reason = response.stop_reason or ""
            if response.stop_reason != "tool_use":
                break
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = execute(store, block.name, dict(block.input or {}))
                result.tool_calls.append({"name": block.name, "input": dict(block.input or {}), "output": outcome.output,
                                          "is_error": outcome.is_error})
                if block.name == "escalate_to_human" and not outcome.is_error:
                    result.escalated = True
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": outcome.content(),
                                     **({"is_error": True} if outcome.is_error else {})})
            messages.append({"role": "user", "content": tool_results})
        else:
            result.stop_reason = "max_steps"
    except BudgetExceeded as exc:
        exc.usage = result.usage  # what this turn had already spent when the cap stopped it
        raise

    last = messages[-1]
    dangling = [b for b in last["content"] if getattr(b, "type", "") == "tool_use"] if last["role"] == "assistant" else []
    if dangling:  # max_tokens (or another stop) cut the reply inside a tool call: close it so history stays valid
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b.id, "content": CUT_OFF,
                                                      "is_error": True} for b in dangling]})
    elif last["role"] == "assistant":
        result.reply = "".join(getattr(b, "text", "") for b in last["content"] if getattr(b, "type", "") == "text").strip()
    if result.stop_reason == "refusal" or not result.reply:
        result.reply = FALLBACK_REPLY
    return result
