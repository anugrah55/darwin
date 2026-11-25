"""darwin — evolve an AI agent with Flagsmith Experimentation. You approve every change."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from pathlib import Path

from .config import ROOT, Settings
from .genome import Genome

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


bold = lambda t: c(t, "1")  # noqa: E731
dim = lambda t: c(t, "2")  # noqa: E731
green = lambda t: c(t, "32")  # noqa: E731
red = lambda t: c(t, "31")  # noqa: E731
yellow = lambda t: c(t, "33")  # noqa: E731
cyan = lambda t: c(t, "36")  # noqa: E731

VERDICT_COLOR = {"SHIP": green, "KILL": red, "DISCARD": red, "HOLD": yellow, "KEEP_RUNNING": yellow, "COLLECTING_DATA": dim}


def actor_of(args) -> str:
    return getattr(args, "actor", None) or os.environ.get("DARWIN_APPROVER") or getpass.getuser()


def steward():
    from .steward import Steward

    return Steward(Settings.load())


def pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"


def print_card(card: dict) -> None:
    bar = "│ "
    if card["kind"] == "launch":
        p, g, stats = card["proposal"], card["genome"], card.get("stats") or {}
        print(bold(f"┌ Proposal {g['name']} — challenger to {g['parent']} (goal: {card['goal']})"))
        if card.get("author", "coach") != "coach":
            print(bar + yellow(f"Written by {card['author']}, not the Coach. It faces the same experiment and guardrails."))
        print(bar + f"The Coach read {card['chats_read']} chats. {g['parent']}: {stats.get('chats', 0)} chats, "
                    f"{pct(stats.get('resolution_rate'))} resolved, {pct(stats.get('escalation_rate'))} handed to a human. "
                    + dim(f"(Coach cost ${card.get('coach_cost_usd', 0):.3f})"))
        if card.get("review_note"):
            print(bar + yellow(f"Revised after your note: “{card['review_note']}”"))
        print(bar + bold("What's going wrong"))
        for x in p["patterns"]:
            print(bar + f"  • {x['pattern']} " + dim(f"(×{x['count']})") + f" — “{x['example_quote'][:110]}”")
        print(bar + bold("Hypothesis: ") + p["hypothesis"])
        print(bar + bold("Expected: ") + p["expected_effect"])
        print(bar + bold("Changes"))
        for ch in p["changes_summary"]:
            print(bar + f"  • {ch}")
        settings = card["diff"]["settings"]
        if settings:
            print(bar + "Settings: " + ", ".join(f"{k} {v['from']} → {v['to']}" for k, v in settings.items()))
        d = card["diff"]
        print(bar + bold(f"Prompt diff ") + green(f"+{d['lines_added']}") + " " + red(f"−{d['lines_removed']}"))
        for line in d["prompt_diff"][2:]:
            if line.startswith("+"):
                print(bar + green(line[:150]))
            elif line.startswith("-"):
                print(bar + red(line[:150]))
        print(bar + dim("Risk: " + p["risks"]))
        if card.get("plan"):
            print(bar + bold("What decides it"))
            for rule in card["plan"]["rules"]:
                print(bar + f"  • {rule}")
    else:
        color = VERDICT_COLOR.get(card["verdict"], bold)
        print(bold(f"┌ Decision on experiment #{card['experiment_id']}: ") + color(card["verdict"]))
        print(bar + card["headline"])
        for r in card["reasons"]:
            print(bar + f"  • {r}")
    print(bar + bold(f"Approving runs {len(card['commands'])} Flagsmith CLI commands:"))
    for i, cmd in enumerate(card["commands"], 1):
        print(bar + f"  {i}. {cmd['summary']}")
        print(bar + cyan(f"     $ {cmd['command']}"))
    print("└ " + dim("darwin approve   ·   darwin reject [--note \"what to change\"]"))


def cmd_init(args) -> None:
    gen1 = Genome.model_validate_json((ROOT / "shopbot" / "genomes" / "gen1.json").read_text())
    for note in steward().init(gen1, actor_of(args)):
        print("  " + note)
    print(green("Darwin is set up.") + " Next: start ShopBot (`darwin shopbot`) and send traffic (`darwin round`).")


def cmd_status(args) -> None:
    st = steward().status()
    print(bold(f"{st['flag']}") + f" — champion {bold(st['champion'] or '–')}")
    for g in st["lineage"]:
        s = g["stats"]
        mark = {"champion": green("★"), "challenger": yellow("◆"), "retired": dim("·"), "killed": red("✗"),
                "discarded": red("✗"), "rejected": dim("✗"), "proposed": yellow("?"), "stopped": red("■")}.get(g["status"], " ")
        line = f"  {mark} {g['name']:<6} {g['status']:<10} {g['model']} · {g['effort']:<6} {s['chats']:>4} chats  " \
               f"resolved {pct(s['resolution_rate']):>4}  escalated {pct(s['escalation_rate']):>4}"
        print(line + dim(f"  {g.get('summary') or ''}"))
    exp = st["experiment"]
    if exp:
        d = exp.get("decision") or {}
        print(bold(f"\nExperiment #{exp['id']}") + f": {exp['champion']} vs {exp['challenger']} (goal {exp['goal']})")
        if d:
            print("  " + VERDICT_COLOR.get(d["verdict"], bold)(d["verdict"]) + "  " + d["headline"])
            for r in d.get("reasons", []):
                print("   · " + r)
        else:
            print("  " + dim("no results read yet — run `darwin check`"))
    if st["pending"]:
        print()
        print_card(st["pending"])
    if st["busy"]:
        print(yellow(f"\nBusy: {st['busy']['what']}"))
    sp = st["spend"]
    print(dim(f"\nClaude spend ${sp['spent_usd']:.4f} of ${sp['cap_usd']:.2f} · Darwin has run {st['activity']['flagsmith_writes']} "
              f"Flagsmith writes · flag: {st['links']['flag']}"))


def cmd_round(args) -> None:
    from sim.run_round import print_summary, run_round

    settings = Settings.load()
    print(f"Sending {args.chats} simulated customers to ShopBot at {settings.shopbot_url} …")
    summary = run_round(chats=args.chats, shopbot_url=settings.shopbot_url, data_dir=settings.data_dir,
                        concurrency=args.concurrency, seed=args.seed, scenario=args.scenario)
    print_summary(summary)


def cmd_evolve(args) -> None:
    print(f"The Coach is reading the champion's conversations (goal: {args.goal}) …")
    card = steward().propose(goal=args.goal, actor=actor_of(args))
    print_card(card)


def cmd_propose(args) -> None:
    prompt = Path(args.prompt_file).read_text()
    card = steward().propose_manual(system_prompt=prompt, goal=args.goal, summary=args.summary, hypothesis=args.hypothesis,
                                    actor=actor_of(args), effort=args.effort, model=args.model)
    print_card(card)


def cmd_approve(args) -> None:
    s = steward()
    card = s.store.read()["pending"]
    if not card:
        sys.exit("Nothing is waiting for approval.")
    print_card(card)
    if not args.yes:
        answer = input(bold(f"\nType 'approve' to run these {len(card['commands'])} commands as {actor_of(args)}: "))
        if answer.strip().lower() != "approve":
            sys.exit("Not approved. Nothing changed.")
    out = s.approve(actor_of(args), card_id=card["id"])
    print(green("Done. ") + ", ".join(f"{k}: {v}" for k, v in out.items()))


def cmd_reject(args) -> None:
    card = steward().reject(actor_of(args), note=args.note)
    if card:
        print("The Coach revised the proposal:\n")
        print_card(card)
    else:
        print("Rejected. Nothing changed in Flagsmith.")


def cmd_check(args) -> None:
    out = steward().check(actor_of(args), refresh=not args.no_refresh)
    if not out["running"]:
        print("No experiment is running.")
        return
    d = out["decision"]
    print(VERDICT_COLOR.get(d["verdict"], bold)(d["verdict"]) + "  " + d["headline"])
    for r in d["reasons"]:
        print("  · " + r)
    if out.get("note"):
        print(dim(out["note"]))
    if d["needs_approval"]:
        print()
        print_card(steward().store.read()["pending"])


def cmd_watch(args) -> None:
    s = steward()
    deadline = time.time() + args.max_minutes * 60
    while time.time() < deadline:
        out = s.check(actor_of(args))
        if not out["running"]:
            print("No experiment is running.")
            return
        d = out["decision"]
        print(time.strftime("%H:%M:%S ") + VERDICT_COLOR.get(d["verdict"], bold)(f"{d['verdict']:<15}") + d["headline"], flush=True)
        if d["needs_approval"]:
            print()
            print_card(s.store.read()["pending"])
            return
        time.sleep(args.every)


def cmd_stop(args) -> None:
    out = steward().abort(actor_of(args), args.reason)
    print(f"Stopped experiment #{out['stopped']}. Everyone gets the champion.")


def cmd_rollback(args) -> None:
    out = steward().rollback(args.generation, actor_of(args))
    print(f"{out['champion']} is the champion again. Everyone gets it now — no redeploy.")


def cmd_unlock(args) -> None:
    busy = steward().unlock(actor_of(args))
    if not busy:
        print("Nothing was holding Darwin.")
        return
    since = time.strftime("%H:%M:%S", time.localtime(busy.get("since") or 0))
    print(f"Cleared: {busy['what']} (since {since}).")
    if busy.get("alive"):
        print(yellow(f"Process {busy['pid']} is still running. If it finishes, its changes still apply."))


def cmd_spend(args) -> None:
    from .llm import SpendLedger

    settings = Settings.load()
    snap = SpendLedger(settings.data_dir / "spend.json", settings.budget_usd).snapshot()
    print(f"${snap['spent_usd']:.4f} of ${snap['cap_usd']:.2f} spent in {snap['calls']} calls (${snap['remaining_usd']:.4f} left)")
    for purpose, v in sorted(snap["by_purpose"].items()):
        print(f"  {purpose:<16} ${v['usd']:.4f}  {v['calls']} calls")


def cmd_lab(args) -> None:
    import uvicorn

    settings = Settings.load()
    print(f"Darwin Lab on http://{settings.lab_host}:{settings.lab_port}")
    uvicorn.run("darwin.lab.app:create_app", factory=True, host=settings.lab_host, port=settings.lab_port, log_level="warning")


def cmd_shopbot(args) -> None:
    import uvicorn

    url = Settings.load().shopbot_url
    host, port = url.split("//")[1].split(":")
    print(f"ShopBot on {url}")
    uvicorn.run("shopbot.app:create_app", factory=True, host=host, port=int(port), log_level="warning")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="darwin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(fn=fn)
        p.add_argument("--as", dest="actor", help="who is approving (default: $DARWIN_APPROVER or your user name)")
        return p

    add("init", cmd_init, "create the flag and metrics in Flagsmith; register gen1 as champion")
    add("status", cmd_status, "lineage, running experiment and anything waiting for you")
    r = add("round", cmd_round, "send simulated customers to ShopBot")
    r.add_argument("--chats", type=int, default=150)
    r.add_argument("--concurrency", type=int, default=6)
    r.add_argument("--seed", type=int)
    r.add_argument("--scenario")
    e = add("evolve", cmd_evolve, "ask the Coach for a challenger")
    e.add_argument("--goal", choices=["quality", "cost"], default="quality")
    pr = add("propose", cmd_propose, "propose your own challenger prompt; it faces the same experiment and guardrails")
    pr.add_argument("--prompt-file", required=True)
    pr.add_argument("--goal", choices=["quality", "cost"], default="quality")
    pr.add_argument("--summary", required=True)
    pr.add_argument("--hypothesis", required=True)
    pr.add_argument("--effort", choices=["low", "medium", "high"])
    pr.add_argument("--model")
    a = add("approve", cmd_approve, "approve the waiting card (launch, ship, discard or kill)")
    a.add_argument("--yes", action="store_true", help="don't ask to type 'approve'")
    j = add("reject", cmd_reject, "reject the waiting card; with --note the Coach revises it")
    j.add_argument("--note")
    ch = add("check", cmd_check, "refresh Flagsmith results and apply the decision rules")
    ch.add_argument("--no-refresh", action="store_true")
    w = add("watch", cmd_watch, "check every minute until a decision needs you")
    w.add_argument("--every", type=int, default=60)
    w.add_argument("--max-minutes", type=int, default=90)
    stp = add("stop", cmd_stop, "emergency stop: end the experiment, keep the champion")
    stp.add_argument("--reason", default="")
    rb = add("rollback", cmd_rollback, "make an earlier generation the champion again")
    rb.add_argument("generation")
    add("unlock", cmd_unlock, "clear a stuck 'busy' flag (Darwin also clears it when its process exits or after 15 minutes)")
    add("spend", cmd_spend, "Claude API spend against the budget")
    add("lab", cmd_lab, "serve the Darwin Lab dashboard")
    add("shopbot", cmd_shopbot, "serve ShopBot")

    args = parser.parse_args(argv)
    from .steward import StewardError
    from .flagsmith_admin import FlagsmithError
    from .llm import BudgetExceeded

    try:
        args.fn(args)
    except (StewardError, FlagsmithError, BudgetExceeded) as exc:
        sys.exit(red("darwin: ") + str(exc))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
