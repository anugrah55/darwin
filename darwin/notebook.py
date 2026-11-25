"""LAB_NOTEBOOK.md: a human-readable record of every generation — hypothesis, evidence, result, decision."""

from __future__ import annotations

import time
from pathlib import Path

HEADER = """# Darwin lab notebook

Every generation of `{flag}`: what the Coach saw, what it changed, what Flagsmith measured and who decided.
Written by Darwin as it runs.

"""


def _ts(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts or time.time()))


class Notebook:
    def __init__(self, path: Path, flag: str):
        self.path = Path(path)
        self.flag = flag

    def _append(self, text: str) -> None:
        if not self.path.exists():
            self.path.write_text(HEADER.format(flag=self.flag))
        with open(self.path, "a") as f:
            f.write(text.rstrip() + "\n\n")

    def champion(self, genome: dict, note: str) -> None:
        self._append(f"## {genome['name']} — champion ({_ts()})\n\n{note}\n\n- Model `{genome['model']}`, effort `{genome['effort']}`")

    def launched(self, card: dict, experiment_id: int, approver: str) -> None:
        p = card["proposal"]
        g = card["genome"]
        patterns = "\n".join(f"  - {x['pattern']} (×{x['count']}) — e.g. “{x['example_quote']}”" for x in p["patterns"])
        changes = "\n".join(f"  - {c}" for c in p["changes_summary"])
        commands = "\n".join(f"    {c['command']}" for c in card["commands"])
        stats = card.get("stats") or {}
        rate = stats.get("resolution_rate")
        baseline = f"{stats.get('chats')} chats, {rate:.0%} resolved, {stats.get('escalation_rate', 0):.0%} escalated" if rate is not None else "–"
        self._append(
            f"## {g['name']} — challenger to {g['parent']} ({_ts()})\n\n"
            f"**Goal:** {card['goal']}  \n**Hypothesis:** {p['hypothesis']}  \n**Expected effect:** {p['expected_effect']}\n\n"
            f"**Evidence** ({card.get('chats_read', 0)} chats read; {g['parent']}: {baseline}):\n{patterns}\n\n"
            f"**Changes:**\n{changes}\n- Model `{g['model']}`, effort `{g['effort']}`\n\n"
            f"**Launched** experiment #{experiment_id}, approved by **{approver}**. Commands run:\n\n{commands}"
        )

    def decided(self, generation: str, verdict: str, headline: str, reasons: list[str], approver: str, experiment_id: int,
                expected: str | None = None) -> None:
        bullet = "\n".join(f"- {r}" for r in reasons)
        predicted = f"\n\n**The Coach predicted:** {expected}  \n**Flagsmith measured:** see above." if expected else ""
        self._append(f"### {generation}: {verdict} ({_ts()})\n\n{headline}\n\n{bullet}{predicted}\n\n"
                     f"Experiment #{experiment_id} completed; approved by **{approver}**.")

    def rejected(self, generation: str, approver: str, note: str | None) -> None:
        extra = f" Note: “{note}”" if note else ""
        self._append(f"### {generation}: proposal rejected by **{approver}** ({_ts()})\n\n{extra.strip() or 'No note.'}")
