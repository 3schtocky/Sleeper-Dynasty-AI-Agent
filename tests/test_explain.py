"""The explanations: every step is computed from the live constants, the chain
must reproduce the number the user was shown, and a chain that doesn't is
never displayed."""

import pytest

from dynasty_agent import explain, metrics, valuation
from dynasty_agent.errors import AgentError
from tests.test_integration import add_player


def add_last_season(conn, pid, fppg, games):
    for week in range(1, games + 1):
        conn.execute(
            "INSERT INTO weekly_stats (player_id, season, week, position, fantasy_points, fetched_at) "
            "VALUES (?, '2024', ?, 'WR', ?, 't')",
            (pid, week, fppg),
        )


def last_values(e):
    return [s.value for s in e.steps]


@pytest.mark.parametrize("position, age", [("WR", 25), ("RB", 28), ("QB", 33), ("TE", 22), ("RB", None)])
def test_the_chain_reproduces_the_valuation_for_every_position_and_age(conn, position, age):
    add_player(conn, "p", position, 14.0, age=age)
    v = valuation.player_valuations(conn, 2025)["p"]
    e = explain.explain_player_value(conn, 2025, "p")
    assert [s.label for s in e.steps] == [
        "Points per game", "Production score", "Age factor", "Situation factor", "Win-now value", "Three-year value",
    ]
    assert e.steps[-2].value == f"{v['win_now_value']:.1f}"
    assert e.steps[-1].value == f"{v['three_year_value']:.1f}"
    assert e.subject == {"player_id": "p", "full_name": "Player p"}


def test_a_veteran_with_a_last_season_shows_the_blend(conn):
    add_player(conn, "p", "WR", 10.0, games=4)
    add_last_season(conn, "p", 20.0, 16)
    e = explain.explain_player_value(conn, 2025, "p")
    step = e.steps[0]
    assert "last season's 20.0" in step.formula and "counts as 4 games" in step.why
    assert step.value == "15.0"  # (4 x 20 + 40) / (4 + 4)


def test_a_player_with_no_last_season_uses_only_this_one(conn):
    add_player(conn, "p", "WR", 10.0, games=3)
    assert "average of 3 real 2025 games" in explain.explain_player_value(conn, 2025, "p").steps[0].formula


def test_age_past_the_peak_shows_the_decay_and_at_the_peak_shows_none(conn):
    add_player(conn, "old", "RB", 12.0, age=29)
    add_player(conn, "prime", "RB", 12.0, age=24)
    old = explain.explain_player_value(conn, 2025, "old").steps[2]
    peak_end, decay = metrics.AGE_CURVES["RB"]
    assert old.formula == f"e^(-{decay} x (29 - {peak_end}))"
    assert old.value == f"{metrics.age_multiplier('RB', 29):.2f}"
    assert "at or below the RB peak" in explain.explain_player_value(conn, 2025, "prime").steps[2].formula


def test_qb_and_wr_get_their_reasons_for_the_position_weight(conn):
    add_player(conn, "qb", "QB", 20.0)
    add_player(conn, "wr", "WR", 20.0)
    assert "replaceable" in explain.explain_player_value(conn, 2025, "qb").steps[1].why
    assert "highest-signal" in explain.explain_player_value(conn, 2025, "wr").steps[1].why


def test_a_chain_that_does_not_reproduce_the_shown_number_is_never_returned(conn, monkeypatch):
    add_player(conn, "p", "WR", 14.0)
    real = valuation.player_valuations(conn, 2025)
    wrong = {"p": {**real["p"], "win_now_value": real["p"]["win_now_value"] + 1.0}}
    monkeypatch.setattr(valuation, "player_valuations", lambda conn, season: wrong)
    with pytest.raises(explain.ExplanationMismatch, match="couldn't reproduce the win-now value"):
        explain.explain_player_value(conn, 2025, "p")


def test_a_player_with_no_valuation_is_an_agent_error_that_says_why(conn):
    add_player(conn, "p", "WR", 14.0, games=0)
    with pytest.raises(AgentError, match="Player p has no valuation this season"):
        explain.explain_player_value(conn, 2025, "p")


