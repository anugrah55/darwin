"""A scripted customer: answers what ShopBot asks, the way the scenario's persona would.

No LLM — so a round of simulated traffic costs nothing but ShopBot's own calls, and every run is
reproducible from its seed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .scenarios import Case

ASKS_ORDER_ID = re.compile(r"order (number|id|#|no\b)|order confirmation|which order", re.I)
ASKS_EMAIL = re.compile(r"e-?mail( address)?", re.I)
ASKS_REASON = re.compile(r"\b(why|reason|what('s| is| went) wrong|what happened)\b", re.I)
CLOSING = re.compile(r"anything else|something else|help with anything|have a (great|nice|good|lovely)", re.I)
GIVE_UP = "Never mind, this is taking too long."
# ShopBot is waiting on the customer when it asks a question *or* asks for something ("I'll need your email").
WAITING = re.compile(r"\?|\b(i('ll| will)? need|i just need|please (provide|share|send|confirm|tell|reply|let me know)|"
                     r"let me know|could you|can you|share (your|the)|send (me )?(your|the)|provide (your|the)|reply with)\b", re.I)


@dataclass
class CustomerState:
    turn: int = 1  # the opening message is turn 1
    gave_order_id: bool = False
    gave_email: bool = False
    gave_reason: bool = False
    gave_up: bool = False  # left with a question from ShopBot still unanswered
    used: set[int] = field(default_factory=set)


def next_message(case: Case, bot_reply: str, state: CustomerState) -> str | None:
    """What the customer says next, or None when they're done talking."""
    asks = bool(WAITING.search(bot_reply))
    if not asks:
        return None  # ShopBot made a statement (an answer or a refusal): the customer leaves
    if state.turn >= case.max_turns:
        state.gave_up = not CLOSING.search(bot_reply)
        return None
    reply = _answer(case, bot_reply, state)
    if reply is not None:
        state.turn += 1
    return reply


def _answer(case: Case, text: str, state: CustomerState) -> str | None:
    if ASKS_REASON.search(text) and not state.gave_reason and not ASKS_ORDER_ID.search(text):
        state.gave_reason = True
        return case.reason
    if ASKS_ORDER_ID.search(text) and not state.gave_order_id:
        state.gave_order_id = True
        if case.knows_order_id and case.order_id:
            return f"It's {case.order_id}."
        state.gave_email = True
        return f"I don't have the order number, sorry. The email on my account is {case.email}."
    if ASKS_EMAIL.search(text) and not state.gave_email:
        state.gave_email = True
        return f"Sure, it's {case.email}."
    for i, (pattern, answer) in enumerate(case.replies):
        if i not in state.used and re.search(pattern, text, re.I):
            state.used.add(i)
            return answer
    if CLOSING.search(text) and not re.search(r"would you like|shall i|should i|do you want|want me to", text, re.I):
        return None
    return case.preference
