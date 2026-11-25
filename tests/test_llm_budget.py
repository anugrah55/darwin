from types import SimpleNamespace

import pytest

from darwin.llm import BudgetExceeded, Claude, SpendLedger, estimate_cost, usage_cost


def usage(i=1000, o=100, cw=0, cr=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr)


class FakeMessages:
    def __init__(self):
        self.calls = 0

    def create(self, **request):
        self.calls += 1
        return SimpleNamespace(model=request["model"], usage=usage())


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def test_usage_cost_uses_per_model_prices():
    # 1M input + 1M output on Haiku 5.5 = $0.10 + $0.50
    assert usage_cost("claude-haiku-5-5", usage(1_000_000, 1_000_000)) == pytest.approx(0.60)
    assert usage_cost("claude-opus-5-5", usage(1_000_000, 0, 0, 1_000_000)) == pytest.approx(4.20)


def test_unknown_models_are_priced_conservatively():
    assert usage_cost("some-new-model", usage(1_000_000, 0)) >= 4.0


def test_estimate_assumes_the_full_max_tokens():
    est = estimate_cost("claude-haiku-5-5", request={"system": "x" * 3000, "messages": [], "max_tokens": 2000})
    assert est == pytest.approx(1000 * 0.10 / 1e6 + 2000 * 0.50 / 1e6)


def test_ledger_records_and_refuses_past_the_cap(tmp_path):
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=0.0015)
    claude = Claude(ledger, client=FakeClient())
    req = dict(model="claude-haiku-5-5", max_tokens=100, messages=[{"role": "user", "content": "hi"}])
    claude.create(purpose="test", **req)  # costs 1000*0.1/1e6 + 100*0.5/1e6 = $0.00015
    snap = ledger.snapshot()
    assert snap["calls"] == 1 and snap["spent_usd"] == pytest.approx(0.00015)
    assert snap["by_purpose"]["test"]["calls"] == 1
    with pytest.raises(BudgetExceeded):
        claude.create(purpose="test", **{**req, "max_tokens": 10_000})  # worst case $0.005 > what's left
    assert claude.client.messages.calls == 1  # the refused call never reached the API


def test_ledger_is_shared_between_instances(tmp_path):
    a = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    b = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    a.record(model="claude-haiku-5-5", usage=usage(), purpose="shopbot")
    b.record(model="claude-haiku-5-5", usage=usage(), purpose="coach")
    assert a.snapshot()["calls"] == 2
