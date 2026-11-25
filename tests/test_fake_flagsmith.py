"""The in-memory Flagsmith must refuse what the real one refuses, or the Steward tests prove nothing."""

from dataclasses import replace

import pytest

from darwin.config import Settings
from darwin.flagsmith_admin import FlagsmithError, Throttled
from tests.fake_flagsmith import FakeFlagsmith, payload


@pytest.fixture
def fs(tmp_path):
    settings = replace(Settings.load(), data_dir=tmp_path, flagsmith_admin_key="k", flagsmith_project="1",
                       flagsmith_environment="ENV")
    fake = FakeFlagsmith(settings)
    fake.run(fake.cmd_create_feature("brain", "{}", "gen1"))
    for name, agg, direction in (("resolved", "occurrence", "up"), ("escalated", "occurrence", "down"),
                                 ("cost_microusd", "sum", "down")):
        fake.run(fake.cmd_create_metric(name, name, agg, direction, ""))
    fake.run(fake.cmd_add_variant("brain", "gen2", '{"name": "gen2"}'))
    return fake


def experiment(fs):
    feature = fs.features["brain"]
    option = feature["variants"][0]["id"]
    metrics = [{"metric": m["id"], "expected_direction": "increase"} for m in fs.metric_list]
    exp = fs.run(fs.cmd_create_experiment(feature_id=feature["id"], name="brain: gen1 vs gen2", hypothesis="h", metrics=metrics,
                                          control_value="{}", control_label="gen1", weights={option: 50}, weight_labels={}))
    return exp["id"], option


def test_a_completed_experiment_cannot_complete_again(fs):
    eid, option = experiment(fs)
    fs.run(fs.cmd_start(eid))
    fs.run(fs.cmd_complete(eid, "variant", option, "gen2"))
    assert fs.experiment(eid)["rollout_decision"] == "variant" and fs.experiment(eid)["rollout_variant"] == option
    with pytest.raises(FlagsmithError) as err:
        fs.run(fs.cmd_complete(eid, "control"))
    assert err.value.status == 400


def test_results_refresh_is_limited_to_once_every_five_minutes(fs):
    eid, _ = experiment(fs)
    with pytest.raises(FlagsmithError):
        fs.run(fs.cmd_refresh_results(eid))  # not started yet
    fs.run(fs.cmd_start(eid))
    now = [1_000_000.0]
    fs.clock = lambda: now[0]
    fs.run(fs.cmd_refresh_results(eid))
    with pytest.raises(Throttled):
        fs.run(fs.cmd_refresh_results(eid))
    now[0] += 301
    fs.run(fs.cmd_refresh_results(eid))


def test_no_inference_below_fifty_identities_or_five_conversions(fs):
    eid, _ = experiment(fs)
    ids = {m["name"]: m["id"] for m in fs.metric_list}
    fs.payloads[eid] = payload(ids, "gen2", escalated=(3, 24))
    metrics = {m["metric_id"]: m for m in fs.results(eid)["payload"]["metrics"]}
    assert metrics[ids["escalated"]]["inference"]["gen2"] is None  # control has 3 escalations, under the floor of 5
    assert metrics[ids["resolved"]]["inference"]["gen2"] is not None
    fs.payloads[eid] = payload(ids, "gen2", n=(40, 40))
    assert all(m["inference"]["gen2"] is None for m in fs.results(eid)["payload"]["metrics"])


def test_patching_the_hypothesis_works_after_completion(fs):
    eid, _ = experiment(fs)
    fs.run(fs.cmd_start(eid))
    fs.run(fs.cmd_complete(eid, "control"))
    fs.run(fs._api(f"experiments/{eid}/", summary="Record", method="PATCH", body={"hypothesis": "h\n\nKilled in Darwin"}))
    assert fs.experiment(eid)["hypothesis"].endswith("Killed in Darwin")
