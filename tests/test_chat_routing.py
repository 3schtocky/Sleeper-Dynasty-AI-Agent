"""Replays the local model's recorded routing answers (from
`dynasty-agent chat-eval --record`) through the tool layer's normalization,
offline, and scores them. A change to the tool schemas, the argument
normalization or tools.fix_sides that would misroute one of these real
questions fails here without Ollama running. Re-record after changing the
prompt or the model."""

import pytest

from dynasty_agent import chat_eval

RECORDING = chat_eval.load_recording()


def test_every_case_was_recorded():
    assert {c.question for c in chat_eval.CASES} <= set(RECORDING["routes"])


@pytest.mark.parametrize("case", chat_eval.CASES, ids=lambda c: c.question[:50])
def test_recorded_route_reaches_the_right_tool_with_the_right_arguments(case):
    calls = RECORDING["routes"][case.question]["calls"]
    ok, why = chat_eval.score(case, chat_eval.first_call(calls))
    assert ok, why


def test_trade_direction_is_scored_not_just_the_tool():
    backwards = {"name": "evaluate_trade", "arguments": {"send_players": ["Bijan Robinson"], "receive_players": ["Rashee Rice"]}}
    # fix_sides turns this one around (both players' owners are known) ...
    assert chat_eval.normalized(backwards)["send_players"] == ["rashee rice"]
    # ... and a case that expects the other direction fails on the raw direction check.
    case = chat_eval.Case("q", ("evaluate_trade",), chat_eval.trade(send=("bijan",), receive=("rice",)))
    assert not chat_eval.score(case, backwards)[0]
