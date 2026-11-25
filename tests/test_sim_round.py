import json
import random
from datetime import date

import httpx
import pytest

from sim.run_round import BudgetStop, run_chat
from sim.scenarios import make_case


def shopbot(message_status: int):
    ended = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/chat/start":
            return httpx.Response(200, json={"session_id": "s1", "generation": "gen1", "variant": "control"})
        if path.endswith("/message"):
            return httpx.Response(message_status, json={"detail": "boom"})
        if path.endswith("/end"):
            ended.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://shopbot"), ended


@pytest.mark.parametrize("status, error", [(500, httpx.HTTPStatusError), (402, BudgetStop)])
def test_a_chat_that_breaks_off_still_reports_an_outcome(status, error):
    http, ended = shopbot(status)
    with pytest.raises(error):
        run_chat(http, make_case(random.Random(1), date(2026, 11, 26)), "sim-x")
    assert len(ended) == 1 and ended[0]["resolved"] is False and ended[0]["note"].startswith("(chat ended early")