def test_text_and_summary_carry_every_step_and_the_limits(conn):
    add_player(conn, "p", "WR", 14.0)
    e = explain.explain_player_value(conn, 2025, "p")
    text = e.text()
    for i, step in enumerate(e.steps, 1):
        assert f"{i}. {step.label}: {step.formula} = {step.value}" in text
    assert "What to keep in mind:" in text and "rounded for display" in text
    summary = e.summary()
    assert len(summary["steps"]) == len(e.steps) and summary["limits"] == e.limits


# -- the glossary ---------------------------------------------------------------------


@pytest.mark.parametrize("topic, term", [
    ("what does situation score mean", "situation score"),
    ("3yr mine", "three-year value"),
    ("how is win probability worked out", "win probability"),
    ("arbitrage", "arbitrage"),
    ("why does age matter for a running back", "age factor"),
    ("how much is a FAAB bid", "faab"),
    ("win-now", "win-now value"),
])
def test_glossary_finds_the_term_in_the_users_words(topic, term):
    assert explain.lookup_glossary(topic)[0] == term


def test_glossary_returns_none_for_an_unknown_topic():
    assert explain.lookup_glossary("the capital of France") is None
    assert explain.lookup_glossary("") is None


def test_the_age_definition_reads_the_live_curves():
    text = explain.lookup_glossary("age curve")[1]
    for position, (peak, _) in metrics.AGE_CURVES.items():
        assert f"{position} peaks at {peak}" in text


@pytest.mark.parametrize("topic, wanted", [
    ("how did you get his win-now value", True),
    ("walk me through the math", True),
    ("what does situation score mean", False),
    ("arbitrage", False),
])
def test_wants_math_tells_a_calculation_from_a_definition(topic, wanted):
    assert explain.wants_math(topic) is wanted


# -- recognizing an explain question --------------------------------------------------


@pytest.mark.parametrize("question", [
    "How did you get Trey Benson's win-now value?",
    "Walk me through how CeeDee Lamb's value is calculated",
    "Explain the math behind Rashee Rice's 3yr value",
    "Where does Puka Nacua's win-now number come from?",
    "How is the win probability calculated?",
    "How do you work out situation score?",
    "Why is his 3yr lower?",
    "Why is the win-now number so high?",
    "Break down that value for me",
    "What does situation score mean?",
    "What is arbitrage?",
    "what's a discount rate",
    "What does win-now value mean?",
    "Why does age matter for running backs?",
    "What is FAAB?",
    "define variance",
])
def test_explain_questions_are_recognized(question):
    assert explain.detect(question), question


@pytest.mark.parametrize("question", ["Why?", "why is that", "Explain that", "How did you get that?", "what does that mean"])
def test_a_bare_follow_up_needs_something_to_refer_to(question):
    assert explain.detect(question, has_recent=True)
    assert not explain.detect(question, has_recent=False)


@pytest.mark.parametrize("question", [
    "Why should I start CeeDee Lamb?",
    "Why start Lamb over Rice?",
    "How do I get Puka Nacua?",
    "How much should I bid on Trey Benson?",
    "What's a good FAAB bid for Justin Fields?",
    "What's the capital of France?",
    "What is the best lineup this week?",
    "Roughly what's Puka Nacua's trade value? Just ballpark it",
    "How's my team looking?",
    "Should I rebuild?",
])
def test_ordinary_questions_are_never_taken_for_explain_questions(question):
    assert not explain.detect(question, has_recent=True), question


def test_none_of_the_bake_off_questions_is_taken_for_an_explain_question():
    """The model routes these 39 questions to the six tools; Python must never take one from it."""
    from dynasty_agent import chat_eval

    taken = [c.question for c in chat_eval.CASES if explain.detect(c.question, has_recent=True)]
    assert taken == []


def test_find_player_reads_the_name_out_of_the_question_possessives_included(conn):
    add_player(conn, "a", "WR", 14.0)
    conn.execute("UPDATE players SET full_name = 'Trey Benson' WHERE player_id = 'a'")
    add_player(conn, "b", "RB", 12.0)
    conn.execute("UPDATE players SET full_name = 'Trey Benson Jr Smith' WHERE player_id = 'b'")
    assert explain.find_player(conn, 2025, "How did you get Trey Benson's win-now value?") == "Trey Benson"
    assert explain.find_player(conn, 2025, "trey benson jr smith?") == "Trey Benson Jr Smith"  # the longest name wins
    assert explain.find_player(conn, 2025, "what does arbitrage mean") is None
