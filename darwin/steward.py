"""The Steward runs the evolution loop. Nothing that touches live traffic happens without a human's approval.

    observe ─► propose (Coach) ─► [approve] launch ─► check (Flagsmith results + rules) ─► [approve] ship / discard / kill

Approval cards carry the evidence and the exact Flagsmith CLI commands an approval will run.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from . import transcripts
from .coach import GOALS, Coach, Proposal, dumps, proposal_to_genome
from .config import Settings
from .flagsmith_admin import Command, FlagsmithAdmin, FlagsmithError, Throttled
from .genome import Genome
from .llm import Claude, make_claude
from .notebook import Notebook
from .results import CONTROL, parse_readout
from .rules import DISCARD, KILL, SHIP, DecisionPolicy, Guardrail, decide
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
POLICIES = {
    "quality": DecisionPolicy(primary="resolved", guardrails=(
        Guardrail("escalated", 0.0, "escalations to a human"), Guardrail("cost_microusd", 0.30, "cost per chat"))),
    "cost": DecisionPolicy(primary="cost_microusd", guardrails=(
        Guardrail("resolved", 0.0, "resolution rate"), Guardrail("escalated", 0.0, "escalations to a human"))),
}
SPLIT = 50  # % of traffic the challenger gets


class StewardError(RuntimeError):
    pass


def _now() -> float:
    return time.time()


class Steward:
    def __init__(self, settings: Settings, *, admin: FlagsmithAdmin | None = None, claude: Claude | None = None,
                 coach: Coach | None = None, store: StateStore | None = None, notebook: Notebook | None = None,
                 transcripts_path: Path | None = None, wait_for_results: float = 20.0):
        self.settings = settings
        self.flag = settings.flag
        self.admin = admin or FlagsmithAdmin(settings)
        self.claude = claude or make_claude(settings.data_dir, settings.budget_usd)
        self.coach = coach or Coach(self.claude, model=settings.coach_model)
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

    def _next_name(self, state: dict) -> str:
        numbers = [int(n[3:]) for n in state["generations"] if n.startswith("gen") and n[3:].isdigit()]
        return f"gen{max(numbers, default=0) + 1}"

    def _feature(self) -> dict:
        feature = self.admin.feature(self.flag)
        if feature is None:
            raise StewardError(f"flag {self.flag} doesn't exist in Flagsmith — run `darwin init`.")
        return feature

    def _stats(self, state: dict, name: str) -> dict:
        since = state["generations"][name].get("became_champion_at") or state["generations"][name].get("born_at")
        return transcripts.stats(transcripts.load(self.transcripts_path, generation=name, since=since))

    # ------------------------------------------------------------------ init
    def init(self, gen1: Genome, actor: str = "darwin") -> list[str]:
        missing = self.settings.missing_for_admin()
        if missing:
            raise StewardError("missing settings: " + ", ".join(missing))
        notes = []
        feature = self.admin.feature(self.flag)
        if feature is None:
            self.admin.run(self.admin.cmd_create_feature(self.flag, gen1.to_flag_value(), f"{gen1.name} genome"))
            feature = self._feature()
            notes.append(f"created flag {self.flag} with {gen1.name} as its value")
        else:
            notes.append(f"flag {self.flag} already exists")
        existing = {m["name"]: m["id"] for m in self.admin.metrics()}
        for name, spec in METRICS.items():
            if name not in existing:
                created = self.admin.run(self.admin.cmd_create_metric(name, spec["event"], spec["aggregation"],
                                                                     spec["direction"], spec["description"]))
                existing[name] = created["id"]
                notes.append(f"created metric {name}")
        current = Genome.from_flag_value(feature["default_value"])
        with self.store.transaction() as s:
            s["feature_id"] = feature["id"]
            s["metrics"] = {n: existing[n] for n in METRICS}
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
        with self.store.transaction() as s:
            if s["experiment"]:
                raise StewardError("an experiment is running — decide it before proposing the next generation")
            if s["pending"]:
                raise StewardError("a proposal is already waiting for approval")
            if s["busy"]:
                raise StewardError(f"busy: {s['busy']['what']}")
            champion = self._champion(s)
            name = self._next_name(s)
            since = s["generations"][champion.name].get("became_champion_at")
            history = self._history_lines(s)
            s["busy"] = {"what": f"The Coach is reading {champion.name}'s conversations", "since": _now()}
        try:
            rows = transcripts.load(self.transcripts_path, generation=champion.name, since=since)
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
        except Exception:
            with self.store.transaction() as s:
                s["busy"] = None
            raise
        with self.store.transaction() as s:
            s["generations"][name] = {"genome": genome.model_dump(), "status": "proposed", "born_at": _now(),
                                      "parent": champion.name, "goal": goal, "summary": genome.summary}
            s["pending"] = card
            s["busy"] = None
            log_event(s, "proposed", f"Coach proposed {name}: {genome.summary}", "coach", generation=name,
                      chats_read=card["chats_read"], coach_cost_usd=card["coach_cost_usd"])
        return card

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
        return {
            "kind": "launch", "id": uuid.uuid4().hex[:8], "created_at": _now(), "goal": goal, "goal_text": GOALS[goal],
            "champion": champion.name, "challenger": genome.name, "genome": genome.model_dump(),
            "diff": champion.diff(genome), "proposal": dumps(proposal), "stats": stats, "chats_read": chats_read,
            "coach_cost_usd": round(coach_cost, 4), "request_path": str(request_path),
            "commands": [c.to_dict() for c in self._launch_plan(champion, genome, goal, proposal.hypothesis)],
            "metrics": [f"{m} ({d.replace('_', ' ')})" for m, d in EXPERIMENT_METRICS[goal]],
        }

    # ------------------------------------------------------------------ launch
    def _launch_plan(self, champion: Genome, challenger: Genome, goal: str, hypothesis: str, *,
                     feature: dict | None = None, option_id: int | None = None, experiment_id: int | None = None,
                     metric_ids: dict | None = None) -> list[Command]:
        feature = feature or self._feature()
        variants = [v for v in feature.get("variants") or [] if v["key"] != challenger.name]
        champ_value = champion.to_flag_value()
        plan = []
        if feature.get("default_value") != champ_value or any(v.get("weight") for v in variants):
            plan.append(self.admin.cmd_set_default(self.flag, champ_value, champion.name, [v["key"] for v in variants]))
        plan.append(self.admin.cmd_add_variant(self.flag, challenger.name, challenger.to_flag_value()))
        new_id = option_id if option_id is not None else f"<{challenger.name} variant id>"
        weights = {v["id"]: 0 for v in variants} | {new_id: SPLIT}
        labels = {v["id"]: v["key"] for v in variants} | {new_id: challenger.name}
        metric_ids = metric_ids or self.store.read()["metrics"]
        metrics = [{"metric": metric_ids.get(m, f"<{m} metric id>"), "expected_direction": d} for m, d in EXPERIMENT_METRICS[goal]]
        plan.append(self.admin.cmd_create_experiment(
            feature_id=feature["id"], name=f"{self.flag}: {champion.name} vs {challenger.name}", hypothesis=hypothesis,
            metrics=metrics, control_value=champ_value, control_label=champion.name, weights=weights, weight_labels=labels))
        plan.append(self.admin.cmd_start(experiment_id if experiment_id is not None else "<new experiment id>"))
        return plan

    def _launch(self, card: dict, actor: str) -> dict:
        s = self.store.read()
        champion = self._champion(s)
        challenger = Genome.model_validate(card["genome"])
        hypothesis = card["proposal"]["hypothesis"]
        feature = self._feature()
        if not any(v["key"] == challenger.name for v in feature.get("variants") or []):
            plan = self._launch_plan(champion, challenger, card["goal"], hypothesis, feature=feature)
            for cmd in plan[:-2]:  # set default (if needed) + add variant
                self.admin.run(cmd)
            feature = self._feature()
        option_id = next(v["id"] for v in feature["variants"] if v["key"] == challenger.name)
        plan = self._launch_plan(champion, challenger, card["goal"], hypothesis, feature=feature, option_id=option_id,
                                 metric_ids=s["metrics"])
        try:
            experiment = self.admin.run(plan[-2])
        except FlagsmithError as exc:
            if exc.status != 409:
                raise
            experiment = next(e for e in self.admin.experiments()
                              if e["status"] != "completed" and challenger.name in e["name"])  # retrying a half-done launch
        experiment_id = experiment["id"]
        if experiment.get("status") in (None, "created"):
            self.admin.run(self.admin.cmd_start(experiment_id))
        with self.store.transaction() as st:
            gen = st["generations"][challenger.name]
            gen.update(status="challenger", option_id=option_id, experiment_id=experiment_id, launched_at=_now(), launched_by=actor)
            st["experiment"] = {"id": experiment_id, "champion": champion.name, "challenger": challenger.name,
                                "goal": card["goal"], "split": {CONTROL: (100 - SPLIT) / 100, challenger.name: SPLIT / 100},
                                "started_at": _now(), "launched_by": actor, "option_id": option_id,
                                "readout": None, "decision": None, "dismissed": None}
            st["pending"] = None
            log_event(st, "launched", f"{actor} approved {challenger.name}; experiment #{experiment_id} is running "
                                      f"({champion.name} vs {challenger.name}, {SPLIT}/{100 - SPLIT})", actor,
                      generation=challenger.name, experiment_id=experiment_id)
        self.notebook.launched(card, experiment_id, actor)
        return {"experiment_id": experiment_id, "challenger": challenger.name}

    # ------------------------------------------------------------------ check
    def check(self, actor: str = "darwin", refresh: bool = True) -> dict:
        s = self.store.read()
        exp = s["experiment"]
        if not exp:
            return {"running": False}
        note = None
        before = (self.admin.results(exp["id"]) or {}).get("as_of")
        if refresh:
            try:
                self.admin.run(self.admin.cmd_refresh_results(exp["id"]))
                deadline = time.time() + self.wait_for_results
                while time.time() < deadline:
                    if (self.admin.results(exp["id"]) or {}).get("as_of") != before:
                        break
                    time.sleep(1.0)
            except Throttled:
                note = "Flagsmith recomputes results at most once every 5 minutes; showing the latest."
        detail = self.admin.experiment(exp["id"])
        results = self.admin.results(exp["id"])
        readout = parse_readout(detail, results, challenger=exp["challenger"], expected_split=exp["split"])
        decision = decide(readout, POLICIES[exp["goal"]], champion=exp["champion"])
        with self.store.transaction() as st:
            e = st["experiment"]
            if not e or e["id"] != exp["id"]:
                return {"running": False}
            e["readout"], e["decision"], e["checked_at"], e["note"] = readout.to_dict(), decision.to_dict(), _now(), note
            pending = st["pending"]
            if decision.needs_approval and e.get("dismissed") != decision.verdict:
                if not pending or pending.get("kind") != "decision" or pending.get("verdict") != decision.verdict:
                    st["pending"] = self._decision_card(st, e, decision)
                    log_event(st, "recommended", f"Rules recommend {decision.verdict} for {e['challenger']}: {decision.headline}",
                              "darwin", generation=e["challenger"], experiment_id=e["id"])
                else:
                    pending.update(headline=decision.headline, reasons=decision.reasons, readout=readout.to_dict())
            elif pending and pending.get("kind") == "decision" and not decision.needs_approval:
                st["pending"] = None
                log_event(st, "withdrawn", f"Recommendation withdrawn: {decision.headline}", "darwin", experiment_id=e["id"])
        return {"running": True, "readout": readout.to_dict(), "decision": decision.to_dict(), "note": note}

    def _decision_card(self, state: dict, exp: dict, decision) -> dict:
        challenger = self._genome(state, exp["challenger"])
        return {"kind": "decision", "id": uuid.uuid4().hex[:8], "created_at": _now(), "verdict": decision.verdict,
                "headline": decision.headline, "reasons": decision.reasons, "experiment_id": exp["id"],
                "champion": exp["champion"], "challenger": exp["challenger"], "goal": exp["goal"],
                "readout": exp["readout"],
                "commands": [c.to_dict() for c in self._decision_plan(exp, decision.verdict, challenger)]}

    def _decision_plan(self, exp: dict, verdict: str, challenger: Genome) -> list[Command]:
        if verdict == SHIP:
            keys = [v["key"] for v in self._feature().get("variants") or []]
            return [self.admin.cmd_complete(exp["id"], "variant", exp["option_id"], challenger.name),
                    self.admin.cmd_set_default(self.flag, challenger.to_flag_value(), challenger.name, keys)]
        return [self.admin.cmd_complete(exp["id"], "control")]

    def _decide(self, card: dict, actor: str) -> dict:
        s = self.store.read()
        exp = s["experiment"]
        if not exp or exp["id"] != card["experiment_id"]:
            raise StewardError("that experiment is no longer running")
        challenger = self._genome(s, exp["challenger"])
        for cmd in self._decision_plan(exp, card["verdict"], challenger):
            self.admin.run(cmd)
        try:
            self.admin.run(self.admin.cmd_refresh_results(exp["id"]))  # freeze the final numbers
        except FlagsmithError:
            pass
        status = {SHIP: "champion", DISCARD: "discarded", KILL: "killed"}[card["verdict"]]
        with self.store.transaction() as st:
            e = st["experiment"]
            gen = st["generations"][e["challenger"]]
            gen.update(status=status, decided_at=_now(), decided_by=actor,
                       result={"verdict": card["verdict"], "headline": card["headline"], "reasons": card["reasons"],
                               "readout": e.get("readout"), "experiment_id": e["id"]})
            if card["verdict"] == SHIP:
                st["generations"][e["champion"]]["status"] = "retired"
                gen["became_champion_at"] = _now()
                st["champion"] = e["challenger"]
            st["experiment"] = None
            st["pending"] = None
            log_event(st, "decided", f"{actor} approved {card['verdict']} for {e['challenger']}: {card['headline']}", actor,
                      generation=e["challenger"], experiment_id=e["id"], verdict=card["verdict"])
        self.notebook.decided(exp["challenger"], card["verdict"], card["headline"], card["reasons"], actor, exp["id"])
        return {"verdict": card["verdict"], "champion": exp["challenger"] if card["verdict"] == SHIP else exp["champion"]}

    # ------------------------------------------------------------------ human actions
    def approve(self, actor: str, card_id: str | None = None) -> dict:
        s = self.store.read()
        card = s["pending"]
        if not card:
            raise StewardError("nothing is waiting for approval")
        if card_id and card["id"] != card_id:
            raise StewardError("that card has been replaced; review the new one")
        if s.get("busy"):
            raise StewardError(f"busy: {s['busy']['what']}")
        with self.store.transaction() as st:
            st["busy"] = {"what": f"Running {len(card['commands'])} Flagsmith commands for {actor}", "since": _now()}
        try:
            return self._launch(card, actor) if card["kind"] == "launch" else self._decide(card, actor)
        finally:
            with self.store.transaction() as st:
                st["busy"] = None

    def reject(self, actor: str, note: str | None = None) -> dict | None:
        s = self.store.read()
        card = s["pending"]
        if not card:
            raise StewardError("nothing is waiting for approval")
        if card["kind"] == "decision":
            with self.store.transaction() as st:
                st["experiment"]["dismissed"] = card["verdict"]
                st["pending"] = None
                log_event(st, "dismissed", f"{actor} kept the experiment running instead of {card['verdict']}"
                          + (f": {note}" if note else ""), actor, experiment_id=card["experiment_id"])
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
        with self.store.transaction() as st:
            st["busy"] = {"what": f"The Coach is revising {card['challenger']}", "since": _now()}
            champion = self._champion(st)
        try:
            request = Path(card["request_path"]).read_text()
            previous = Proposal.model_validate(card["proposal"])
            spent_before = self.claude.ledger.snapshot()["spent_usd"]
            proposal = self.coach.revise(request=request, previous=previous, note=note, champion=champion, goal=card["goal"])
            cost = self.claude.ledger.snapshot()["spent_usd"] - spent_before + card.get("coach_cost_usd", 0)
            genome = proposal_to_genome(proposal, champion, card["challenger"])
            new = self._launch_card(champion, genome, proposal, card["goal"], card["stats"], card["chats_read"], cost,
                                    Path(card["request_path"]))
            new["revision_of"], new["review_note"] = card["id"], note
        finally:
            with self.store.transaction() as st:
                st["busy"] = None
        with self.store.transaction() as st:
            st["generations"][genome.name].update(genome=genome.model_dump(), summary=genome.summary)
            st["pending"] = new
            log_event(st, "revised", f"{actor} asked for changes (“{note}”); the Coach revised {genome.name}", actor,
                      generation=genome.name)
        self.notebook.rejected(card["challenger"], actor, note)
        return new

    def abort(self, actor: str, reason: str = "") -> dict:
        """Emergency stop: end the running experiment and keep the champion for everyone."""
        s = self.store.read()
        exp = s["experiment"]
        if not exp:
            raise StewardError("no experiment is running")
        self.admin.run(self.admin.cmd_complete(exp["id"], "control"))
        with self.store.transaction() as st:
            st["generations"][exp["challenger"]].update(status="stopped", decided_at=_now(), decided_by=actor)
            st["experiment"] = None
            st["pending"] = None
            log_event(st, "stopped", f"{actor} stopped experiment #{exp['id']}" + (f": {reason}" if reason else ""), actor,
                      experiment_id=exp["id"])
        return {"stopped": exp["id"]}

    def rollback(self, generation: str, actor: str) -> dict:
        s = self.store.read()
        if s["experiment"]:
            raise StewardError("an experiment is running — stop or decide it first")
        if generation not in s["generations"]:
            raise StewardError(f"unknown generation {generation}")
        genome = self._genome(s, generation)
        keys = [v["key"] for v in self._feature().get("variants") or []]
        self.admin.run(self.admin.cmd_set_default(self.flag, genome.to_flag_value(), genome.name, keys))
        with self.store.transaction() as st:
            old = st["champion"]
            if old and old != generation:
                st["generations"][old]["status"] = "retired"
            st["generations"][generation].update(status="champion", became_champion_at=_now())
            st["champion"] = generation
            log_event(st, "rollback", f"{actor} rolled {self.flag} back to {generation}", actor, generation=generation)
        return {"champion": generation}

    # ------------------------------------------------------------------ views
    def status(self) -> dict:
        s = self.store.read()
        lineage = []
        for name, g in sorted(s["generations"].items(), key=lambda kv: int(kv[0][3:]) if kv[0][3:].isdigit() else 0):
            lineage.append({"name": name, "status": g["status"], "parent": g.get("parent"), "summary": g.get("summary"),
                            "goal": g.get("goal"), "model": g["genome"]["model"], "effort": g["genome"]["effort"],
                            "prompt_chars": len(g["genome"]["system_prompt"]), "result": g.get("result"),
                            "experiment_id": g.get("experiment_id"), "stats": self._stats(s, name)})
        journal = self.admin.journal()
        return {
            "flag": self.flag, "champion": s["champion"], "lineage": lineage, "experiment": s["experiment"],
            "pending": s["pending"], "busy": s["busy"], "history": s["history"][-40:],
            "spend": self.claude.ledger.snapshot(),
            "activity": {"flagsmith_writes": sum(1 for j in journal if j.get("ok")),
                         "chats_read_by_coach": sum(e.get("chats_read", 0) for e in s["history"] if e["kind"] == "proposed"),
                         "coach_cost_usd": round(sum(e.get("coach_cost_usd", 0) for e in s["history"] if e["kind"] == "proposed"), 4),
                         "recent_commands": journal[-12:]},
            "links": {"flag": self.settings.flag_url(), "dashboard": self.settings.flagsmith_dashboard_url},
        }
