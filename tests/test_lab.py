import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from darwin.config import Settings
from darwin.lab.app import create_app
from darwin.llm import SpendLedger
from darwin.steward import Steward
from tests.fake_flagsmith import FakeFlagsmith
from tests.test_steward import GEN1, FakeCoach, chats


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv("DARWIN_APPROVER", "maya")
    settings = replace(Settings.load(), data_dir=tmp_path, notebook_path=tmp_path / "LAB_NOTEBOOK.md",
                       flagsmith_admin_key="k", flagsmith_project="1", flagsmith_environment="ENV")
    steward = Steward(settings, admin=FakeFlagsmith(settings), claude=SimpleNamespace(ledger=SpendLedger(tmp_path / "spend.json", 2.0)),
                      coach=FakeCoach(), wait_for_results=0)
    steward.init(GEN1)
    chats(tmp_path, "gen1")
    app = create_app(settings, steward=steward, auto_check=False)
    return TestClient(app), steward


def wait_idle(app_lab, timeout=5.0):
    deadline = time.time() + timeout
    while app_lab._worker and app_lab._worker.is_alive() and time.time() < deadline:
        time.sleep(0.02)


def test_state_offers_a_default_approver(lab):
    client, _ = lab
    assert client.get("/api/state").json()["default_approver"] == "maya"


def test_the_goal_picked_on_the_page_is_the_one_the_coach_gets(lab):
    client, steward = lab
    assert client.post("/api/evolve", json={"actor": "maya", "goal": "cost"}).json() == {"started": True}
    wait_idle(client.app.state.lab)
    card = steward.store.read()["pending"]
    assert card["goal"] == "cost" and steward.coach.calls[-1][1]["goal"] == "cost"


def test_the_page_keeps_inputs_open_details_and_armed_buttons_across_refreshes():
    """The page repaints a panel only when its data changes and never replaces a focused field."""
    from pathlib import Path

    page = (Path(__file__).parent.parent / "darwin" / "lab" / "static" / "index.html").read_text()
    assert 'id="approver"' in page.split("<script>")[0]  # the approver field is static markup, not re-rendered
    script = page.split("<script>")[1]
    assert "if (!el || painted[id] === key) return;" in script
    assert "/^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)" in script
    assert 'querySelectorAll("details[data-key]")' in script
    assert "ui.confirmFor" in script and "ui.stopArmedFor" in script and "S.default_approver" in script
    assert 'post("/api/evolve", { goal: ui.goal })' in script
