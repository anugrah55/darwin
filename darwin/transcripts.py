"""Read ShopBot's transcripts and render them compactly for the Coach."""

from __future__ import annotations

import json
import random
from pathlib import Path


def load(path: Path, *, generation: str | None = None, since: float | None = None, channel: str | None = None) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if generation and row.get("generation") != generation:
            continue
        if since and row.get("started_at", 0) < since:
            continue
        if channel and row.get("channel") != channel:
            continue
        rows.append(row)
    return rows


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"chats": 0, "resolved": 0, "escalated": 0, "resolution_rate": None, "escalation_rate": None, "avg_cost_usd": None}
    resolved = sum(1 for r in rows if r.get("resolved"))
    escalated = sum(1 for r in rows if r.get("escalated"))
    return {
        "chats": n,
        "resolved": resolved,
        "escalated": escalated,
        "resolution_rate": resolved / n,
        "escalation_rate": escalated / n,
        "avg_cost_usd": sum(r.get("cost_usd", 0) for r in rows) / n,
        "avg_turns": sum(sum(1 for t in r["turns"] if t["role"] == "customer") for r in rows) / n,
    }


def _short(value, limit: int = 240) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(row: dict, index: int) -> str:
    """One chat as plain text: what the customer said, what ShopBot did and said, and how it ended."""
    outcome = "RESOLVED" if row.get("resolved") else "NOT RESOLVED"
    if row.get("escalated"):
        outcome += ", handed to a human"
    lines = [f"### Chat {index} — {outcome} ({row.get('generation')}, {sum(1 for t in row['turns'] if t['role'] == 'customer')} customer messages)"]
    for turn in row["turns"]:
        if turn["role"] == "customer":
            lines.append(f"customer: {turn['text']}")
            continue
        for call in turn.get("tools", []):
            args = ", ".join(f"{k}={_short(v, 80)}" for k, v in call["input"].items())
            output = call["output"]
            if call["name"] == "get_policy" and isinstance(output, dict) and "policy" in output:
                shown = f"policy[{output.get('topic')}]: {output['policy']}"
            else:
                shown = _short(output)
            lines.append(f"  [tool] {call['name']}({args}) -> {'ERROR ' if call.get('is_error') else ''}{shown}")
        lines.append(f"shopbot: {turn['text']}")
    if row.get("note"):
        lines.append(f"customer feedback: {row['note']}")
    return "\n".join(lines)


def sample_for_coach(rows: list[dict], *, failed: int = 24, succeeded: int = 4, seed: int = 0) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    bad = [r for r in rows if not r.get("resolved")]
    good = [r for r in rows if r.get("resolved")]
    rng.shuffle(bad)
    rng.shuffle(good)
    return bad[:failed], good[:succeeded]
