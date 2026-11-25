"""The Steward runs the evolution loop. Nothing that touches live traffic happens without a human's approval.

    observe ─► propose (Coach) ─► [approve] launch ─► check (Flagsmith results + rules) ─► [approve] ship / discard / kill

Approval cards carry the evidence and the exact Flagsmith CLI commands an approval will run.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from . import transcripts
from .coach import GOALS, Coach, Proposal, dumps, proposal_to_genome
from .config import Settings
from .flagsmith_admin import Command, FlagsmithAdmin, FlagsmithError, Throttled
from .genome import Genome, GenomeError
from .llm import Claude, make_claude, price_of
from .notebook import Notebook
from .results import CONTROL, parse_readout
from .rules import DISCARD, KILL, LABELS, SHIP, DecisionPolicy, Guardrail, decide
from .state import StateStore, log_event

METRICS = {
    "resolved": dict(event="resolved", aggregation="occurrence", direction="up",
                     description="ShopBot handled the chat correctly by itself"),
    "escalated": dict(event="escalated", aggregation="occurrence", direction="down",
                      description="The chat was handed to a human"),
    "cost_microusd": dict(event="cost_microusd", aggregation="sum", direction="down",
                          description="Model cost of the chat, in millionths of a US dollar"),
}
EXPERIMENT_METRICS = {
    "quality": [("resolved", "increase"), ("escalated", "not_increase"), ("cost_microusd", "not_increase")],
    "cost": [("cost_microusd", "decrease"), ("resolved", "not_decrease"), ("escalated", "not_increase")],
}
# Guardrail margins per goal. Rates are small, so occurrence guardrails use absolute points (0.05 = 5 points);
# cost uses a relative margin. A guardrail must be proven within its margin before a winner ships.
POLICIES = {
    "quality": DecisionPolicy(primary="resolved", guardrails=(
        Guardrail("escalated", margin=0.05, worse="up", scale="points", label="escalations", plural=True),
        Guardrail("cost_microusd", margin=0.30, worse="up", scale="relative", label="cost per chat"))),
    "cost": DecisionPolicy(primary="cost_microusd", guardrails=(
        Guardrail("resolved", margin=0.05, worse="down", scale="points", label="resolution"),
        Guardrail("escalated", margin=0.05, worse="up", scale="points", label="escalations", plural=True))),
}
SPLIT = 50  # % of traffic the challenger gets
STOP = "STOP"  # an emergency stop: complete the experiment and keep the champion
APPROVER = "<approver>"  # stands for the approving human on a card; their name replaces it when the commands run
VERBS = {SHIP: "Shipped", DISCARD: "Discarded", KILL: "Killed", STOP: "Stopped"}


class StewardError(RuntimeError):
    pass


REFRESH_INTERVAL = 300  # Flagsmith's RESULTS_REFRESH_MIN_INTERVAL
MIN_CHATS_FOR_RESULTS = 50  # Flagsmith shows no inference until each arm has 50 identities
FINAL_REFRESH_ATTEMPTS = 3


def refresh_wait_seconds(refresh_requested_at: str | None, now: float | None = None) -> int:
    """Seconds until Flagsmith will accept another results refresh for this experiment."""
    if not refresh_requested_at:
        return 0
    from datetime import datetime

    requested = datetime.fromisoformat(refresh_requested_at.replace("Z", "+00:00")).timestamp()
    return max(0, int(requested + REFRESH_INTERVAL - (now or time.time())) + 1)


def _now() -> float:
    return time.time()


BUSY_TTL = 15 * 60  # a sequence or Coach call that holds `busy` longer than this is treated as dead


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def busy_is_stale(busy: dict | None, now: float | None = None) -> bool:
    """`busy` is stale when the process that set it has exited or it is older than BUSY_TTL."""
    if not busy:
        return False
    if (now or time.time()) - float(busy.get("since") or 0) > BUSY_TTL:
        return True
    pid = busy.get("pid")
    return bool(pid) and not _pid_alive(pid)


class Steward:
    def __init__(self, settings: Settings, *, admin: FlagsmithAdmin | None = None, claude: Claude | None = None,
                 coach: Coach | None = None, store: StateStore | None = None, notebook: Notebook | None = None,
                 transcripts_path: Path | None = None, wait_for_results: float = 20.0):
        self.settings = settings
        self.flag = settings.flag
        self.admin = admin or FlagsmithAdmin(settings)
        self.claude = claude or make_claude(settings.data_dir, settings.budget_usd)
        self.coach = coach or Coach(self.claude, model=settings.coach_model, allow_model_change=settings.allow_model_change)
        self.store = store or StateStore(settings.data_dir / "lab.json", settings.flag)
        self.notebook = notebook or Notebook(settings.notebook_path, settings.flag)
        self.transcripts_path = transcripts_path or settings.data_dir / "transcripts.jsonl"
        self.proposals_dir = settings.data_dir / "proposals"
        self.wait_for_results = wait_for_results

    # ------------------------------------------------------------------ helpers
    def _genome(self, state: dict, name: str) -> Genome:
        return Genome.model_validate(state["generations"][name]["genome"])

    def _champion(self, state: dict) -> Genome:
        if not state.get("champion"):
            raise StewardError("Darwin isn't initialised yet — run `darwin init`.")
        return self._genome(state, state["champion"])

    def _next_name(self, state: dict, taken: list[str] = ()) -> str:
        """The next free generation name. `taken` are the variant keys Flagsmith already has: a key that exists
        there may hold another genome, so it is never reused."""
        names = [*state["generations"], *taken]
        numbers = [int(n[3:]) for n in names if n.startswith("gen") and n[3:].isdigit()]
        return f"gen{max(numbers, default=0) + 1}"

    def _variant_keys(self) -> list[str]:
        return [v["key"] for v in self._feature().get("variants") or []]

    @staticmethod
    def _served_value(feature: dict) -> str:
        """What everyone outside an experiment gets: a variant rolled out to 100%, else the default value."""
        full = [v for v in feature.get("variants") or [] if float(v.get("weight") or 0) >= 100]
        return full[0]["value"] if full else feature["default_value"]

    @staticmethod
    def _expire_busy(state: dict) -> None:
        if busy_is_stale(state.get("busy")):
            stale = state["busy"]
            state["busy"] = None
            log_event(state, "unlocked", f"Cleared a stale busy flag ({stale.get('what')}): its process is gone or it ran over "
                                         f"{BUSY_TTL // 60} minutes")

    @contextmanager
    def _claim(self, prepare: Callable[[dict], str]) -> Iterator[None]:
        """Check-and-set `busy` in one transaction, so the CLI and the Lab can't both start a sequence.

        `prepare(state)` checks the state (raising StewardError to refuse) and returns what Darwin is doing.
        `busy` is cleared on any exit, Ctrl-C included.
        """
        token = uuid.uuid4().hex
        with self.store.transaction() as s:
            self._expire_busy(s)
            if s.get("busy"):
                raise StewardError(f"busy: {s['busy']['what']} (`darwin unlock` clears it if that is stuck)")
            what = prepare(s)
            s["busy"] = {"what": what, "since": _now(), "pid": os.getpid(), "token": token}
        try:
            yield
        finally:
            with self.store.transaction() as s:
                if (s.get("busy") or {}).get("token") == token:
                    s["busy"] = None

    @staticmethod
    def _dismissed(exp: dict, verdict: str, as_of: str | None) -> bool:
        """"Keep running" holds for the results it was made on. New results from Flagsmith bring the card back."""
        d = exp.get("dismissed")
        if isinstance(d, str):  # older lab.json files stored only the verdict
            d = {"verdict": d, "as_of": None}
        return bool(d) and d.get("verdict") == verdict and d.get("as_of") == as_of

    def _busy_now(self, state: dict) -> dict | None:
        busy = state.get("busy")
        return None if busy_is_stale(busy) else busy

    def _feature(self) -> dict:
        """The feature's id plus this environment's live value and variant weights.

        `flagsmith feature get` reports project-level defaults (the value the flag was created with);
        `flagsmith flag get` reports what this environment actually serves.
        """
        feature = self.admin.feature(self.flag)
        if feature is None:
            raise StewardError(f"flag {self.flag} doesn't exist in Flagsmith — run `darwin init`.")
        flag = self.admin.flag(self.flag) or {}
        merged = dict(feature)
        merged["default_value"] = flag.get("value", feature.get("default_value"))
        if flag.get("variants") is not None:
            merged["variants"] = flag["variants"]
        return merged

    def _same_plan(self, card: dict, build: Callable[[str], list[Command]], changed: str | None = None) -> list[Command]:
        """Approval runs exactly the commands the human saw: every summary, command line and JSON body.

        `build(card_id)` makes the plan from Flagsmith's current state. If it differs from the card, the card is
        replaced (new id, new commands) and the approval stops, so the human reviews what would really run.
        """
        plan = build(card["id"])
        if card["commands"] == [c.to_dict() for c in plan]:
            return plan
        new_id = uuid.uuid4().hex[:8]
        fresh = [c.to_dict() for c in build(new_id)]
        with self.store.transaction() as st:
            if st["pending"] and st["pending"]["id"] == card["id"]:
                st["pending"]["commands"] = fresh
                st["pending"]["id"] = new_id
                log_event(st, "card-updated", "Flagsmith changed since the card was made; its commands were updated for review")
        raise StewardError(changed or "Flagsmith changed since this card was made, so the commands changed. "
                                      "Review the updated card and approve again.")

    def _stats(self, state: dict, name: str) -> dict:
        # A genome never changes, so every chat it served — as challenger or champion — is evidence about it.
        return transcripts.stats(transcripts.load(self.transcripts_path, generation=name))

    # ------------------------------------------------------------------ init
    def init(self, gen1: Genome, actor: str = "darwin") -> list[str]:
        missing = self.settings.missing_for_admin()
        if missing:
            raise StewardError("missing settings: " + ", ".join(missing))
        notes = []
        if self.admin.feature(self.flag) is None:
            self.admin.run(self.admin.cmd_create_feature(self.flag, gen1.to_flag_value(), f"{gen1.name} genome"))
            notes.append(f"created flag {self.flag} with {gen1.name} as its value")
        else:
            notes.append(f"flag {self.flag} already exists")
        feature = self._feature()  # the environment's live value, not the value the flag was created with
        existing = {m["name"]: m["id"] for m in self.admin.metrics()}
        for name, spec in METRICS.items():
            if name not in existing:
                created = self.admin.run(self.admin.cmd_create_metric(name, spec["event"], spec["aggregation"],
                                                                     spec["direction"], spec["description"]))
                existing[name] = created["id"]
                notes.append(f"created metric {name}")
        try:
            current = Genome.from_flag_value(self._served_value(feature))
        except GenomeError as exc:
            raise StewardError(f"{self.flag} serves a value Darwin can't read as a genome ({exc}). Fix the flag in Flagsmith first.") from exc
        with self.store.transaction() as s:
            s["feature_id"] = feature["id"]
            s["metrics"] = {n: existing[n] for n in METRICS}
            if s["champion"] and s["champion"] != current.name:
                notes.append(f"Flagsmith serves {current.name} but Darwin's champion is {s['champion']}; "
                             f"`darwin rollback {s['champion']}` makes them match")
            if s["champion"] is None:
                s["generations"][current.name] = {"genome": current.model_dump(), "status": "champion", "born_at": _now(),
                                                  "became_champion_at": _now(), "parent": current.parent, "summary": current.summary}
                s["champion"] = current.name
                log_event(s, "init", f"{current.name} is the champion; Darwin is watching {self.flag}", actor)
                self.notebook.champion(current.model_dump(), "Baseline. Darwin starts watching this generation.")
                notes.append(f"{current.name} is the champion")
        return notes

    # ------------------------------------------------------------------ propose
    def propose(self, goal: str = "quality", actor: str = "darwin") -> dict:
        if goal not in GOALS:
            raise StewardError(f"unknown goal {goal!r}; use one of {', '.join(GOALS)}")
        taken = self._variant_keys()
        ctx: dict = {}

        def prepare(s: dict) -> str:
            if s["experiment"]:
                raise StewardError("an experiment is running — decide it before proposing the next generation")
            if s["pending"]:
                raise StewardError("a proposal is already waiting for approval")
            ctx.update(champion=self._champion(s), name=self._next_name(s, taken), history=self._history_lines(s))
            return f"The Coach is reading {ctx['champion'].name}'s conversations"

        with self._claim(prepare):
            champion, name, history = ctx["champion"], ctx["name"], ctx["history"]
            rows = transcripts.load(self.transcripts_path, generation=champion.name)
            if not rows:
                raise StewardError(f"no conversations for {champion.name} yet — run a round of traffic first")
            stats = transcripts.stats(rows)
            failed, ok = transcripts.sample_for_coach(rows, failed=24, succeeded=4 if goal == "quality" else 10, seed=len(rows))
            if goal == "quality" and not failed:
                raise StewardError(f"{champion.name} resolved every chat it was given — try --goal cost")
            from shopbot.tools import TOOLS  # the agent's tool catalogue, shown to the Coach

            spent_before = self.claude.ledger.snapshot()["spent_usd"]
            proposal, request = self.coach.propose(goal=goal, champion=champion, challenger_name=name, tools=TOOLS,
                                                   stats=stats, history=history, failed=failed, succeeded=ok)
            coach_cost = self.claude.ledger.snapshot()["spent_usd"] - spent_before
            genome = proposal_to_genome(proposal, champion, name)
            self.proposals_dir.mkdir(parents=True, exist_ok=True)
            request_path = self.proposals_dir / f"{name}-request.md"
            request_path.write_text(request)
            card = self._launch_card(champion, genome, proposal, goal, stats, len(failed) + len(ok), coach_cost, request_path)
            with self.store.transaction() as s:
                s["generations"][name] = {"genome": genome.model_dump(), "status": "proposed", "born_at": _now(),
                                          "parent": champion.name, "goal": goal, "summary": genome.summary}
                s["pending"] = card
                log_event(s, "proposed", f"Coach proposed {name}: {genome.summary}", "coach", generation=name,
                          chats_read=card["chats_read"], coach_cost_usd=card["coach_cost_usd"],
                          expected_effect=card["proposal"]["expected_effect"])
        return card

    def _expected(self, state: dict, name: str) -> str | None:
        if state["generations"].get(name, {}).get("expected_effect"):
            return state["generations"][name]["expected_effect"]
        for event in reversed(state["history"]):
            if event.get("kind") == "proposed" and event.get("generation") == name:
                return event.get("expected_effect")
        return None

    def propose_manual(self, *, system_prompt: str, goal: str, summary: str, hypothesis: str, actor: str,
                       effort: str | None = None, model: str | None = None) -> dict:
        """A human-written challenger. It goes through the same experiment, rules and approval as the Coach's."""
        from .coach import Proposal, check_proposal

        if goal not in GOALS:
            raise StewardError(f"unknown goal {goal!r}; use one of {', '.join(GOALS)}")
        taken = self._variant_keys()
        with self.store.transaction() as s:
            self._expire_busy(s)
            if s["experiment"] or s["pending"] or s["busy"]:
                raise StewardError("finish the running experiment or pending card first")
            champion = self._champion(s)
            name = self._next_name(s, taken)
            history = self._history_lines(s)
        proposal = Proposal(patterns=[], hypothesis=hypothesis, expected_effect=f"Proposed by {actor}; not predicted by the Coach.",
                            changes_summary=[f"Prompt written by {actor}"], risks="Not reviewed by the Coach.", summary=summary,
                            system_prompt=system_prompt, effort=effort or champion.effort, model=model or champion.model)
        problems = check_proposal(proposal, champion, goal, allow_model_change=self.settings.allow_model_change)
        if problems:
            raise StewardError("proposal failed Darwin's checks: " + "; ".join(problems))
        genome = proposal_to_genome(proposal, champion, name)
        rows = transcripts.load(self.transcripts_path, generation=champion.name)
        stats = transcripts.stats(rows)
        # The same request the Coach would get, so "Reject with a note" can ask the Coach to revise this prompt.
        request_path = self._write_request(goal, champion, name, rows, history)
        card = self._launch_card(champion, genome, proposal, goal, stats, 0, 0.0, request_path)
        card["author"] = actor
        with self.store.transaction() as s:
            s["generations"][name] = {"genome": genome.model_dump(), "status": "proposed", "born_at": _now(),
                                      "parent": champion.name, "goal": goal, "summary": genome.summary, "author": actor}
            s["pending"] = card
            log_event(s, "proposed", f"{actor} proposed {name}: {genome.summary}", actor, generation=name, chats_read=0,
                      coach_cost_usd=0, expected_effect=None)
        return card

    def _write_request(self, goal: str, champion: Genome, name: str, rows: list[dict], history: list[str]) -> Path:
        from shopbot.tools import TOOLS

        from .coach import build_request

        failed, ok = transcripts.sample_for_coach(rows, failed=24, succeeded=4 if goal == "quality" else 10, seed=len(rows))
        request = build_request(goal=goal, champion=champion, challenger_name=name, tools=TOOLS, stats=transcripts.stats(rows),
                                history=history, failed=failed, succeeded=ok, allow_model_change=self.settings.allow_model_change)
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        path = self.proposals_dir / f"{name}-request.md"
        path.write_text(request)
        return path

    def _history_lines(self, state: dict) -> list[str]:
        lines = []
        for name, g in state["generations"].items():
            if not g.get("parent"):
                continue
            result = g.get("result", {})
            outcome = g["status"] + (f" — {result.get('headline')}" if result.get("headline") else "")
            lines.append(f"{name} (from {g['parent']}, goal {g.get('goal', 'quality')}): \"{g.get('summary', '')}\" → {outcome}")
        return lines

    def _launch_card(self, champion: Genome, genome: Genome, proposal: Proposal, goal: str, stats: dict,
                     chats_read: int, coach_cost: float, request_path: Path) -> dict:
        card_id = uuid.uuid4().hex[:8]
        return {
            "kind": "launch", "id": card_id, "created_at": _now(), "goal": goal, "goal_text": GOALS[goal], "author": "coach",
            "champion": champion.name, "challenger": genome.name, "genome": genome.model_dump(),
            "diff": champion.diff(genome), "proposal": dumps(proposal), "stats": stats, "chats_read": chats_read,
            "coach_cost_usd": round(coach_cost, 4), "request_path": str(request_path),
            "commands": [c.to_dict() for c in self._launch_plan(champion, genome, goal, proposal.hypothesis, card_id=card_id)],
            "metrics": [f"{m} ({d.replace('_', ' ')})" for m, d in EXPERIMENT_METRICS[goal]],
            "plan": self._experiment_plan(goal),
            "cost": self._projected_cost(champion, genome, stats),
        }

    def _projected_cost(self, champion: Genome, challenger: Genome, stats: dict) -> dict:
        """An estimate of what the planned experiment costs in Claude calls: the planned chats per arm times the
        champion's observed cost per chat, scaled by the model price ratio for the challenger. Effort and prompt
        length changes aren't priced in."""
        per_arm = self.settings.min_per_arm
        snap = self.claude.ledger.snapshot()
        out = {"per_arm": per_arm, "chats": 2 * per_arm, "usd": None, "remaining_usd": snap["remaining_usd"],
               "cap_usd": snap["cap_usd"]}
        per_chat = (stats or {}).get("avg_cost_usd")
        if not per_chat:
            out["text"] = f"Projected Claude cost: unknown ({champion.name} has no chats yet) · ${snap['remaining_usd']:.2f} " \
                          f"of ${snap['cap_usd']:.2f} left"
            return out
        ratio = max(price_of(challenger.model)[i] / price_of(champion.model)[i] for i in (0, 1))
        usd = per_arm * per_chat * (1 + ratio)
        out.update(usd=round(usd, 4), ratio=round(ratio, 3), per_chat_usd=per_chat, fits=usd <= snap["remaining_usd"])
        model = f", ×{ratio:g} for {challenger.model}" if ratio != 1 else ""
        out["text"] = (f"Projected Claude cost: about ${usd:.2f} for {2 * per_arm} chats (estimate: ${per_chat:.5f} per chat"
                       f"{model}) · ${snap['remaining_usd']:.2f} of ${snap['cap_usd']:.2f} left")
        return out

    def _check_budget(self, card: dict) -> None:
        """Refuse a launch whose planned traffic would run past the Claude budget (only when the cost is known)."""
        champion, challenger = Genome.model_validate(self._genome_dict(card["champion"])), Genome.model_validate(card["genome"])
        cost = self._projected_cost(champion, challenger, card.get("stats") or {})
        if cost["usd"] is not None and not cost["fits"]:
            raise StewardError(f"This experiment needs about ${cost['usd']:.2f} of Claude calls ({cost['chats']} chats), but only "
                               f"${cost['remaining_usd']:.2f} of the ${cost['cap_usd']:.2f} budget is left. Raise "
                               f"DARWIN_BUDGET_USD or lower DARWIN_MIN_PER_ARM, then approve again. Nothing ran.")

    def _genome_dict(self, name: str) -> dict:
        return self.store.read()["generations"][name]["genome"]

    def _experiment_plan(self, goal: str) -> dict:
        """What will decide the experiment, fixed at launch: the sample per arm and every rule."""
        policy = POLICIES[goal]
        per_arm = self.settings.min_per_arm
        return {
            "min_per_arm": per_arm,
            "rules": [f"Darwin calls a winner or a loser once each arm has {per_arm} chats (a guardrail breach kills sooner)",
                      f"ship: at least {policy.ship_at:.0%} chance {LABELS.get(policy.primary, policy.primary)} is better, "
                      f"and every guardrail proven within its margin",
                      f"discard: {policy.discard_at:.0%} chance or less",
                      *[f"kill: {g.describe()} (proven at {policy.breach_at:.0%})" for g in policy.guardrails]],
        }

    # ------------------------------------------------------------------ launch
    def _launch_plan(self, champion: Genome, challenger: Genome, goal: str, hypothesis: str, *, card_id: str = "",
                     approver: str = APPROVER, feature: dict | None = None, option_id: int | None = None,
                     experiment_id: int | None = None, metric_ids: dict | None = None) -> list[Command]:
        feature = feature or self._feature()
        variants = [v for v in feature.get("variants") or [] if v["key"] != challenger.name]
        existing = next((v for v in feature.get("variants") or [] if v["key"] == challenger.name), None)
        if existing is not None and existing.get("value") != challenger.to_flag_value():
            raise StewardError(f"Flagsmith already has a variant called {challenger.name} with a different genome. Darwin won't "
                               f"test one genome and ship another: reject this card and propose again (the new proposal "
                               f"gets a free name).")
        champ_value = champion.to_flag_value()
        plan = []
        if feature.get("default_value") != champ_value or any(v.get("weight") for v in variants):
            plan.append(self.admin.cmd_set_default(self.flag, champ_value, champion.name, [v["key"] for v in variants]))
        if existing is None:
            plan.append(self.admin.cmd_add_variant(self.flag, challenger.name, challenger.to_flag_value()))
        elif option_id is None:
            option_id = existing["id"]  # added by an earlier, half-finished launch of this same genome
        new_id = option_id if option_id is not None else f"<{challenger.name} variant id>"
        weights = {v["id"]: 0 for v in variants} | {new_id: SPLIT}
        labels = {v["id"]: v["key"] for v in variants} | {new_id: challenger.name}
        metric_ids = metric_ids or self.store.read()["metrics"]
        metrics = [{"metric": metric_ids.get(m, f"<{m} metric id>"), "expected_direction": d} for m, d in EXPERIMENT_METRICS[goal]]
        if card_id:
            hypothesis = f"{hypothesis}\n\nApproved in Darwin by {approver} (card {card_id})."
        plan.append(self.admin.cmd_create_experiment(
            feature_id=feature["id"], name=self._experiment_name(champion.name, challenger.name), hypothesis=hypothesis,
            metrics=metrics, control_value=champ_value, control_label=champion.name, weights=weights, weight_labels=labels))
        plan.append(self.admin.cmd_start(experiment_id if experiment_id is not None else "<new experiment id>"))
        return plan

    def _experiment_name(self, champion: str, challenger: str) -> str:
        return f"{self.flag}: {champion} vs {challenger}"

    def _active_experiment(self, name: str) -> dict:
        """After a 409: the active experiment Flagsmith has for this flag must be ours, matched by its exact name."""
        active = [e for e in self.admin.experiments() if e.get("status") != "completed"
                  and (e.get("feature") or {}).get("name", self.flag) == self.flag]
        mine = [e for e in active if e.get("name") == name]
        if mine:
            return mine[0]  # an earlier, half-finished launch of this same card
        other = ", ".join(f"#{e['id']} '{e.get('name')}' ({e.get('status')})" for e in active) or "one Darwin can't see"
        raise StewardError(f"Flagsmith already has an active experiment on {self.flag}: {other}. "
                           f"Complete or delete it in Flagsmith, then approve again.")

    def _launch(self, card: dict, actor: str) -> dict:
        self._check_budget(card)
        s = self.store.read()
        champion = self._champion(s)
        challenger = Genome.model_validate(card["genome"])
        hypothesis = card["proposal"]["hypothesis"]
        feature = self._feature()
        plan = self._same_plan(card, lambda cid: self._launch_plan(champion, challenger, card["goal"], hypothesis, card_id=cid,
                                                                   feature=feature, metric_ids=s["metrics"]))
        for cmd in plan[:-2]:  # set the champion as default (if needed), add the challenger variant (if needed)
            self.admin.run(cmd)
        if plan[:-2]:
            feature = self._feature()
        option_id = next((v["id"] for v in feature.get("variants") or [] if v["key"] == challenger.name), None)
        if option_id is None:
            raise StewardError(f"Flagsmith doesn't show the {challenger.name} variant after adding it; approve again to retry.")
        plan = self._launch_plan(champion, challenger, card["goal"], hypothesis, card_id=card["id"], approver=actor,
                                 feature=feature, option_id=option_id, metric_ids=s["metrics"])
        try:
            experiment = self.admin.run(plan[-2])
        except FlagsmithError as exc:
            if exc.status != 409:
                raise
            experiment = self._active_experiment(self._experiment_name(champion.name, challenger.name))
        experiment_id = experiment["id"]
        if experiment.get("status") in (None, "created", "paused"):
            self.admin.run(self.admin.cmd_start(experiment_id))
        with self.store.transaction() as st:
            gen = st["generations"][challenger.name]
            gen.update(status="challenger", option_id=option_id, experiment_id=experiment_id, launched_at=_now(), launched_by=actor,
                       hypothesis=card["proposal"]["hypothesis"], expected_effect=card["proposal"]["expected_effect"])
            st["experiment"] = {"id": experiment_id, "champion": champion.name, "challenger": challenger.name,
                                "goal": card["goal"], "split": {CONTROL: (100 - SPLIT) / 100, challenger.name: SPLIT / 100},
                                "started_at": _now(), "launched_by": actor, "option_id": option_id,
                                "min_per_arm": (card.get("plan") or {}).get("min_per_arm", self.settings.min_per_arm),
                                "card_id": card["id"], "readout": None, "decision": None, "dismissed": None}
            st["pending"] = None
            log_event(st, "launched", f"{actor} approved {challenger.name}; experiment #{experiment_id} is running "
                                      f"({champion.name} vs {challenger.name}, {SPLIT}/{100 - SPLIT})", actor,
                      generation=challenger.name, experiment_id=experiment_id)
        self.notebook.launched(card, experiment_id, actor)
        return {"experiment_id": experiment_id, "challenger": challenger.name}

    # ------------------------------------------------------------------ check
    def arm_counts(self, exp: dict) -> dict[str, int]:
        """Chats per arm that ShopBot recorded for this experiment, from the local transcripts."""
        counts = {CONTROL: 0, exp["challenger"]: 0}
        for row in transcripts.load(self.transcripts_path, since=exp.get("started_at")):
            if row.get("experiment_id") == exp["id"] and row.get("variant") in counts:
                counts[row["variant"]] += 1
        return counts

    def check(self, actor: str = "darwin", refresh: bool | str = True) -> dict:
        """Read Flagsmith's results and apply the rules.

        refresh=True asks Flagsmith to recompute (at most once every 5 minutes). refresh="auto" asks only once the
        transcripts show 50 chats per arm and new chats since the last request, so the 5-minute window isn't spent
        on results Flagsmith can't compute yet.
        """
        s = self.store.read()
        exp = s["experiment"]
        if not exp:
            return {"running": False, "finalized": self._finalize_results(s)}
        if self._busy_now(s):
            return {"running": True, "busy": True, "readout": exp.get("readout"), "decision": exp.get("decision"),
                    "note": f"Darwin is busy ({s['busy']['what']}); results weren't checked this time."}
        detail = self._experiment_detail(exp["id"])
        if detail is None or detail.get("status") == "completed":
            try:
                out = self.reconcile(actor)
            except StewardError as exc:  # another action claimed Darwin in the meantime
                return {"running": True, "busy": True, "note": str(exc), "readout": exp.get("readout"),
                        "decision": exp.get("decision")}
            return {"running": not out["settled"], "reconciled": out, "note": out["note"],
                    "readout": exp.get("readout"), "decision": exp.get("decision")}
        if exp.get("deciding"):
            # An approval stopped before `complete` took effect (Flagsmith still shows the experiment open).
            with self.store.transaction() as st:
                e = st["experiment"]
                if e and e["id"] == exp["id"] and not self._busy_now(st):
                    e["deciding"] = None
        note = None
        if detail.get("status") == "paused":
            note = "Flagsmith shows this experiment as paused: no new traffic is split. Resume it in Flagsmith, or stop it."
        current = self.admin.results(exp["id"]) or {}
        before = current.get("as_of")
        chats = None
        if refresh == "auto":
            counts = self.arm_counts(exp)
            chats = sum(counts.values())
            if min(counts.values()) < MIN_CHATS_FOR_RESULTS:
                refresh = False
                arms = ", ".join(f"{exp['champion'] if k == CONTROL else k} {v}" for k, v in counts.items())
                note = note or (f"Darwin asks Flagsmith to recompute once each arm has {MIN_CHATS_FOR_RESULTS} chats "
                                f"(so far: {arms}).")
            elif chats <= (exp.get("refreshed_at_chats") or 0):
                refresh = False  # nothing new since the last recompute
        wait = refresh_wait_seconds(current.get("refresh_requested_at"))
        if refresh and wait > 0:
            refresh = False
            note = note or f"Flagsmith recomputes results at most every 5 minutes; next refresh in {wait // 60}m {wait % 60:02d}s."
        if refresh:
            try:
                self.admin.run(self.admin.cmd_refresh_results(exp["id"]))
                if chats is not None:
                    with self.store.transaction() as st:
                        if st["experiment"] and st["experiment"]["id"] == exp["id"]:
                            st["experiment"]["refreshed_at_chats"] = chats
                deadline = time.time() + self.wait_for_results
                while time.time() < deadline:
                    if (self.admin.results(exp["id"]) or {}).get("as_of") != before:
                        break
                    time.sleep(1.0)
            except Throttled:
                note = note or "Flagsmith recomputes results at most once every 5 minutes; showing the latest."
        results = self.admin.results(exp["id"])
        readout = parse_readout(detail, results, challenger=exp["challenger"], expected_split=exp["split"])
        decision = decide(readout, POLICIES[exp["goal"]], champion=exp["champion"],
                          min_per_arm=exp.get("min_per_arm", self.settings.min_per_arm))

        def same_card(pending: dict | None) -> bool:
            return bool(pending) and pending.get("kind") == "decision" and pending.get("verdict") == decision.verdict

        # Build a new card (which reads Flagsmith) before taking the state lock; the transaction only stores it.
        latest = self.store.read()
        card = None
        if decision.needs_approval and latest["experiment"] and latest["experiment"]["id"] == exp["id"] \
                and not self._dismissed(latest["experiment"], decision.verdict, readout.as_of) and not same_card(latest["pending"]):
            card = self._decision_card(latest, exp, decision, detail, readout.to_dict())
        with self.store.transaction() as st:
            e = st["experiment"]
            if not e or e["id"] != exp["id"]:
                return {"running": False}
            e["readout"], e["decision"], e["checked_at"], e["note"] = readout.to_dict(), decision.to_dict(), _now(), note
            e["flagsmith_status"] = detail.get("status")
            dismissed = e.get("dismissed")
            if dismissed and (dismissed.get("as_of") if isinstance(dismissed, dict) else None) != readout.as_of:
                e["dismissed"] = None  # Flagsmith has new results since the human chose to keep running
            pending = st["pending"]
            if decision.needs_approval and not self._dismissed(e, decision.verdict, readout.as_of):
                if same_card(pending):
                    pending.update(headline=decision.headline, reasons=decision.reasons, readout=readout.to_dict())
                elif card is not None and (not pending or pending.get("kind") == "decision"):
                    st["pending"] = card
                    log_event(st, "recommended", f"Rules recommend {decision.verdict} for {e['challenger']}: {decision.headline}",
                              "darwin", generation=e["challenger"], experiment_id=e["id"])
            elif pending and pending.get("kind") == "decision" and not decision.needs_approval:
                st["pending"] = None
                log_event(st, "withdrawn", f"Recommendation withdrawn: {decision.headline}", "darwin", experiment_id=e["id"])
        return {"running": True, "readout": readout.to_dict(), "decision": decision.to_dict(), "note": note}

    def _decision_card(self, state: dict, exp: dict, decision, detail: dict, readout: dict | None) -> dict:
        challenger = self._genome(state, exp["challenger"])
        card_id = uuid.uuid4().hex[:8]
        return {"kind": "decision", "id": card_id, "created_at": _now(), "verdict": decision.verdict,
                "headline": decision.headline, "reasons": decision.reasons, "experiment_id": exp["id"],
                "champion": exp["champion"], "challenger": exp["challenger"], "goal": exp["goal"],
                "readout": readout,
                "commands": [c.to_dict() for c in self._decision_plan(exp, decision.verdict, challenger, card_id=card_id,
                                                                      detail=detail)]}

    @staticmethod
    def _record_line(verdict: str, approver: str, card_id: str | None = None, reason: str = "") -> str:
        line = f"{VERBS[verdict]} in Darwin by {approver}" + (f" (card {card_id})" if card_id else "")
        return line + (f": {reason}" if reason else "") + "."

    def _decision_plan(self, exp: dict, verdict: str, challenger: Genome, *, card_id: str | None, approver: str = APPROVER,
                       detail: dict | None = None, reason: str = "") -> list[Command]:
        """The commands that settle the experiment, given what Flagsmith already shows: complete it (unless it is
        completed), make a shipped challenger the flag's default value, and record the decision in the experiment's
        hypothesis so Flagsmith shows who approved it."""
        detail = detail if detail is not None else self.admin.experiment(exp["id"])
        completed = detail.get("status") == "completed"
        plan = []
        if not completed:
            plan.append(self.admin.cmd_complete(exp["id"], "variant", exp["option_id"], challenger.name) if verdict == SHIP
                        else self.admin.cmd_complete(exp["id"], "control"))
        if verdict == SHIP:
            feature = self._feature()
            value = challenger.to_flag_value()
            settled = feature.get("default_value") == value and not any(v.get("weight") for v in feature.get("variants") or [])
            if not completed or not settled:
                plan.append(self.admin.cmd_set_default(self.flag, value, challenger.name,
                                                       [v["key"] for v in feature.get("variants") or []]))
        hypothesis = detail.get("hypothesis") or ""
        if f"{VERBS[verdict]} in Darwin by" not in hypothesis:
            line = self._record_line(verdict, approver, card_id, reason)
            plan.append(self.admin.cmd_update_hypothesis(exp["id"], f"{hypothesis}\n\n{line}".strip(), line))
        return plan

    def _decide(self, card: dict, actor: str) -> dict:
        s = self.store.read()
        exp = s["experiment"]
        if not exp or exp["id"] != card["experiment_id"]:
            raise StewardError("that experiment is no longer running")
        challenger = self._genome(s, exp["challenger"])
        detail = self._experiment_detail(exp["id"])
        intent = exp.get("deciding")
        completed = detail is not None and detail.get("status") == "completed"
        if detail is None or (completed and not self._matches(intent, exp, detail)):
            out = self._reconcile(exp, detail, actor)
            raise StewardError(out["note"] + " Nothing was run.")
        verdict, reason, changed = card["verdict"], "", None
        if completed:  # an approved sequence stopped halfway: finish only what is missing
            verdict, reason = intent["verdict"], intent.get("reason", "")
            changed = ("Flagsmith already completed this experiment, so the card now lists only the approved steps that "
                       "didn't run. Review it and approve again.")
        self._same_plan(card, lambda cid: self._decision_plan(exp, verdict, challenger, card_id=cid, detail=detail, reason=reason),
                        changed=changed)
        self._run_decision(exp, verdict, challenger, card_id=card["id"], actor=actor, detail=detail, reason=reason)
        headline = card.get("original_headline") or card["headline"]
        reasons = card.get("original_reasons") or card["reasons"]
        self._settle_decision(exp, verdict, actor, headline, reasons, s)
        return {"verdict": verdict, "champion": exp["challenger"] if verdict == SHIP else exp["champion"]}

    # ------------------------------------------------------------------ reconciliation
    def _experiment_detail(self, experiment_id: int) -> dict | None:
        """Flagsmith's view of the experiment, or None if it no longer exists there."""
        try:
            return self.admin.experiment(experiment_id)
        except FlagsmithError as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def _matches(intent: dict | None, exp: dict, detail: dict) -> bool:
        """Does Flagsmith's rollout decision match what the human approved?"""
        if not intent:
            return False
        if intent["verdict"] == SHIP:
            return detail.get("rollout_decision") == "variant" and detail.get("rollout_variant") == exp.get("option_id")
        return (detail.get("rollout_decision") or "control") == "control"

    def _run_decision(self, exp: dict, verdict: str, challenger: Genome, *, card_id: str | None, actor: str, detail: dict,
                      reason: str = "") -> None:
        """Write the approved intent first, then run the commands. If they stop halfway, the next check, approval
        or `darwin reconcile` sees the intent and finishes only the missing steps."""
        with self.store.transaction() as st:
            e = st["experiment"]
            if not e or e["id"] != exp["id"]:
                raise StewardError("that experiment is no longer running")
            e["deciding"] = {"verdict": verdict, "card_id": card_id, "actor": actor, "at": _now(), "reason": reason}
        try:
            for cmd in self._decision_plan(exp, verdict, challenger, card_id=card_id, approver=actor, detail=detail, reason=reason):
                self.admin.run(cmd)
        except Exception:
            if detail.get("status") != "completed":
                self._drop_intent_unless_completed(exp)
            raise

    def _drop_intent_unless_completed(self, exp: dict) -> None:
        try:
            now = self._experiment_detail(exp["id"])
        except FlagsmithError:
            return  # can't tell; the next check decides
        if now is not None and now.get("status") != "completed":
            with self.store.transaction() as st:
                if st["experiment"] and st["experiment"]["id"] == exp["id"]:
                    st["experiment"]["deciding"] = None

    def _settle(self, exp: dict, *, challenger_status: str, verdict: str, actor: str, headline: str, reasons: list[str],
                history: str, kind: str = "decided", new_champion: str | None = None) -> bool:
        """Close the experiment in Darwin's state. Returns False if something else already closed it."""
        with self.store.transaction() as st:
            e = st["experiment"]
            if not e or e["id"] != exp["id"]:
                return False
            gen = st["generations"][e["challenger"]]
            gen.update(status=challenger_status, decided_at=_now(), decided_by=actor,
                       result={"verdict": verdict, "headline": headline, "reasons": reasons, "readout": e.get("readout"),
                               "experiment_id": e["id"]})
            if new_champion and new_champion != st["champion"]:
                if st["champion"] in st["generations"]:
                    st["generations"][st["champion"]]["status"] = "retired"
                st["generations"][new_champion].update(status="champion", became_champion_at=_now())
                st["champion"] = new_champion
            st["experiment"] = None
            if st["pending"] and st["pending"].get("kind") == "decision":
                st["pending"] = None
            if e.get("started_at"):
                # Flagsmith marks results final only after a refresh that follows `complete`, and refreshes are
                # 5 minutes apart, so a later check does it once the window opens.
                st.setdefault("finalize", []).append({"experiment_id": e["id"], "generation": e["challenger"],
                                                      "split": e.get("split"), "attempts": 0})
            log_event(st, kind, history, actor, generation=e["challenger"], experiment_id=e["id"], verdict=verdict)
        return True

    def _finalize_results(self, state: dict) -> list[str]:
        """Ask Flagsmith for the final numbers of experiments Darwin settled, once each refresh window opens."""
        notes = []
        for job in list(state.get("finalize") or []):
            eid, attempts, done, final = job["experiment_id"], job.get("attempts", 0), False, None
            try:
                results = self.admin.results(eid)
                if results and results.get("is_final"):
                    done, final = True, results
                elif attempts >= FINAL_REFRESH_ATTEMPTS:
                    done = True  # Flagsmith hasn't produced final numbers after several tries; stop asking
                elif refresh_wait_seconds((results or {}).get("refresh_requested_at")) == 0:
                    self.admin.run(self.admin.cmd_refresh_results(eid))
                    attempts += 1
                    notes.append(f"asked Flagsmith for experiment #{eid}'s final results")
            except Throttled:
                pass
            except FlagsmithError as exc:
                done = exc.status in (400, 404)  # already final, never started, or deleted: nothing more to do
            readout = None
            if final is not None:
                try:
                    readout = parse_readout(self.admin.experiment(eid), final, challenger=job["generation"],
                                            expected_split=job.get("split") or {}).to_dict()
                    notes.append(f"stored experiment #{eid}'s final results")
                except FlagsmithError:
                    readout = None
            with self.store.transaction() as st:
                queue = st.get("finalize") or []
                for j in queue:
                    if j["experiment_id"] == eid:
                        j["attempts"] = attempts
                if done:
                    st["finalize"] = [j for j in queue if j["experiment_id"] != eid]
                    gen = st["generations"].get(job["generation"]) or {}
                    if readout and gen.get("result"):
                        gen["result"]["final_readout"] = readout
        return notes

    def _settle_decision(self, exp: dict, verdict: str, actor: str, headline: str, reasons: list[str], state: dict) -> None:
        status = {SHIP: "champion", DISCARD: "discarded", KILL: "killed", STOP: "stopped"}[verdict]
        if verdict == STOP:
            reason = (exp.get("deciding") or {}).get("reason") or headline
            history = f"{actor} stopped experiment #{exp['id']}" + (f": {reason}" if reason else "")
            if self._settle(exp, challenger_status=status, verdict=STOP, actor=actor, headline=history, reasons=[],
                            history=history, kind="stopped"):
                self.notebook.stopped(exp["challenger"], exp["id"], actor, reason)
            return
        history = f"{actor} approved {verdict} for {exp['challenger']}: {headline}"
        if self._settle(exp, challenger_status=status, verdict=verdict, actor=actor, headline=headline, reasons=reasons,
                        history=history, new_champion=exp["challenger"] if verdict == SHIP else None):
            expected = self._expected(state, exp["challenger"])
            self.notebook.decided(exp["challenger"], verdict, headline, reasons, actor, exp["id"], expected)

    def _variant_key(self, option_id: int | None) -> str | None:
        if option_id is None:
            return None
        return next((v["key"] for v in self._feature().get("variants") or [] if v["id"] == option_id), None)

    def _adopt(self, exp: dict, detail: dict | None, actor: str) -> dict:
        """Flagsmith settled the experiment outside Darwin (the dashboard, or a rollout Darwin didn't approve).
        Darwin's state follows Flagsmith; nothing is written to Flagsmith."""
        s = self.store.read()
        eid, challenger = exp["id"], exp["challenger"]
        if detail is None:
            note = f"Experiment #{eid} no longer exists in Flagsmith, so Darwin stopped tracking it. {exp['champion']} is still the champion."
            status, new_champion, verdict = "stopped", None, STOP
        else:
            decision = detail.get("rollout_decision") or "control"
            key = self._variant_key(detail.get("rollout_variant")) if decision == "variant" else None
            if decision == "variant" and key == challenger:
                note = f"Flagsmith shows experiment #{eid} completed with {challenger} rolled out to everyone (done outside Darwin)."
                status, new_champion, verdict = "champion", challenger, SHIP
            elif decision == "variant" and key in s["generations"]:
                note = f"Flagsmith shows experiment #{eid} completed with {key} rolled out to everyone (done outside Darwin)."
                status, new_champion, verdict = "stopped", key, STOP
            elif decision == "variant":
                note = (f"Flagsmith shows experiment #{eid} completed with a variant Darwin doesn't know ({key or detail.get('rollout_variant')}) "
                        f"rolled out. Darwin stopped tracking it; check the flag in Flagsmith.")
                status, new_champion, verdict = "stopped", None, STOP
            elif decision == "disable":
                note = (f"Flagsmith shows experiment #{eid} completed with the flag turned off: ShopBot serves its bundled fallback "
                        f"genome until someone turns {self.flag} back on.")
                status, new_champion, verdict = "stopped", None, STOP
            else:
                note = f"Flagsmith shows experiment #{eid} completed with {exp['champion']} kept for everyone (done outside Darwin)."
                status, new_champion, verdict = "stopped", None, STOP
        note += " Darwin's state now matches; nothing was changed in Flagsmith."
        if self._settle(exp, challenger_status=status, verdict=verdict, actor="flagsmith", headline=note, reasons=[], history=note,
                        kind="reconciled", new_champion=new_champion):
            self.notebook.event(f"{challenger}: settled in Flagsmith", note)
        return {"settled": True, "note": note, "card": None}

    def _reconcile(self, exp: dict, detail: dict | None, actor: str) -> dict:
        """Match Darwin's state to Flagsmith's for this experiment. Never writes to Flagsmith: if an approved sequence
        stopped halfway, the missing steps go on a card for a human to approve."""
        if detail is None:
            return self._adopt(exp, None, actor)
        status = detail.get("status")
        if status != "completed":
            note = (f"Experiment #{exp['id']} is paused in Flagsmith: resume it there, or stop it." if status == "paused"
                    else f"Experiment #{exp['id']} is {status} in Flagsmith; nothing to reconcile.")
            return {"settled": False, "note": note, "card": None}
        intent = exp.get("deciding")
        if not self._matches(intent, exp, detail):
            return self._adopt(exp, detail, actor)
        s = self.store.read()
        challenger = self._genome(s, exp["challenger"])
        verdict, reason = intent["verdict"], intent.get("reason", "")

        def build(cid: str) -> list[Command]:
            return self._decision_plan(exp, verdict, challenger, card_id=cid, detail=detail, reason=reason)

        if not build("x"):  # every approved step is visible in Flagsmith: only Darwin's state is behind
            pending = s["pending"] if (s["pending"] or {}).get("experiment_id") == exp["id"] else {}
            self._settle_decision(exp, verdict, intent.get("actor") or actor, pending.get("original_headline") or
                                  pending.get("headline") or (exp.get("decision") or {}).get("headline") or verdict,
                                  pending.get("original_reasons") or pending.get("reasons") or [], s)
            return {"settled": True, "note": f"Experiment #{exp['id']} was already settled in Flagsmith; Darwin's state now matches.",
                    "card": None}
        pending = s["pending"]
        if pending and pending.get("kind") == "decision" and pending.get("experiment_id") == exp["id"] \
                and pending.get("finishing") and pending["commands"] == [c.to_dict() for c in build(pending["id"])]:
            return {"settled": False, "note": pending["headline"], "card": pending}
        card_id = uuid.uuid4().hex[:8]
        commands = [c.to_dict() for c in build(card_id)]
        base = pending if pending and pending.get("experiment_id") == exp["id"] else {}
        headline = (f"Flagsmith already completed experiment #{exp['id']} as approved ({VERBS[verdict].lower()}), but "
                    f"{len(commands)} approved step{'s' if len(commands) > 1 else ''} didn't run. Approving runs only "
                    f"{'these' if len(commands) > 1 else 'this one'}.")
        card = {"kind": "decision", "id": card_id, "created_at": _now(), "verdict": verdict, "finishing": True,
                "headline": headline, "reasons": base.get("reasons") or [], "experiment_id": exp["id"],
                "original_headline": base.get("original_headline") or base.get("headline") or (exp.get("decision") or {}).get("headline"),
                "original_reasons": base.get("original_reasons") or base.get("reasons") or [],
                "champion": exp["champion"], "challenger": exp["challenger"], "goal": exp["goal"],
                "readout": exp.get("readout"), "commands": commands}
        with self.store.transaction() as st:
            if st["experiment"] and st["experiment"]["id"] == exp["id"]:
                st["pending"] = card
                log_event(st, "unfinished", headline, "darwin", experiment_id=exp["id"])
        return {"settled": False, "note": headline, "card": card}

    def reconcile(self, actor: str = "darwin") -> dict:
        """Read Flagsmith's real state and make Darwin's match. Writes to Flagsmith only ever go through a card."""
        s = self.store.read()
        exp = s["experiment"]
        if not exp:
            return self._untracked()

        def prepare(st: dict) -> str:
            if not st["experiment"] or st["experiment"]["id"] != exp["id"]:
                raise StewardError("the experiment changed while reconciling; run it again")
            return f"Reconciling experiment #{exp['id']} with Flagsmith"

        with self._claim(prepare):
            return self._reconcile(exp, self._experiment_detail(exp["id"]), actor)

    def _untracked(self) -> dict:
        active = [e for e in self.admin.experiments() if e.get("status") != "completed"
                  and (e.get("feature") or {}).get("name", self.flag) == self.flag]
        if active:
            listed = ", ".join(f"#{e['id']} '{e.get('name')}' ({e.get('status')})" for e in active)
            return {"settled": False, "card": None,
                    "note": f"Darwin isn't running an experiment, but Flagsmith has an active one on {self.flag}: {listed}. "
                            f"Complete or delete it in Flagsmith before the next launch."}
        return {"settled": False, "card": None, "note": "Nothing to reconcile: no experiment is running, and Flagsmith agrees."}

    # ------------------------------------------------------------------ human actions
    def approve(self, actor: str, card_id: str | None = None) -> dict:
        ctx: dict = {}

        def prepare(s: dict) -> str:
            card = s["pending"]
            if not card:
                raise StewardError("nothing is waiting for approval")
            if card_id and card["id"] != card_id:
                raise StewardError("that card has been replaced; review the new one")
            ctx["card"] = card
            return f"Running {len(card['commands'])} Flagsmith commands for {actor}"

        with self._claim(prepare):
            card = ctx["card"]
            return self._launch(card, actor) if card["kind"] == "launch" else self._decide(card, actor)

    def reject(self, actor: str, note: str | None = None) -> dict | None:
        s = self.store.read()
        card = s["pending"]
        if not card:
            raise StewardError("nothing is waiting for approval")
        if self._busy_now(s):
            raise StewardError(f"busy: {s['busy']['what']}")
        if card["kind"] == "decision":
            with self.store.transaction() as st:
                e = st["experiment"]
                if e and e["id"] == card["experiment_id"]:
                    e["dismissed"] = {"verdict": card["verdict"], "as_of": (card.get("readout") or {}).get("as_of")}
                if st["pending"] and st["pending"]["id"] == card["id"]:
                    st["pending"] = None
                log_event(st, "dismissed", f"{actor} kept the experiment running instead of {card['verdict']}"
                          + (f": {note}" if note else "") + ". Darwin asks again when Flagsmith has new results.", actor,
                          experiment_id=card["experiment_id"])
            return None
        if note:
            return self._revise(card, note, actor)
        with self.store.transaction() as st:
            st["generations"][card["challenger"]]["status"] = "rejected"
            st["pending"] = None
            log_event(st, "rejected", f"{actor} rejected {card['challenger']}", actor, generation=card["challenger"])
        self.notebook.rejected(card["challenger"], actor, None)
        return None

    def _revise(self, card: dict, note: str, actor: str) -> dict:
        request_path = Path(card.get("request_path") or "")
        if not request_path.is_file():
            raise StewardError("this card has no Coach request to revise from; reject it and propose again")
        taken = self._variant_keys()  # checked before any money is spent
        ctx: dict = {}

        def prepare(s: dict) -> str:
            if not s["pending"] or s["pending"]["id"] != card["id"]:
                raise StewardError("that card has been replaced; review the new one")
            ctx["champion"] = self._champion(s)
            # If Flagsmith already has a variant with this card's name (from someone else), the revision gets a new one.
            ctx["name"] = card["challenger"] if card["challenger"] not in taken else self._next_name(s, taken)
            return f"The Coach is revising {card['challenger']}"

        with self._claim(prepare):
            champion, name = ctx["champion"], ctx["name"]
            request = request_path.read_text()
            previous = Proposal.model_validate(card["proposal"])
            spent_before = self.claude.ledger.snapshot()["spent_usd"]
            proposal = self.coach.revise(request=request, previous=previous, note=note, champion=champion, goal=card["goal"])
            cost = self.claude.ledger.snapshot()["spent_usd"] - spent_before + card.get("coach_cost_usd", 0)
            genome = proposal_to_genome(proposal, champion, name)
            new = self._launch_card(champion, genome, proposal, card["goal"], card["stats"], card["chats_read"], cost, request_path)
            new["revision_of"], new["review_note"] = card["id"], note
            if card.get("author", "coach") != "coach":
                new["based_on"] = card["author"]  # the Coach revised a human's prompt
            with self.store.transaction() as st:
                if name == card["challenger"]:
                    st["generations"][name].update(genome=genome.model_dump(), summary=genome.summary, author=None)
                else:
                    st["generations"][card["challenger"]]["status"] = "rejected"
                    st["generations"][name] = {"genome": genome.model_dump(), "status": "proposed", "born_at": _now(),
                                               "parent": champion.name, "goal": card["goal"], "summary": genome.summary}
                st["pending"] = new
                log_event(st, "revised", f"{actor} asked for changes (“{note}”); the Coach revised {genome.name}", actor,
                          generation=genome.name)
        self.notebook.rejected(card["challenger"], actor, note)
        return new

    def stop_plan(self, actor: str, reason: str = "") -> list[dict]:
        """The commands `darwin stop` would run, for the human to read first."""
        s = self.store.read()
        exp = s["experiment"]
        if not exp:
            raise StewardError("no experiment is running")
        detail = self._experiment_detail(exp["id"])
        if detail is None or detail.get("status") == "completed":
            return []
        return [c.to_dict() for c in self._decision_plan(exp, STOP, self._genome(s, exp["challenger"]), card_id=None,
                                                         approver=actor, detail=detail, reason=reason)]

    def abort(self, actor: str, reason: str = "", expected: list[dict] | None = None) -> dict:
        """Emergency stop: end the running experiment and keep the champion for everyone.

        `expected` is the plan the human was shown (see stop_plan); if Flagsmith changed since, nothing runs.
        """
        ctx: dict = {}

        def prepare(st: dict) -> str:
            if not st["experiment"]:
                raise StewardError("no experiment is running")
            ctx["exp"] = st["experiment"]
            return f"Stopping experiment #{st['experiment']['id']} for {actor}"

        with self._claim(prepare):
            exp = ctx["exp"]
            s = self.store.read()
            detail = self._experiment_detail(exp["id"])
            if detail is None or detail.get("status") == "completed":
                out = self._reconcile(exp, detail, actor)
                return {"stopped": exp["id"], "note": out["note"]}
            challenger = self._genome(s, exp["challenger"])
            plan = self._decision_plan(exp, STOP, challenger, card_id=None, approver=actor, detail=detail, reason=reason)
            if expected is not None and expected != [c.to_dict() for c in plan]:
                raise StewardError("Flagsmith changed since the commands were shown, so nothing ran. Run `darwin stop` again.")
            self._run_decision(exp, STOP, challenger, card_id=None, actor=actor, detail=detail, reason=reason)
            self._settle_decision(exp, STOP, actor, reason, [], s)
        return {"stopped": exp["id"]}

    @staticmethod
    def _rollback_allowed(state: dict, generation: str) -> None:
        if state["experiment"]:
            raise StewardError("an experiment is running — stop or decide it first")
        if generation not in state["generations"]:
            raise StewardError(f"unknown generation {generation}")

    def _rollback_commands(self, state: dict, generation: str) -> list[Command]:
        genome = self._genome(state, generation)
        keys = [v["key"] for v in self._feature().get("variants") or []]
        return [self.admin.cmd_set_default(self.flag, genome.to_flag_value(), genome.name, keys)]

    def rollback_plan(self, generation: str) -> list[dict]:
        """The commands `darwin rollback` would run, for the human to read first."""
        s = self.store.read()
        self._rollback_allowed(s, generation)
        return [c.to_dict() for c in self._rollback_commands(s, generation)]

    def rollback(self, generation: str, actor: str, expected: list[dict] | None = None) -> dict:
        """Make an earlier generation the champion again: one flag update, no redeploy.

        `expected` is the plan the human was shown (see rollback_plan); if Flagsmith changed since, nothing runs.
        """
        def prepare(st: dict) -> str:
            self._rollback_allowed(st, generation)
            return f"Rolling {self.flag} back to {generation} for {actor}"

        with self._claim(prepare):
            s = self.store.read()
            plan = self._rollback_commands(s, generation)
            if expected is not None and expected != [c.to_dict() for c in plan]:
                raise StewardError("Flagsmith changed since the command was shown, so nothing ran. Run `darwin rollback` again.")
            for cmd in plan:
                self.admin.run(cmd)
            with self.store.transaction() as st:
                old = st["champion"]
                if old and old != generation:
                    st["generations"][old]["status"] = "retired"
                st["generations"][generation].update(status="champion", became_champion_at=_now())
                st["champion"] = generation
                log_event(st, "rollback", f"{actor} rolled {self.flag} back to {generation}", actor, generation=generation)
        self.notebook.rolled_back(generation, old, actor)
        return {"champion": generation, "previous": old}

    def unlock(self, actor: str) -> dict | None:
        """Clear `busy` by hand. Darwin clears it itself when its process exits or after 15 minutes."""
        with self.store.transaction() as st:
            busy = st.get("busy")
            st["busy"] = None
            if busy:
                log_event(st, "unlocked", f"{actor} cleared the busy flag ({busy.get('what')})", actor)
        if busy:
            busy = dict(busy, alive=_pid_alive(busy.get("pid")) if busy.get("pid") else None)
        return busy

    # ------------------------------------------------------------------ views
    def status(self) -> dict:
        s = self.store.read()
        if busy_is_stale(s.get("busy")):
            with self.store.transaction() as st:
                self._expire_busy(st)
            s = self.store.read()
        lineage = []
        for name, g in sorted(s["generations"].items(), key=lambda kv: int(kv[0][3:]) if kv[0][3:].isdigit() else 0):
            lineage.append({"name": name, "status": g["status"], "parent": g.get("parent"), "summary": g.get("summary"),
                            "author": g.get("author") or ("coach" if g.get("parent") else "baseline"),
                            "goal": g.get("goal"), "model": g["genome"]["model"], "effort": g["genome"]["effort"],
                            "prompt_chars": len(g["genome"]["system_prompt"]), "result": g.get("result"),
                            "experiment_id": g.get("experiment_id"), "stats": self._stats(s, name)})
        journal = self.admin.journal()
        refresh = lambda j: "recompute" in j.get("summary", "") or "recount" in j.get("summary", "")  # noqa: E731
        changes = [j for j in journal if not refresh(j)]
        return {
            "flag": self.flag, "champion": s["champion"], "lineage": lineage, "experiment": s["experiment"],
            "pending": s["pending"], "busy": s["busy"], "history": s["history"][-40:],
            "spend": self.claude.ledger.snapshot(),
            "activity": {"flagsmith_writes": sum(1 for j in changes if j.get("ok")),
                         "results_refreshes": sum(1 for j in journal if refresh(j) and j.get("ok")),
                         "chats_read_by_coach": sum(e.get("chats_read", 0) for e in s["history"] if e["kind"] == "proposed"),
                         "coach_cost_usd": round(sum(e.get("coach_cost_usd", 0) for e in s["history"] if e["kind"] == "proposed"), 4),
                         "recent_commands": changes[-12:]},
            "links": {"flag": self.settings.flag_url(), "dashboard": self.settings.flagsmith_dashboard_url},
        }
