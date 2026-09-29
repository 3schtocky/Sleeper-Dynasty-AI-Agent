"""The chat loop with a scripted stand-in for the model: what gets shown,
what gets dropped, what the router sees, and the stats line."""

import io

import pytest

from dynasty_agent import chat, grounding, llm, tools


class FakeModel:
    """Plays back scripted routing decisions and takes."""

    def __init__(self, routes, takes=()):
        self.routes, self.takes, self.seen = list(routes), list(takes), []
        self.model = "fake:4b"

    def route(self, messages, tool_list):
        self.seen.append(messages)
        calls, text = self.routes.pop(0)
        return calls, text, llm.Stats("fake:4b", eval_count=20, eval_duration_ns=10**9)

    def stream(self, messages):
        for piece in self.takes.pop(0):
            yield piece
        yield llm.Stats("fake:4b", eval_count=60, eval_duration_ns=2 * 10**9, first_token_s=0.4)


@pytest.fixture
def fake_tool(monkeypatch):
    def run(conn, name, args):
        if args.get("ambiguous"):
            return tools.ToolResult(name, clarification="Which Kenneth Walker do you mean: A or B?")
        return tools.ToolResult(name, numbers="Net market value (players + picks): -221\nWin probability: 78.1%",
                                compact={"net_market_value": "-221"})

    monkeypatch.setattr(tools, "run_tool", run)


def session(model):
    return chat.ChatSession(conn=None, client=model, out=io.StringIO(), width=80)


def test_numbers_block_then_a_grounded_take_then_stats(fake_tool):
    s = session(FakeModel([([{"name": "evaluate_trade", "arguments": {}}], "")],
                          [["Don't do it. ", "It nets -221 in market value."]]))
    s.ask("Should I trade Coleman?")
    out = s.out.getvalue()
    assert out.index("Net market value") < out.index("Don't do it.") < out.index("It nets -221")
    assert "fake:4b · 30.0 tok/s · first words 0.4s · tool: evaluate_trade" in out


def test_a_sentence_with_an_invented_number_is_never_shown(fake_tool):
    s = session(FakeModel([([{"name": "evaluate_trade", "arguments": {}}], "")],
                          [["Don't do it. ", "You'd lose about 300 points. ", "Also 5 more things."]]))
    turn = s.ask("Should I trade Coleman?")
    out = s.out.getvalue()
    assert "Don't do it." in out and "lose about 300" not in out
    assert "quoted 300, which isn't in the numbers above" in out
    assert "5 more things" not in out and turn.reply == "Don't do it."


def test_no_tool_reply_with_a_made_up_number_is_replaced(fake_tool):
    s = session(FakeModel([([], "Your team scores 142 points a week.")]))
    s.ask("how good am I")
    assert "142" not in s.out.getvalue() and "outside what I can answer" in s.out.getvalue()


def test_a_plain_no_tool_reply_is_shown(fake_tool):
    s = session(FakeModel([([], "Paris. I can help with lineups, trades and waivers.")]))
    s.ask("What's the capital of France?")
    assert s.out.getvalue().startswith("Paris.") and "tool: none" in s.out.getvalue()


def test_only_a_clarifying_exchange_reaches_the_router_next_turn(fake_tool):
    model = FakeModel([
        ([{"name": "evaluate_trade", "arguments": {}}], ""),
        ([{"name": "evaluate_trade", "arguments": {"ambiguous": True}}], ""),
        ([{"name": "evaluate_trade", "arguments": {}}], ""),
    ], [["Fine."], ["Fine."]])
    s = session(model)
    s.ask("Trade Coleman?")
    s.ask("Trade Kenneth Walker for Puka?")
    s.ask("the RB")
    assert [m["role"] for m in model.seen[1]] == ["system", "user"]  # an answered question isn't replayed
    assert [m["content"] for m in model.seen[2][1:]] == [
        "Trade Kenneth Walker for Puka?", "Which Kenneth Walker do you mean: A or B?", "the RB",
    ]


def test_commands(fake_tool):
    s = session(FakeModel([([{"name": "my_team", "arguments": {"x": 1}}], "")], [["Ok."]]))
    assert s.command("/raw") and "ask a question first" in s.out.getvalue()
    s.ask("am I good?")
    s.command("/raw")
    assert '"tool": "my_team"' in s.out.getvalue() and '"summary_given_to_model"' in s.out.getvalue()
    s.command("/stats off")
    assert not s.show_stats
    assert s.command("/nope") and "Unknown command" in s.out.getvalue()
    assert s.command("/quit") is False


def test_stats_line_says_loading_after_a_cold_start():
    cold = llm.Stats("qwen3:4b-instruct", eval_count=60, eval_duration_ns=2 * 10**9, load_duration_ns=18 * 10**9, first_token_s=19.0)
    line = chat.format_stats(cold, "set_lineup", 100)
    assert "loading model... 18s" in line and "first words" not in line
    assert line.endswith("tool: set_lineup") and len(line) == 100  # right-aligned


@pytest.mark.parametrize("pieces, expected", [
    (["Start T.J. Hockenson. ", "He has 8.3."], ["Start T.J. Hockenson.", "He has 8.3."]),
    (["Win probability is 78", ".1%. Sell!", " Now?"], ["Win probability is 78.1%.", "Sell!", "Now?"]),
    (["no period at the end"], ["no period at the end"]),
])
def test_sentences_split_on_real_ends_only(pieces, expected):
    assert list(chat.sentences(pieces)) == expected


def test_grounding_normalizes_how_numbers_are_written():
    allowed = grounding.allowed_numbers("market 1890, net -221, bid $27, 78.1%, +3.0 to lineup, 36th percentile")
    for fine in ("1,890", "221", "$27", "78.1%", "3", "36th", "+3.0"):
        assert not grounding.ungrounded(fine, allowed), fine
    assert grounding.ungrounded("about 78%", allowed) == {"78"}
    assert grounding.ungrounded("QB4 and WR2", allowed) == set()  # position ranks aren't numbers
