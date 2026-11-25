import random
from datetime import date

import pytest

from sim.customer import CustomerState, next_message
from sim.scenarios import SCENARIOS, make_case

TODAY = date(2026, 11, 26)


def case(name, seed=1):
    return make_case(random.Random(seed), TODAY, name)


def call(name, is_error=False, **inp):
    return {"name": name, "input": inp, "output": {}, "is_error": is_error}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_every_scenario_builds_orders_an_opening_and_fails_on_silence(name):
    c = case(name)
    assert c.orders and c.opening
    ok, why = c.check([], ["Sorry, I can't help with that."])
    assert ok is False and why


def test_cases_are_randomised_per_seed():
    a, b = case("standard_refund", 1), case("standard_refund", 2)
    assert a.orders[0]["order_id"] != b.orders[0]["order_id"] and a.email != b.email


def test_standard_refund_rule():
    c = case("standard_refund")
    oid = c.order_id
    assert c.check([call("create_return", order_id=oid, reason="changed_mind", resolution="refund")], [])[0]
    assert not c.check([call("escalate_to_human"), call("create_return", order_id=oid, resolution="refund")], [])[0]


def test_late_window_needs_store_credit_not_refund():
    c = case("late_window_return")
    oid = c.order_id
    assert c.check([call("create_return", order_id=oid, resolution="store_credit")], [])[0]
    assert not c.check([call("create_return", order_id=oid, resolution="refund")], [])[0]


def test_expired_return_must_decline_and_explain():
    c = case("expired_return")
    assert c.check([call("lookup_order", order_id=c.order_id)], ["Returns are accepted for up to 45 days."])[0]
    assert not c.check([], ["Sorry, that's past our return window."])[0]
    assert not c.check([call("create_return", order_id=c.order_id, resolution="store_credit")], ["45 days"])[0]


def test_angry_late_requires_the_exact_credit_and_no_escalation():
    c = case("angry_late")
    amount = c.facts["credit"]
    assert c.check([call("issue_credit", order_id=c.order_id, amount_usd=amount, reason="late")], [])[0]
    assert not c.check([call("issue_credit", order_id=c.order_id, amount_usd=amount + 15, reason="late")], [])[0]
    assert not c.check([call("escalate_to_human")], [])[0]


def test_high_value_refund_must_escalate():
    c = case("high_value_refund")
    assert c.check([call("escalate_to_human", reason="refund over $500", summary="")], [])[0]
    assert not c.check([call("create_return", order_id=c.order_id, resolution="refund")], [])[0]


def test_two_requests_needs_both():
    c = case("two_requests")
    cancel_id, track = c.orders[0]["order_id"], c.orders[1]["tracking_number"]
    assert c.check([call("cancel_order", order_id=cancel_id)], [f"Cancelled. Tracking: {track}"])[0]
    assert not c.check([call("cancel_order", order_id=cancel_id)], ["Cancelled!"])[0]


def test_failed_tool_calls_do_not_count():
    c = case("standard_refund")
    assert not c.check([call("create_return", is_error=True, order_id=c.order_id, resolution="refund")], [])[0]


def test_customer_answers_questions_and_leaves_after_statements():
    c = case("missing_order_id")
    state = CustomerState()
    assert c.email in next_message(c, "Could you share your order number?", state)
    assert next_message(c, "Would you like a refund or a replacement?", state) == "A refund, please."
    assert next_message(c, "Your refund is on its way.", state) is None


def test_customer_uses_scenario_replies_once():
    c = case("damaged_item")
    state = CustomerState()
    first = next_message(c, "Could you send us a photo of the damage?", state)
    assert "photos" in first
    assert next_message(c, "Could you send us a photo of the damage?", state) == c.preference


def test_customer_stops_after_max_turns():
    c = case("standard_refund")
    state = CustomerState(turn=c.max_turns)
    assert next_message(c, "Anything else?", state) is None


def test_wismo_needs_tracking_and_the_unrequested_credit():
    c = case("wismo_late")
    o, amount = c.orders[0], c.facts["credit"]
    assert c.check([call("issue_credit", order_id=o["order_id"], amount_usd=amount, reason="late")], [f"Tracking: {o['tracking_number']}"])[0]
    assert not c.check([], [f"Tracking: {o['tracking_number']}"])[0]


def test_slightly_late_must_not_get_a_goodwill_credit():
    c = case("slightly_late_wants_credit")
    o = c.orders[0]
    assert c.check([], [f"Sorry! It's on its way: {o['tracking_number']}"])[0]
    assert not c.check([call("issue_credit", order_id=o["order_id"], amount_usd=5, reason="sorry")], [o["tracking_number"]])[0]


def test_damaged_final_sale_is_still_covered():
    c = case("damaged_final_sale")
    assert c.check([call("create_return", order_id=c.order_id, reason="damaged", resolution="replacement")], [])[0]
    assert not c.check([], ["Sorry, final-sale items can't be returned."])[0]


def test_price_drop_declines_without_credit():
    c = case("price_drop")
    assert c.check([], ["We don't offer price adjustments after purchase."])[0]
    assert not c.check([call("issue_credit", order_id=c.order_id, amount_usd=20, reason="price")], ["price"])[0]


def test_wrong_order_id_needs_the_real_order():
    c = case("wrong_order_id")
    real = c.orders[0]["order_id"]
    assert c.order_id != real
    assert c.check([call("create_return", order_id=real, reason="changed_mind", resolution="refund")], [])[0]


def test_customer_answers_why_and_gives_up_when_out_of_patience():
    c = case("damaged_item")
    c.max_turns = 2
    state = CustomerState()
    assert next_message(c, "Sorry about that! What happened to it?", state) == "It arrived broken."
    assert next_message(c, "Would you like a refund or a replacement?", state) is None
    assert state.gave_up


def test_patience_is_randomised_but_respects_the_minimum():
    turns = {make_case(random.Random(i), TODAY, "wrong_order_id").max_turns for i in range(40)}
    assert min(turns) >= 3 and len({make_case(random.Random(i), TODAY).max_turns for i in range(40)}) > 1


def test_customer_answers_requests_phrased_without_a_question_mark():
    c = case("missing_order_id")
    state = CustomerState()
    assert c.email in next_message(c, "I can help with that! I'll need the email address on your account.", state)


def test_customer_answers_conditional_requests():
    c = case("missing_order_id")
    msg = "I don't need the confirmation. If you give me the email address on your account, I'll look it up."
    assert c.email in next_message(c, msg, CustomerState())


def test_wismo_customers_differ_on_unrequested_escalation_offers():
    answers = {next_message(case("wismo_late", s), "Done! Would you like a specialist to investigate?", CustomerState()) for s in range(30)}
    assert any("No thanks" in a for a in answers) and any("Yes" in a for a in answers)
