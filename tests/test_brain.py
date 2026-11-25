from types import SimpleNamespace

from darwin.brain import Brain
from darwin.genome import Genome

GEN1 = Genome(name="gen1", generation=1, model="claude-haiku-5-5", system_prompt="You are ShopBot.")
GEN2 = GEN1.child(name="gen2", system_prompt="You are ShopBot. Check policy first.")


class FakeSDK:
    def __init__(self, flag=None, error=None):
        self.flag, self.error, self.events = flag, error, []

    def get_experiment_flag(self, name, identifier, traits=None):
        if self.error:
            raise self.error
        return self.flag

    def track_event(self, event, identifier=None, value=None):
        self.events.append((event, identifier, value))


def flag(value, *, enabled=True, variant="gen2", experiment=(7, "gen1 vs gen2", True)):
    exp = SimpleNamespace(id=experiment[0], name=experiment[1], in_experiment=experiment[2]) if experiment else None
    return SimpleNamespace(enabled=enabled, value=value, variant=variant, experiment=exp)


def test_assigns_the_variant_genome_and_experiment():
    sdk = FakeSDK(flag(GEN2.to_flag_value()))
    a = Brain("shopbot_brain", fallback=GEN1, client=sdk).assign("u1")
    assert (a.genome.name, a.variant, a.source, a.experiment_id, a.in_experiment) == ("gen2", "gen2", "flag", 7, True)


def test_tracks_events_for_the_assigned_identity():
    sdk = FakeSDK(flag(GEN2.to_flag_value()))
    a = Brain("shopbot_brain", fallback=GEN1, client=sdk).assign("u1")
    a.track("resolved")
    a.track("tokens_used", 1234)
    assert sdk.events == [("resolved", "u1", None), ("tokens_used", "u1", 1234)]


def test_falls_back_when_flagsmith_is_down():
    a = Brain("shopbot_brain", fallback=GEN1, client=FakeSDK(error=ConnectionError("boom"))).assign("u1")
    assert a.source == "fallback" and a.genome == GEN1 and "boom" in a.reason


def test_falls_back_when_the_flag_is_off_or_garbage():
    off = Brain("shopbot_brain", fallback=GEN1, client=FakeSDK(flag(GEN2.to_flag_value(), enabled=False))).assign("u1")
    junk = Brain("shopbot_brain", fallback=GEN1, client=FakeSDK(flag("{not json"))).assign("u1")
    assert off.source == junk.source == "fallback"
    assert "off" in off.reason and "genome" in junk.reason


def test_no_running_experiment_still_serves_the_flag_value():
    a = Brain("shopbot_brain", fallback=GEN1, client=FakeSDK(flag(GEN2.to_flag_value(), variant=None, experiment=None))).assign("u1")
    assert (a.genome.name, a.variant, a.in_experiment) == ("gen2", "control", False)


def test_an_unreachable_flagsmith_serves_the_last_good_champion(tmp_path):
    sdk = FakeSDK(flag(GEN2.to_flag_value(), variant=None, experiment=None))
    brain = Brain("shopbot_brain", fallback=GEN1, client=sdk, cache_path=tmp_path / "last-good.json")
    assert brain.assign("u1").genome.name == "gen2"
    sdk.error = ConnectionError("Flagsmith is down")
    a = brain.assign("u2")
    assert (a.genome.name, a.source, a.variant) == ("gen2", "cache", "last-good") and "down" in a.reason
    # a restarted app remembers it too
    restarted = Brain("shopbot_brain", fallback=GEN1, client=FakeSDK(error=ConnectionError("still down")),
                      cache_path=tmp_path / "last-good.json")
    assert restarted.assign("u3").genome.name == "gen2"


def test_turning_the_flag_off_is_a_kill_switch_even_with_a_cached_genome(tmp_path):
    sdk = FakeSDK(flag(GEN2.to_flag_value(), variant=None, experiment=None))
    brain = Brain("shopbot_brain", fallback=GEN1, client=sdk, cache_path=tmp_path / "last-good.json")
    brain.assign("u1")
    sdk.flag = flag(GEN2.to_flag_value(), enabled=False, variant=None, experiment=None)
    a = brain.assign("u2")
    assert (a.genome.name, a.source) == ("gen1", "fallback") and "kill switch" in a.reason


def test_a_challenger_is_never_cached_and_a_bad_value_serves_the_last_good_genome(tmp_path):
    champion = FakeSDK(flag(GEN1.to_flag_value(), variant="control"))
    brain = Brain("shopbot_brain", fallback=GEN1, client=champion)
    brain.assign("u1")
    champion.flag = flag(GEN2.to_flag_value(), variant="gen2")  # this user is in the challenger arm
    brain.assign("u2")
    assert brain.last_good.name == "gen1"
    champion.flag = flag("{broken json", variant=None, experiment=None)
    a = brain.assign("u3")
    assert (a.genome.name, a.source) == ("gen1", "cache")


def test_with_nothing_cached_an_outage_serves_the_bundled_fallback():
    a = Brain("shopbot_brain", fallback=GEN1, client=FakeSDK(error=TimeoutError("slow"))).assign("u1")
    assert a.source == "fallback" and "no last good genome" in a.reason
