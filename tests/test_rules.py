import json
from pathlib import Path

from darwin.results import Arm, Inference, MetricReadout, Readout, chi_squared_p, parse_readout
from darwin.rules import COLLECTING_DATA, DISCARD, HOLD, KEEP_RUNNING, KILL, SHIP, decide

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "results_running.json").read_text())


def metric(name, *, direction="up", aggregation="occurrence", inference=None, n=(100, 100), total=(40, 70)):
    arms = {"control": Arm("control", n[0], total[0], total[0]), "gen2": Arm("gen2", n[1], total[1], total[1])}
    return MetricReadout(1, name, aggregation, direction, "increase", arms, inference)


def readout(*metrics, srm=None, as_of="2026-11-26T10:00:00Z"):
    r = Readout(7, "running", "gen2", as_of, False, None, {m.name: m for m in metrics}, {"control": 100, "gen2": 100})
    r.srm_p_value, r.srm_source = srm, "darwin" if srm is not None else None
    return r


def test_parses_a_real_flagsmith_results_payload():
    r = parse_readout(FIXTURE["experiment"], FIXTURE["results"], challenger="gen2", expected_split={"control": 0.5, "gen2": 0.5})
    m = next(iter(r.metrics.values()))
    assert m.arms["gen2"].n == 58 and m.arms["control"].total == 32
    assert m.inference.chance_to_win > 0.99
    assert r.exposures == {"gen2": 58, "control": 72}
    assert r.srm_source == "darwin" and 0.2 < r.srm_p_value < 0.25  # 72/58 on a 50/50 split is fine


def test_chi_squared_matches_known_values():
    assert chi_squared_p([50, 50], [0.5, 0.5]) == 1.0
    assert chi_squared_p([77, 53], [0.5, 0.5]) < 0.05
    assert chi_squared_p([10, 0], [1.0, 0.0]) is None


def test_no_results_yet_means_collecting_data():
    assert decide(None).verdict == COLLECTING_DATA
    assert decide(readout(metric("resolved"), as_of=None)).verdict == COLLECTING_DATA


def test_primary_without_inference_means_collecting_data():
    d = decide(readout(metric("resolved", inference=None)))
    assert d.verdict == COLLECTING_DATA and "50 identities" in d.reasons[0]


def test_clear_winner_ships_and_completes_with_the_variant():
    d = decide(readout(metric("resolved", inference=Inference(0.7, 0.3, 1.1, 0.998))))
    assert d.verdict == SHIP and d.rollout_decision == "variant" and d.needs_approval


def test_clear_loser_is_discarded():
    d = decide(readout(metric("resolved", inference=Inference(-0.4, -0.6, -0.2, 0.01))))
    assert d.verdict == DISCARD and d.rollout_decision == "control"


def test_unclear_keeps_running_without_asking_for_approval():
    d = decide(readout(metric("resolved", inference=Inference(0.05, -0.1, 0.2, 0.7))))
    assert d.verdict == KEEP_RUNNING and not d.needs_approval


def test_guardrail_breach_kills_even_a_winner():
    winner = metric("resolved", inference=Inference(0.5, 0.2, 0.8, 0.999))
    worse_escalations = metric("escalated", direction="down", inference=Inference(0.6, 0.15, 1.05, 0.01))
    d = decide(readout(winner, worse_escalations))
    assert d.verdict == KILL and d.rollout_decision == "control"
    assert next(g for g in d.guardrails if g.metric == "escalated").state == "breach"


def test_token_guardrail_tolerates_a_modest_increase():
    winner = metric("resolved", inference=Inference(0.5, 0.2, 0.8, 0.999))
    tokens_up_20 = metric("cost_microusd", direction="down", aggregation="sum", inference=Inference(0.2, 0.1, 0.3, 0.0))
    d = decide(readout(winner, tokens_up_20))
    assert d.verdict == SHIP
    assert next(g for g in d.guardrails if g.metric == "cost_microusd").state in ("ok", "watch")


def test_broken_split_holds():
    d = decide(readout(metric("resolved", inference=Inference(0.5, 0.2, 0.8, 0.999)), srm=0.0001))
    assert d.verdict == HOLD and not d.needs_approval
