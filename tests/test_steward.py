import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from darwin.coach import Pattern, Proposal
from darwin.config import Settings
from darwin.genome import Genome
from darwin.llm import SpendLedger
from darwin.steward import Steward, StewardError
from tests.fake_flagsmith import FakeFlagsmith, payload

GEN1 = Genome(name="gen1", generation=1, model="claude-haiku-5-5", effort="medium", system_prompt="You are ShopBot.")


class FakeCoach:
    def __init__(self):
        self.calls = []

    def _proposal(self, prompt):
        return Proposal(patterns=[Pattern(pattern="Escalates angry customers at once", count=3, example_quote="Get me a human",
                                          root_cause="No escalation rule", fix="Offer to help first")],
                        hypothesis="Offering help first raises resolution", expected_effect="resolved up",
                        changes_summary=["Escalation rule"], risks="Slower handoffs", summary="Offer help before escalating",
                        system_prompt=prompt, effort="medium", model="claude-haiku-5-5")

    def propose(self, **kw):
        self.calls.append(("propose", kw))
        return self._proposal("You are ShopBot. Offer to help before escalating."), "REQUEST"

    def revise(self, **kw):
        self.calls.append(("revise", kw))
        return self._proposal("You are ShopBot. Offer to help before escalating. Keep it short.")


@pytest.fixture
def env(tmp_path):
    settings = replace(Settings.load(), data_dir=tmp_path, notebook_path=tmp_path / "LAB_NOTEBOOK.md",
                       flagsmith_admin_key="k", flagsmith_project="1", flagsmith_environment="ENV")
    fs = FakeFlagsmith(settings)
    claude = SimpleNamespace(ledger=SpendLedger(tmp_path / "spend.json", 2.0))
    steward = Steward(settings, admin=fs, claude=claude, coach=FakeCoach(), wait_for_results=0)
    return steward, fs, tmp_path


def chats(tmp_path, generation, n=6, resolved_every=2):
    with open(tmp_path / "transcripts.jsonl", "a") as f:
        for i in range(n):
            f.write(json.dumps({"generation": generation, "resolved": i % resolved_every == 0, "escalated": False, "started_at": 9e12,
                                "cost_usd": 0.0005, "turns": [{"role": "customer", "text": "hi"}, {"role": "bot", "text": "hello", "tools": []}]}) + "\n")


def test_init_creates_the_flag_and_metrics_once(env):
    steward, fs, _ = env
    steward.init(GEN1)
    steward.init(GEN1)
    assert list(fs.features) == ["shopbot_brain"] and len(fs.metric_list) == 3
    assert steward.store.read()["champion"] == "gen1"


