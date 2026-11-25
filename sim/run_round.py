"""Run a round of simulated customers against a running ShopBot.

    uv run darwin round --chats 150

Each customer gets a fresh Flagsmith identity, so Flagsmith assigns them to an arm of whatever experiment
is running. Success is judged by the scenario's rule and reported to ShopBot as the customer's
"did this solve your problem?" answer — exactly the path a real customer's click takes. The reason a
chat failed stays in the simulator's own log (data/sim/), never in the transcripts the Coach reads.
"""

from __future__ import annotations

import json
import random
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from .customer import GIVE_UP, CustomerState, next_message
from .scenarios import Case, make_case


class BudgetStop(RuntimeError):
    pass


@dataclass
class ChatOutcome:
    user_id: str
    scenario: str
    generation: str
    variant: str
    resolved: bool
    escalated: bool
    why: str
    turns: int
    tools: list[str]


def run_chat(http: httpx.Client, case: Case, user_id: str) -> ChatOutcome:
    s = http.post("/chat/start", json={"user_id": user_id, "channel": "sim"}).raise_for_status().json()
    sid, calls, replies, escalated = s["session_id"], [], [], False
    try:
        message, state = case.opening, CustomerState()
        while message is not None:
            r = http.post(f"/chat/{sid}/message", json={"text": message})
            if r.status_code == 402:
                raise BudgetStop(r.json().get("detail", "budget exhausted"))
            data = r.raise_for_status().json()
            replies.append(data["reply"])
            calls.extend(data["tools"])
            if data["escalated"]:
                escalated = True
                break
            message = next_message(case, data["reply"], state)
        resolved, why = case.check(calls, replies)
        if state.gave_up and not resolved:
            why = f"customer gave up after {state.turn} messages; {why}"
        # A customer who walks away says so, like a real one would; the rule's verdict stays in the sim log.
        note = f"(customer left the chat) {GIVE_UP}" if state.gave_up and not resolved else None
        http.post(f"/chat/{sid}/end", json={"resolved": resolved, "note": note}).raise_for_status()
    except BaseException as exc:
        # Starting the chat recorded an exposure in Flagsmith, so it always gets an outcome (and its cost).
        what = "the Claude budget ran out" if isinstance(exc, BudgetStop) else "ShopBot or the simulator hit an error"
        try:
            http.post(f"/chat/{sid}/end", json={"resolved": False, "note": f"(chat ended early: {what})"})
        except Exception:
            pass
        raise
    return ChatOutcome(user_id, case.scenario, s["generation"], s["variant"], resolved, escalated, why, len(replies),
                       [c["name"] for c in calls])


def run_round(*, chats: int, shopbot_url: str, data_dir: Path, concurrency: int = 6, seed: int | None = None,
              scenario: str | None = None, quiet: bool = False) -> dict:
    seed = seed if seed is not None else int(time.time())
    round_id = f"r{seed % 1_000_000:06d}"
    today = date.today()
    cases = [make_case(random.Random(f"{seed}-{i}"), today, scenario) for i in range(chats)]
    log_dir = Path(data_dir) / "sim"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{round_id}.jsonl"

    outcomes: list[ChatOutcome] = []
    errors: list[str] = []
    lock = threading.Lock()
    started = time.time()
    with httpx.Client(base_url=shopbot_url, timeout=180) as http:
        http.get("/health").raise_for_status()
        http.post("/_sim/seed", json={"orders": [o for c in cases for o in c.orders]}).raise_for_status()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(run_chat, http, c, f"sim-{round_id}-{i:04d}"): c for i, c in enumerate(cases)}
            for future in as_completed(futures):
                try:
                    o = future.result()
                except BudgetStop as exc:
                    errors.append(str(exc))
                    for f in futures:
                        f.cancel()
                    continue
                except Exception as exc:  # one broken chat shouldn't sink the round
                    errors.append(f"{type(exc).__name__}: {exc}")
                    continue
                with lock:
                    outcomes.append(o)
                    with open(log_path, "a") as f:
                        f.write(json.dumps(o.__dict__) + "\n")
                    if not quiet:
                        mark = "✓" if o.resolved else ("⇪" if o.escalated else "✗")
                        if sys.stdout.isatty():
                            sys.stdout.write(f"\r  {len(outcomes):>4}/{chats}  {mark} {o.generation:<6} {o.scenario:<26}")
                            sys.stdout.flush()
                        elif len(outcomes) % 25 == 0 or len(outcomes) == chats:
                            print(f"  {len(outcomes)}/{chats} chats done", flush=True)
        http.post("/_sim/flush")
    if not quiet:
        sys.stdout.write("\n")
    summary = summarise(outcomes)
    summary.update({"round_id": round_id, "seed": seed, "seconds": round(time.time() - started, 1), "errors": errors,
                    "log": str(log_path)})
    return summary


def summarise(outcomes: list[ChatOutcome]) -> dict:
    by_gen: dict[str, Counter] = defaultdict(Counter)
    by_scenario: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for o in outcomes:
        for bucket in (by_gen[o.generation], by_scenario[o.scenario][o.generation]):
            bucket["chats"] += 1
            bucket["resolved"] += o.resolved
            bucket["escalated"] += o.escalated
    return {
        "chats": len(outcomes),
        "by_generation": {g: dict(c) for g, c in sorted(by_gen.items())},
        "by_scenario": {s: {g: dict(c) for g, c in gens.items()} for s, gens in sorted(by_scenario.items())},
    }


def print_summary(summary: dict) -> None:
    print(f"\nRound {summary['round_id']}: {summary['chats']} chats in {summary['seconds']}s")
    for gen, c in summary["by_generation"].items():
        print(f"  {gen:<6} {c['chats']:>4} chats   resolved {c['resolved'] / c['chats']:6.1%}   escalated {c['escalated'] / c['chats']:6.1%}")
    gens = list(summary["by_generation"])
    print("\n  scenario               " + "".join(f"{g:>12}" for g in gens))
    for scenario, per_gen in summary["by_scenario"].items():
        cells = []
        for g in gens:
            c = per_gen.get(g)
            cells.append(f"{c['resolved']:>3}/{c['chats']:<3} {c['resolved'] / c['chats']:4.0%}" if c else f"{'–':>12}")
        print(f"  {scenario:<22} " + "".join(f"{x:>12}" for x in cells))
    for e in summary["errors"][:5]:
        print(f"  ! {e}")
