import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from darwin.coach import Pattern, Proposal
from darwin.config import Settings
from darwin.genome import Genome
from darwin.flagsmith_admin import FlagsmithError
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



def test_second_launch_doesnt_reset_a_default_that_ship_already_set(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp1 = steward.approve("a")["experiment_id"]
    fs.payloads[exp1] = payload(steward.store.read()["metrics"], "gen2")
    steward.check()
    steward.approve("a")
    chats(tmp, "gen2")
    card = steward.propose()
    assert not any("default value" in c["summary"] for c in card["commands"])  # ship already made gen2 the default


def test_approval_refuses_when_flagsmith_changed_since_the_card(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    fs.features["shopbot_brain"]["default_value"] = "someone edited the flag in the dashboard"
    with pytest.raises(StewardError):
        steward.approve("a", card_id=card["id"])
    updated = steward.store.read()["pending"]
    assert updated["id"] != card["id"] and any("default value" in c["summary"] for c in updated["commands"])
    steward.approve("a", card_id=updated["id"])
    assert steward.store.read()["experiment"]


def test_a_human_written_challenger_gets_the_same_gate(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    card = steward.propose_manual(system_prompt="You are ShopBot. Be brief.", goal="cost", summary="Trim the prompt",
                                  hypothesis="A shorter prompt costs less", actor="pm", effort="low")
    assert card["author"] == "pm" and card["challenger"] == "gen2" and card["proposal"]["patterns"] == []
    assert len(fs.log) == 4  # nothing ran yet: only init's writes
    steward.approve("anugrah")
    assert steward.store.read()["experiment"]["goal"] == "cost"


def ship_gen2(steward, fs, tmp):
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2")
    assert steward.check()["decision"]["verdict"] == "SHIP"
    steward.approve("a")
    return exp_id


def test_init_registers_the_genome_flagsmith_serves_not_the_value_the_flag_was_created_with(env):
    steward, fs, tmp = env
    ship_gen2(steward, fs, tmp)
    (tmp / "lab.json").unlink()  # state lost, or a fresh clone pointed at the same Flagsmith
    steward.init(GEN1)
    assert steward.store.read()["champion"] == "gen2"


def test_a_new_generation_never_reuses_a_variant_key_flagsmith_already_has(env):
    steward, fs, tmp = env
    ship_gen2(steward, fs, tmp)
    gen2_value = next(v["value"] for v in fs.features["shopbot_brain"]["variants"] if v["key"] == "gen2")
    (tmp / "lab.json").unlink()
    steward.init(GEN1)
    chats(tmp, "gen2")
    card = steward.propose()
    assert card["challenger"] == "gen3"
    steward.approve("a", card_id=card["id"])
    assert next(v["value"] for v in fs.features["shopbot_brain"]["variants"] if v["key"] == "gen2") == gen2_value


def test_launch_refuses_when_the_variant_key_holds_a_different_genome(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    f = fs.features["shopbot_brain"]
    f["type"] = "multivariate"
    f["variants"].append({"id": 99, "key": "gen2", "value": "an older gen2 written somewhere else", "weight": 0.0})
    with pytest.raises(StewardError, match="different genome"):
        steward.approve("a", card_id=card["id"])
    assert not fs.exps and steward.store.read()["experiment"] is None


def test_approval_compares_command_bodies_not_just_summaries(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    f = fs.features["shopbot_brain"]
    f["type"] = "multivariate"
    f["variants"].append({"id": 99, "key": "hotfix", "value": "{}", "weight": 0.0})  # added in the dashboard
    with pytest.raises(StewardError, match="changed"):
        steward.approve("a", card_id=card["id"])
    updated = steward.store.read()["pending"]
    assert updated["id"] != card["id"] and '"multivariate_feature_option": 99' in next(c["body"] for c in updated["commands"] if c["body"])
    exp_id = steward.approve("a", card_id=updated["id"])["experiment_id"]
    options = [v["multivariate_feature_option"] for v in fs.exps[exp_id]["body"]["experiment_rollout"]["multivariate_feature_state_values"]]
    assert 99 in options


def test_retrying_a_half_done_launch_runs_only_what_is_left(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    fs.fail_next = lambda c: c.args[:1] == ["api"] and c.args[1].endswith("/start/")
    with pytest.raises(FlagsmithError):
        steward.approve("a", card_id=card["id"])  # variant added, experiment created, start failed
    assert [e["status"] for e in fs.exps.values()] == ["created"]
    with pytest.raises(StewardError):
        steward.approve("a", card_id=card["id"])  # the variant exists now, so the card changes
    updated = steward.store.read()["pending"]
    assert not any("variant add" in c["command"] for c in updated["commands"])
    exp_id = steward.approve("a", card_id=updated["id"])["experiment_id"]
    assert len(fs.exps) == 1 and fs.exps[exp_id]["status"] == "running"
    assert steward.store.read()["experiment"]["id"] == exp_id


def test_a_conflicting_experiment_someone_else_created_is_a_clear_error(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    fid = fs.features["shopbot_brain"]["id"]
    fs.exps[500] = {"id": 500, "name": "pricing test", "status": "running", "feature": {"id": fid, "name": "shopbot_brain"}}
    with pytest.raises(StewardError, match="pricing test"):
        steward.approve("a", card_id=card["id"])


def test_ctrl_c_during_a_coach_call_leaves_darwin_unlocked(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")

    def interrupted(**kw):
        raise KeyboardInterrupt
    steward.coach.propose = interrupted
    with pytest.raises(KeyboardInterrupt):
        steward.propose()
    assert steward.store.read()["busy"] is None
    del steward.coach.propose
    assert steward.propose()["challenger"] == "gen2"


def dead_pid() -> int:
    import subprocess
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_a_busy_flag_left_by_a_dead_process_or_older_than_15_minutes_clears_itself(env):
    import os
    import time as _time
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    with steward.store.transaction() as s:
        s["busy"] = {"what": "The Coach is reading gen1's conversations", "since": _time.time(), "pid": dead_pid()}
    assert steward.propose()["challenger"] == "gen2"
    steward.reject("a")
    with steward.store.transaction() as s:
        s["busy"] = {"what": "Running 4 Flagsmith commands", "since": _time.time() - 16 * 60, "pid": os.getpid()}
    assert steward.status()["busy"] is None


def test_a_live_busy_flag_blocks_other_actions_until_unlocked(env):
    import os
    import time as _time
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    with steward.store.transaction() as s:
        s["busy"] = {"what": "Running 4 Flagsmith commands for the Lab", "since": _time.time(), "pid": os.getpid(), "token": "x"}
    with pytest.raises(StewardError, match="busy"):
        steward.approve("a")
    with pytest.raises(StewardError, match="busy"):
        steward.reject("a")
    cleared = steward.unlock("a")
    assert cleared["what"].startswith("Running") and steward.store.read()["busy"] is None
    steward.approve("a")
    assert steward.store.read()["experiment"] and steward.store.read()["busy"] is None


def test_cli_unlock_reports_what_it_cleared(env, monkeypatch, capsys):
    import time as _time
    import darwin.cli as cli
    steward, fs, tmp = env
    steward.init(GEN1)
    with steward.store.transaction() as s:
        s["busy"] = {"what": "The Coach is revising gen2", "since": _time.time(), "pid": dead_pid()}
    monkeypatch.setattr(cli, "steward", lambda: steward)
    cli.main(["unlock", "--as", "anugrah"])
    assert "Cleared: The Coach is revising gen2" in capsys.readouterr().out
    assert steward.store.read()["busy"] is None


def test_the_planned_sample_is_on_the_launch_card_and_fixed_at_launch(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    assert card["plan"]["min_per_arm"] == 100
    assert any("escalations may not rise more than 5 pts" in r for r in card["plan"]["rules"])
    exp_id = steward.approve("a")["experiment_id"]
    assert steward.store.read()["experiment"]["min_per_arm"] == 100
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2", n=(80, 80))
    d = steward.check()["decision"]
    assert d["verdict"] == "COLLECTING_DATA" and "80 of 100 chats per arm" in d["headline"]
    assert steward.store.read()["pending"] is None


def test_flagsmith_records_who_approved_the_launch_and_the_decision(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose()
    create = next(c for c in card["commands"] if c["body"] and '"hypothesis"' in c["body"])
    assert f"Approved in Darwin by <approver> (card {card['id']})." in create["body"]  # the card shows a placeholder
    exp_id = steward.approve("anugrah", card_id=card["id"])["experiment_id"]
    assert fs.exps[exp_id]["hypothesis"].endswith(f"Approved in Darwin by anugrah (card {card['id']}).")
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2")
    steward.check()
    decision = steward.store.read()["pending"]
    record = decision["commands"][-1]
    assert "PATCH" in record["command"] and "Shipped in Darwin by <approver>" in record["summary"]
    steward.approve("maya", card_id=decision["id"])
    assert fs.exps[exp_id]["hypothesis"].endswith(f"Shipped in Darwin by maya (card {decision['id']}).")
    assert "Approved in Darwin by anugrah" in fs.exps[exp_id]["hypothesis"]


def to_ship_card(steward, fs, tmp):
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2")
    assert steward.check()["decision"]["verdict"] == "SHIP"
    return exp_id


def completes(fs):
    return [c for c in fs.log if c.args[0] == "api" and c.args[1].endswith("/complete/")]


def test_a_ship_that_stops_halfway_is_finished_without_completing_twice(env):
    steward, fs, tmp = env
    exp_id = to_ship_card(steward, fs, tmp)
    fs.fail_next = lambda c: c.args[:2] == ["flag", "update"]  # complete works, then making gen2 the default fails
    with pytest.raises(FlagsmithError):
        steward.approve("a")
    assert fs.exps[exp_id]["status"] == "completed" and steward.store.read()["experiment"]["deciding"]["verdict"] == "SHIP"
    out = steward.check()
    card = out["reconciled"]["card"]
    assert out["running"] and card["finishing"] and not any("complete" in c["command"] for c in card["commands"])
    assert len(card["commands"]) == 2  # make gen2 the default, record the decision in Flagsmith
    assert steward.check()["reconciled"]["card"]["id"] == card["id"]  # checking again doesn't churn the card
    steward.approve("a", card_id=card["id"])
    s = steward.store.read()
    assert s["champion"] == "gen2" and s["experiment"] is None and s["pending"] is None
    assert json.loads(fs.features["shopbot_brain"]["default_value"])["name"] == "gen2"
    assert "Shipped in Darwin by a" in fs.exps[exp_id]["hypothesis"] and len(completes(fs)) == 1
    chats(tmp, "gen2")
    assert steward.propose()["challenger"] == "gen3"  # the loop isn't wedged


def test_approving_the_old_card_again_after_a_partial_failure_shows_only_the_missing_steps(env):
    steward, fs, tmp = env
    exp_id = to_ship_card(steward, fs, tmp)
    fs.fail_next = lambda c: c.args[:2] == ["flag", "update"]
    with pytest.raises(FlagsmithError):
        steward.approve("a")
    with pytest.raises(StewardError, match="already completed"):
        steward.approve("a")
    card = steward.store.read()["pending"]
    assert not any("complete" in c["command"] for c in card["commands"])
    steward.approve("a", card_id=card["id"])
    assert steward.store.read()["champion"] == "gen2" and len(completes(fs)) == 1


def test_a_failed_complete_leaves_nothing_half_done(env):
    steward, fs, tmp = env
    exp_id = to_ship_card(steward, fs, tmp)
    fs.fail_next = lambda c: c.args[0] == "api" and c.args[1].endswith("/complete/")
    with pytest.raises(FlagsmithError):
        steward.approve("a")
    s = steward.store.read()
    assert fs.exps[exp_id]["status"] == "running" and s["experiment"]["deciding"] is None and s["pending"]["kind"] == "decision"
    steward.approve("a")
    assert steward.store.read()["champion"] == "gen2"


def test_an_experiment_completed_in_the_dashboard_is_adopted_without_writing_to_flagsmith(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.complete_in_dashboard(exp_id, "variant", "gen2")
    writes = len(fs.log)
    out = steward.check()
    s = steward.store.read()
    assert not out["running"] and "outside Darwin" in out["note"]
    assert s["champion"] == "gen2" and s["experiment"] is None and len(fs.log) == writes


def test_stop_after_flagsmith_already_completed_never_completes_again(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.complete_in_dashboard(exp_id, "control")
    out = steward.abort("a", "looks wrong")
    s = steward.store.read()
    assert "gen1 kept for everyone" in out["note"] and s["experiment"] is None and s["generations"]["gen2"]["status"] == "stopped"
    assert completes(fs) == []


def test_stop_completes_with_control_and_records_it_in_flagsmith_and_the_notebook(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    shown = steward.stop_plan("anugrah", "escalations look high")
    assert "complete" in shown[0]["command"] and "Stopped in Darwin by anugrah" in shown[1]["summary"]
    steward.abort("anugrah", "escalations look high", expected=shown)
    assert fs.exps[exp_id]["rollout_decision"] == "control"
    assert fs.exps[exp_id]["hypothesis"].endswith("Stopped in Darwin by anugrah: escalations look high.")
    assert "gen2: STOPPED" in (tmp / "LAB_NOTEBOOK.md").read_text()


def test_stop_refuses_when_the_plan_changed_since_it_was_shown(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    shown = steward.stop_plan("anugrah")
    fs.exps[exp_id]["hypothesis"] += " (edited in the dashboard)"
    with pytest.raises(StewardError, match="changed"):
        steward.abort("anugrah", expected=shown)
    assert fs.exps[exp_id]["status"] == "running"


def test_a_deleted_or_paused_experiment_is_reported(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.exps[exp_id]["status"] = "paused"
    assert "paused" in steward.check()["note"]
    del fs.exps[exp_id]
    out = steward.check()
    assert not out["running"] and "no longer exists" in out["note"]
    assert steward.store.read()["generations"]["gen2"]["status"] == "stopped"


def test_reconcile_reports_an_active_experiment_darwin_isnt_tracking(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    fid = fs.features["shopbot_brain"]["id"]
    fs.exps[300] = {"id": 300, "name": "someone's test", "status": "running", "feature": {"id": fid, "name": "shopbot_brain"}}
    assert "someone's test" in steward.reconcile("a")["note"]


def test_cli_reconcile_shows_the_missing_steps_and_finishes_them(env, monkeypatch, capsys):
    import darwin.cli as cli
    steward, fs, tmp = env
    exp_id = to_ship_card(steward, fs, tmp)
    fs.fail_next = lambda c: c.args[:2] == ["flag", "update"]
    with pytest.raises(FlagsmithError):
        steward.approve("a")
    monkeypatch.setattr(cli, "steward", lambda: steward)
    cli.main(["reconcile", "--yes", "--as", "anugrah"])
    out = capsys.readouterr().out
    assert "didn't run" in out and "Done." in out
    assert steward.store.read()["champion"] == "gen2" and "Shipped in Darwin by anugrah" in fs.exps[exp_id]["hypothesis"]


def test_check_never_calls_flagsmith_while_holding_the_state_lock(env):
    from contextlib import contextmanager
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    fs.payloads[exp_id] = payload(steward.store.read()["metrics"], "gen2")
    inside = {"n": 0}
    real = steward.store.transaction

    @contextmanager
    def tracked():
        with real() as s:
            inside["n"] += 1
            try:
                yield s
            finally:
                inside["n"] -= 1
    steward.store.transaction = tracked
    for name in ("feature", "flag", "experiment", "experiments", "results", "run"):
        original = getattr(fs, name)

        def guarded(*a, _original=original, **k):
            assert inside["n"] == 0, "Flagsmith was called while lab.json was locked"
            return _original(*a, **k)
        setattr(fs, name, guarded)
    assert steward.check()["decision"]["verdict"] == "SHIP"
    assert steward.store.read()["pending"]["kind"] == "decision"


def test_keep_running_lasts_until_flagsmith_has_new_results(env, monkeypatch, capsys):
    import darwin.cli as cli
    steward, fs, tmp = env
    exp_id = to_ship_card(steward, fs, tmp)
    steward.reject("a")  # "Keep running"
    assert steward.check()["decision"]["verdict"] == "SHIP" and steward.store.read()["pending"] is None
    monkeypatch.setattr(cli, "steward", lambda: steward)
    cli.cmd_check(SimpleNamespace(actor="a", no_refresh=True))  # used to crash on print_card(None)
    assert "keep running" in capsys.readouterr().out
    fs.as_of[exp_id] = "2026-11-26T11:00:00Z"  # Flagsmith recomputed
    steward.check()
    s = steward.store.read()
    assert s["pending"]["verdict"] == "SHIP" and s["experiment"]["dismissed"] is None


def test_an_old_string_dismissal_still_works(env):
    steward, fs, tmp = env
    exp_id = to_ship_card(steward, fs, tmp)
    with steward.store.transaction() as s:
        s["pending"] = None
        s["experiment"]["dismissed"] = "SHIP"  # how older lab.json files stored it
    steward.check()
    assert steward.store.read()["pending"]["verdict"] == "SHIP"  # no as_of recorded, so new results bring it back


def experiment_chats(tmp_path, exp_id, challenger, n_per_arm):
    with open(tmp_path / "transcripts.jsonl", "a") as f:
        for arm, generation in (("control", "gen1"), (challenger, challenger)):
            for _ in range(n_per_arm):
                f.write(json.dumps({"generation": generation, "variant": arm, "experiment_id": exp_id, "resolved": True,
                                    "escalated": False, "started_at": 9e12, "cost_usd": 0.0005, "turns": []}) + "\n")


def test_auto_check_asks_flagsmith_to_recompute_only_with_50_chats_per_arm_and_new_chats(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    experiment_chats(tmp, exp_id, "gen2", 30)
    out = steward.check(refresh="auto")
    assert exp_id not in fs.refreshed_at and "50 chats" in out["note"]
    experiment_chats(tmp, exp_id, "gen2", 25)
    steward.check(refresh="auto")
    assert exp_id in fs.refreshed_at and steward.store.read()["experiment"]["refreshed_at_chats"] == 110
    del fs.refreshed_at[exp_id]  # the 5-minute window has passed, but there are no new chats
    steward.check(refresh="auto")
    assert exp_id not in fs.refreshed_at


def test_final_results_are_fetched_by_a_later_check_once_the_window_opens(env):
    import time as _time
    steward, fs, tmp = env
    exp_id = ship_gen2(steward, fs, tmp)
    assert steward.store.read()["finalize"][0]["experiment_id"] == exp_id
    assert steward.check()["finalized"] == []  # the refresh window from the last check is still closed
    fs.refreshed_at[exp_id] = _time.time() - 400
    assert "final results" in steward.check()["finalized"][0]
    notes = steward.check()["finalized"]
    s = steward.store.read()
    assert any("stored" in n for n in notes) and s["finalize"] == []
    assert s["generations"]["gen2"]["result"]["final_readout"]["is_final"] is True


def test_round_ends_with_a_check_while_an_experiment_runs(env, monkeypatch, capsys):
    import darwin.cli as cli
    import sim.run_round as rr
    steward, fs, tmp = env
    to_ship_card(steward, fs, tmp)
    monkeypatch.setattr(rr, "run_round", lambda **kw: {"chats": 0})
    monkeypatch.setattr(rr, "print_summary", lambda summary: None)
    monkeypatch.setattr(cli, "steward", lambda: steward)
    cli.main(["round", "--chats", "1"])
    assert "SHIP" in capsys.readouterr().out


def test_cli_stop_shows_the_commands_and_asks_first(env, monkeypatch, capsys):
    import darwin.cli as cli
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    exp_id = steward.approve("a")["experiment_id"]
    monkeypatch.setattr(cli, "steward", lambda: steward)
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    with pytest.raises(SystemExit):
        cli.main(["stop", "--reason", "too many escalations", "--as", "anugrah"])
    assert fs.exps[exp_id]["status"] == "running"
    out = capsys.readouterr().out
    assert "Complete the experiment and keep the champion" in out and "Stopped in Darwin by anugrah" in out
    monkeypatch.setattr("builtins.input", lambda prompt: "stop")
    cli.main(["stop", "--reason", "too many escalations", "--as", "anugrah"])
    assert fs.exps[exp_id]["rollout_decision"] == "control" and steward.store.read()["experiment"] is None


def test_rollback_shows_its_command_asks_and_writes_the_notebook(env, monkeypatch, capsys):
    import darwin.cli as cli
    steward, fs, tmp = env
    ship_gen2(steward, fs, tmp)
    monkeypatch.setattr(cli, "steward", lambda: steward)
    cli.main(["rollback", "gen1", "--yes", "--as", "anugrah"])
    assert "Make gen1 the flag's default value" in capsys.readouterr().out
    assert json.loads(fs.features["shopbot_brain"]["default_value"])["name"] == "gen1"
    assert steward.store.read()["champion"] == "gen1"
    assert "gen1 — champion again" in (tmp / "LAB_NOTEBOOK.md").read_text()


def test_rollback_refuses_when_flagsmith_changed_since_the_command_was_shown(env):
    steward, fs, tmp = env
    ship_gen2(steward, fs, tmp)
    shown = steward.rollback_plan("gen1")
    fs.features["shopbot_brain"]["variants"].append({"id": 98, "key": "hotfix", "value": "{}", "weight": 0.0})
    with pytest.raises(StewardError, match="changed"):
        steward.rollback("gen1", "a", expected=shown)
    assert steward.store.read()["champion"] == "gen2"


def test_a_human_written_challenger_cant_switch_models_unless_allowed(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    with pytest.raises(StewardError, match="model changes are off"):
        steward.propose_manual(system_prompt="You are ShopBot. Be brief.", goal="quality", summary="Sonnet", hypothesis="h",
                               actor="pm", model="claude-sonnet-5-5")


def test_the_launch_card_projects_the_experiment_cost_and_approval_refuses_past_the_budget(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")  # $0.0005 per chat
    card = steward.propose()
    assert card["cost"]["usd"] == pytest.approx(200 * 0.0005) and card["cost"]["fits"]
    assert "about $0.10 for 200 chats" in card["cost"]["text"] and "$2.00 left" in card["cost"]["text"]
    steward.claude.ledger.record(model="claude-haiku-5-5", purpose="test",
                                 usage={"input_tokens": 19_500_000, "output_tokens": 0})  # $1.95 spent, $0.05 left
    writes = len(fs.log)
    with pytest.raises(StewardError, match="needs about \\$0.10"):
        steward.approve("a", card_id=card["id"])
    assert len(fs.log) == writes and steward.store.read()["experiment"] is None


def test_a_human_written_card_can_be_sent_back_with_a_note(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    card = steward.propose_manual(system_prompt="You are ShopBot. Be brief.", goal="cost", summary="Trim", hypothesis="Shorter costs less",
                                  actor="pm", effort="low")
    assert Path(card["request_path"]).is_file()
    revised = steward.reject("anugrah", note="make it a bit longer")
    assert revised["review_note"] == "make it a bit longer" and revised["based_on"] == "pm"
    assert steward.coach.calls[-1][0] == "revise" and steward.store.read()["pending"]["id"] == revised["id"]


def test_a_revision_never_reuses_a_variant_key_flagsmith_already_has(env):
    steward, fs, tmp = env
    steward.init(GEN1)
    chats(tmp, "gen1")
    steward.propose()
    f = fs.features["shopbot_brain"]
    f["type"] = "multivariate"
    f["variants"].append({"id": 99, "key": "gen2", "value": "someone else's gen2", "weight": 0.0})
    revised = steward.reject("anugrah", note="Keep it short")
    s = steward.store.read()
    assert revised["challenger"] == "gen3" and s["generations"]["gen2"]["status"] == "rejected"
    steward.approve("anugrah", card_id=revised["id"])
    assert steward.store.read()["experiment"]["challenger"] == "gen3"
