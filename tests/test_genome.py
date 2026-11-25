import json

import pytest

from darwin.genome import FLAG_VALUE_LIMIT, Genome, GenomeError


def make(**overrides):
    base = dict(name="gen1", generation=1, model="claude-haiku-5-5", effort="medium", system_prompt="You are ShopBot.\nBe kind.")
    base.update(overrides)
    return Genome(**base)


def test_round_trips_through_a_flag_value():
    g = make()
    value = g.to_flag_value()
    assert json.loads(value)["name"] == "gen1"
    assert Genome.from_flag_value(value) == g


@pytest.mark.parametrize("bad", [None, "", "not json", json.dumps({"name": "x"}), 42])
def test_rejects_values_that_are_not_genomes(bad):
    with pytest.raises(GenomeError):
        Genome.from_flag_value(bad)


def test_rejects_unknown_fields_so_typos_do_not_silently_pass():
    value = json.dumps({**make().model_dump(), "temprature": 0.2})
    with pytest.raises(GenomeError):
        Genome.from_flag_value(value)


def test_refuses_genomes_too_big_for_a_flag_value():
    g = make(system_prompt="x" * 11_999, summary="y" * (FLAG_VALUE_LIMIT - 11_000))
    with pytest.raises(GenomeError):
        g.to_flag_value()


def test_child_keeps_lineage_and_settings():
    parent = make()
    child = parent.child(name="gen2", system_prompt="You are ShopBot.\nCheck policy first.", effort="low", summary="policy first")
    assert (child.generation, child.parent, child.model, child.effort) == (2, "gen1", "claude-haiku-5-5", "low")


def test_diff_shows_prompt_lines_and_setting_changes():
    parent = make()
    child = parent.child(name="gen2", system_prompt="You are ShopBot.\nCheck policy first.", effort="low")
    d = parent.diff(child)
    assert d["settings"] == {"effort": {"from": "medium", "to": "low"}}
    assert "+Check policy first." in d["prompt_diff"]
    assert "-Be kind." in d["prompt_diff"]
    assert (d["lines_added"], d["lines_removed"]) == (1, 1)
