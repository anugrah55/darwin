import json
from pathlib import Path

import pytest

from darwin.results import Arm, Inference, MetricReadout, Readout, chi_squared_p, parse_readout
from darwin.rules import (COLLECTING_DATA, DISCARD, HOLD, KEEP_RUNNING, KILL, SHIP, DecisionPolicy, Guardrail, decide,
                          p_bad_from_counts, p_bad_from_inference)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "results_running.json").read_text())
QUALITY = DecisionPolicy(primary="resolved", guardrails=(
    Guardrail("escalated", 0.05, "up", "points", "escalations", plural=True),
    Guardrail("cost_microusd", 0.30, "up", "relative", "cost per chat")))
COST = DecisionPolicy(primary="cost_microusd", guardrails=(
    Guardrail("resolved", 0.05, "down", "points", "resolution"),
    Guardrail("escalated", 0.05, "up", "points", "escalations", plural=True)))


def metric(name, *, direction="up", aggregation="occurrence", inference=None, n=(100, 100), total=(40, 70)):
    arms = {"control": Arm("control", n[0], total[0], total[0]), "gen2": Arm("gen2", n[1], total[1], total[1])}
    return MetricReadout(1, name, aggregation, direction, "increase", arms, inference)


def readout(*metrics, srm=None, as_of="2026-11-26T10:00:00Z"):
    n = next(iter(metrics)).arms if metrics else {}
    r = Readout(7, "running", "gen2", as_of, False, None, {m.name: m for m in metrics},
                {k: a.n for k, a in n.items()} or {"control": 100, "gen2": 100})
    r.srm_p_value, r.srm_source = srm, "darwin" if srm is not None else None
    return r


WINNER = Inference(0.5, 0.2, 0.8, 0.999)


def safe_escalations(n=(100, 100)):
    return metric("escalated", direction="down", inference=Inference(-0.5, -0.8, -0.2, 0.99), n=n, total=(20, 10))


def safe_cost(n=(100, 100)):
    return metric("cost_microusd", direction="down", aggregation="sum", inference=Inference(0.05, 0.0, 0.10, 0.1),
                  n=n, total=(60000, 63000))


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
    d = decide(readout(metric("resolved", inference=None)), QUALITY)
    assert d.verdict == COLLECTING_DATA and "50 identities" in d.reasons[0]


def test_a_winner_ships_only_when_every_guardrail_is_proven_safe():
    d = decide(readout(metric("resolved", inference=WINNER), safe_escalations(), safe_cost()), QUALITY)
    assert d.verdict == SHIP and d.rollout_decision == "variant" and d.needs_approval
    assert [g.state for g in d.guardrails] == ["ok", "ok"]


def test_a_winner_with_missing_guardrail_data_keeps_running():
    d = decide(readout(metric("resolved", inference=WINNER)), QUALITY)
    assert d.verdict == KEEP_RUNNING and not d.needs_approval
    assert "escalations aren't proven safe yet (no data)" in d.headline


def test_a_winner_with_an_uncertain_guardrail_keeps_running_and_says_why():
    # escalations 8% -> 10%: probably fine, but not proven to be within 5 points at 100 chats per arm
    unsure = metric("escalated", direction="down", inference=Inference(0.25, -0.6, 1.1, 0.3), total=(8, 10))
    d = decide(readout(metric("resolved", inference=WINNER), unsure, safe_cost()), QUALITY, champion="gen1")
    g = d.guardrails[0]
    assert d.verdict == KEEP_RUNNING and g.state == "uncertain" and 0.05 < g.p_bad < 0.95
    assert d.headline.startswith("gen2 resolves more chats (99.9%), but escalations aren't proven safe yet (")
    assert "chance they rose more than 5 pts" in d.headline and d.headline.endswith("keep running.")
    assert "8% → 10% (+2.0 pts)" in g.text and g.margin == "+5 pts"


def test_a_guardrail_breach_kills_even_a_winner():
    worse = metric("escalated", direction="down", inference=Inference(0.6, 0.15, 1.05, 0.01), total=(30, 48))
    d = decide(readout(metric("resolved", inference=WINNER), worse, safe_cost()), QUALITY)
    assert d.verdict == KILL and d.rollout_decision == "control"
    assert d.guardrails[0].state == "breach" and d.guardrails[0].p_bad >= 0.95
    assert "escalations rose more than the 5 pts margin" in d.headline