def test_propose_requires_traffic_and_builds_a_card_with_commands(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    with pytest.raises(StewardError):
        steward.propose()
    chats(tmp, "gen1")
    writes_before = len(fs.log)
    card = steward.propose()
    assert card["kind"] == "launch" and card["challenger"] == "gen2"
    commands = [c["command"] for c in card["commands"]]
    assert any("feature variant add shopbot_brain --key gen2" in c for c in commands)
    assert any("experiments/" in c and "POST" in c for c in commands) and "start" in commands[-1]
    assert card["diff"]["lines_added"] >= 1
    assert len(fs.log) == writes_before  # proposing changes nothing in Flagsmith; only approval does


def test_full_generation_launch_check_ship(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    out = steward.approve("anugrah")
    exp_id = out["experiment_id"]
    s = steward.store.read()
    assert s["experiment"]["challenger"] == "gen2" and fs.exps[exp_id]["status"] == "running"
    body = fs.exps[exp_id]["body"]["experiment_rollout"]
    assert body["enabled"] is False and body["rollout_percentage"] == 100
    assert json.loads(body["feature_state_value"]["value"])["name"] == "gen1"  # control = champion

    # not enough data yet: no card
    assert steward.check()["decision"]["verdict"] == "COLLECTING_DATA"
    assert steward.store.read()["pending"] is None

    # a clear winner: the rules recommend SHIP, a human approves
    fs.payloads[exp_id] = payload(s["metrics"], "gen2")
    assert steward.check()["decision"]["verdict"] == "SHIP"
    card = steward.store.read()["pending"]
    assert card["kind"] == "decision" and "complete" in card["commands"][0]["command"]
    steward.approve("anugrah")
    s = steward.store.read()
    assert s["champion"] == "gen2" and s["experiment"] is None and s["generations"]["gen1"]["status"] == "retired"
    assert fs.exps[exp_id]["decision"]["rollout_decision"] == "variant"
    assert json.loads(fs.served_value("shopbot_brain"))["name"] == "gen2"
    assert json.loads(fs.features["shopbot_brain"]["default_value"])["name"] == "gen2"  # default normalised too
    assert "gen2: SHIP" in (tmp / "LAB_NOTEBOOK.md").read_text()


def test_guardrail_breach_kills_and_keeps_the_champion(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("anugrah")["experiment_id"]
    p = payload(steward.store.read()["metrics"], "gen2")
    p["metrics"][1]["inference"]["gen2"] = {"lift": 0.8, "ci_low": 0.3, "ci_high": 1.3, "chance_to_win": 0.01}  # escalations up
    fs.payloads[exp_id] = p
    assert steward.check()["decision"]["verdict"] == "KILL"
    steward.approve("anugrah")
    s = steward.store.read()
    assert s["champion"] == "gen1" and s["generations"]["gen2"]["status"] == "killed"
    assert fs.exps[exp_id]["decision"] == {"rollout_decision": "control"}
    assert json.loads(fs.served_value("shopbot_brain"))["name"] == "gen1"


def test_second_generation_uses_the_new_champion_as_control(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp1 = steward.approve("a")["experiment_id"]
    fs.payloads[exp1] = payload(steward.store.read()["metrics"], "gen2")
    steward.check()
    steward.approve("a")
    chats(tmp, "gen2")
    steward.propose()
    exp2 = steward.approve("a")["experiment_id"]
    rollout = fs.exps[exp2]["body"]["experiment_rollout"]
    assert json.loads(rollout["feature_state_value"]["value"])["name"] == "gen2"
    weights = {v["multivariate_feature_option"]: v["percentage_allocation"] for v in rollout["multivariate_feature_state_values"]}
    by_key = {v["key"]: v["id"] for v in fs.features["shopbot_brain"]["variants"]}
    assert weights == {by_key["gen2"]: 0, by_key["gen3"]: 50}  # every variant listed, older ones at 0


def test_reject_with_a_note_asks_the_coach_to_revise(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    card = steward.reject("anugrah", note="Keep it short")
    assert card["review_note"] == "Keep it short" and "Keep it short." in card["genome"]["system_prompt"]
    steward.reject("anugrah")
    s = steward.store.read()
    assert s["pending"] is None and s["generations"]["gen2"]["status"] == "rejected"


def test_dismissing_a_recommendation_keeps_the_experiment_running(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2")
    steward.check()
    steward.reject("a")
    steward.check()
    assert steward.store.read()["pending"] is None and fs.exps[exp_id]["status"] == "running"


def test_throttled_refresh_still_reads_the_latest_results(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2")
    fs.throttle_refresh = True
    out = steward.check()
    assert out["decision"]["verdict"] == "SHIP" and "5 minutes" in out["note"]


def test_abort_and_rollback(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    steward.approve("a")
    with pytest.raises(StewardError):
        steward.rollback("gen1", "a")
    steward.abort("a", "looks wrong")
    assert steward.store.read()["generations"]["gen2"]["status"] == "stopped"
    steward.rollback("gen1", "a")
    assert json.loads(fs.features["shopbot_brain"]["default_value"])["name"] == "gen1"


def test_refresh_wait_follows_flagsmiths_five_minute_rule():
    from darwin.steward import refresh_wait_seconds

    assert refresh_wait_seconds(None) == 0
    assert refresh_wait_seconds("2026-11-25T14:18:00Z", now=1795616280 + 60) in range(239, 242)  # 14:18 UTC + 1 min
    assert refresh_wait_seconds("2026-11-25T14:18:00Z", now=1795616280 + 400) == 0