def test_a_guardrail_flagsmith_wont_score_is_computed_from_raw_counts():
    # the champion escalated 3 of 80 chats, under Flagsmith's 5-conversion floor; the challenger 24 of 80
    blind = metric("escalated", direction="down", inference=None, n=(80, 80), total=(3, 24))
    d = decide(readout(metric("resolved", inference=WINNER, n=(80, 80), total=(56, 70)), blind,
                       safe_cost(n=(80, 80))), QUALITY, min_per_arm=80)
    g = d.guardrails[0]
    assert g.source == "counts" and g.state == "breach" and d.verdict == KILL
    assert "from raw counts" in g.text


def test_raw_counts_that_look_alike_are_uncertain_not_safe():
    alike = metric("escalated", direction="down", inference=None, n=(60, 60), total=(3, 4))
    g = decide(readout(metric("resolved", inference=WINNER, n=(60, 60)), alike, safe_cost(n=(60, 60))), QUALITY).guardrails[0]
    assert g.source == "counts" and g.state == "uncertain"


def test_nothing_is_called_before_the_planned_sample_except_a_kill():
    small = (80, 80)
    winner = metric("resolved", inference=WINNER, n=small)
    d = decide(readout(winner, safe_escalations(small), safe_cost(small)), QUALITY)
    assert d.verdict == COLLECTING_DATA and "80 of 100 chats per arm" in d.headline
    assert decide(readout(winner, safe_escalations(small), safe_cost(small)), QUALITY, min_per_arm=80).verdict == SHIP
    worse = metric("escalated", direction="down", inference=Inference(0.9, 0.4, 1.4, 0.0), n=small, total=(16, 30))
    assert decide(readout(winner, worse, safe_cost(small)), QUALITY).verdict == KILL


def test_a_clear_loser_is_discarded():
    d = decide(readout(metric("resolved", inference=Inference(-0.4, -0.6, -0.2, 0.01))), QUALITY)
    assert d.verdict == DISCARD and d.rollout_decision == "control"


def test_unclear_keeps_running_without_asking_for_approval():
    d = decide(readout(metric("resolved", inference=Inference(0.05, -0.1, 0.2, 0.7)), safe_escalations(), safe_cost()), QUALITY)
    assert d.verdict == KEEP_RUNNING and not d.needs_approval


def test_cost_guardrail_tolerates_a_modest_increase():
    up_12 = metric("cost_microusd", direction="down", aggregation="sum", inference=Inference(0.12, 0.05, 0.19, 0.0),
                   total=(60000, 67200))
    d = decide(readout(metric("resolved", inference=WINNER), safe_escalations(), up_12), QUALITY)
    g = next(g for g in d.guardrails if g.metric == "cost_microusd")
    assert g.state == "ok" and g.p_bad < 0.01 and d.verdict == SHIP


def test_cost_goal_guards_resolution_in_points():
    cheaper = metric("cost_microusd", direction="down", aggregation="sum", inference=Inference(-0.25, -0.35, -0.15, 0.999),
                     n=(200, 200), total=(120000, 90000))
    resolution_down_10_pts = metric("resolved", inference=Inference(-0.11, -0.16, -0.06, 0.0), n=(200, 200), total=(180, 160))
    escalations = metric("escalated", direction="down", inference=Inference(0.0, -0.5, 0.5, 0.5), n=(200, 200), total=(10, 10))
    d = decide(readout(cheaper, resolution_down_10_pts, escalations), COST)
    assert d.guardrails[0].state == "breach" and d.verdict == KILL


def test_broken_split_holds():
    d = decide(readout(metric("resolved", inference=WINNER), safe_escalations(), safe_cost(), srm=0.0001), QUALITY)
    assert d.verdict == HOLD and not d.needs_approval


def test_p_bad_from_flagsmith_inference_converts_a_points_margin_with_the_control_rate():
    g = Guardrail("escalated", 0.05, "up", "points")
    # control 10%: +5 pts is a +50% relative change; lift +50% with a symmetric interval is a coin flip
    assert p_bad_from_inference(0.5, 0.0, 1.0, g, 0.10) == pytest.approx(0.5)
    assert p_bad_from_inference(0.0, -0.2, 0.2, g, 0.10) < 0.001
    down = Guardrail("resolved", 0.05, "down", "points")
    assert p_bad_from_inference(-0.2, -0.3, -0.1, down, 0.9) > 0.99


def test_p_bad_from_counts_is_deterministic_and_sensible():
    worse = p_bad_from_counts(80, 3, 80, 24, 0.05, "up", "points")
    assert worse == p_bad_from_counts(80, 3, 80, 24, 0.05, "up", "points") and worse > 0.99
    assert p_bad_from_counts(400, 20, 400, 18, 0.05, "up", "points") < 0.01
    assert p_bad_from_counts(400, 360, 400, 300, 0.05, "down", "points") > 0.99
