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


# -- phase B: lineups and trades --------------------------------------------------------


def lineup_result(prob=None, opponent=True):
    def p(name, mean, variance, **kw):
        return {"full_name": name, "mean": mean, "variance": variance, "injury_status": None, "vegas_multiplier": 1.0,
                "on_bye": False, "variance_estimated": False, **kw}

    starters = [p("Amon-Ra", 14.0, 30.0), p("Rice", 10.0, 25.0, injury_status="Questionable", vegas_multiplier=1.1),
                p("Rookie", 5.0, 20.0, variance_estimated=True)]
    mean_me, var_me = 29.0, 75.0
    result = {"week": 4, "recommended_lineup": starters, "opponent_mean": None, "opponent_variance": None,
              "opponent_note": "no matchup set for this week yet", "opponent_source": None, "opponent_roster_id": None,
              "recommended_win_probability": None, "differs_from_points_max": False, "swaps_from_points_max": [],
              "points_max_total": None}
    if opponent:
        result.update(opponent_mean=25.0, opponent_variance=60.0, opponent_source="their set lineup", opponent_roster_id=7,
                      opponent_note=None,
                      recommended_win_probability=prob if prob is not None else
                      metrics.matchup_win_probability(mean_me - 25.0, (var_me + 60.0) ** 0.5))
    return result


def test_the_win_probability_is_recomputed_from_the_gap_and_spread_shown():
    r = lineup_result()
    e = explain.explain_lineup(r)
    labels = [s.label for s in e.steps]
    assert labels == ["Your projected points, week 4", "Adjustments this week", "Your spread (variance)",
                      "Opponent (roster 7)", "The gap", "Win probability"]
    assert e.steps[0].value == "29.0" and e.steps[2].value == "75.0"
    assert e.steps[4].value == "+4.0" and "sqrt(75.0 + 60.0) = 11.6" in e.steps[4].why
    assert e.steps[-1].value == f"{r['recommended_win_probability']:.1%}"
    assert "Rice: Questionable x0.85, Vegas x1.10" in e.steps[1].formula
    assert "Rookie" in e.steps[2].why  # a borrowed variance is disclosed
    assert any("draft heuristic" in line and "independent" in line for line in e.limits)


def test_a_win_probability_that_does_not_reproduce_is_never_explained():
    with pytest.raises(explain.ExplanationMismatch, match="win probability"):
        explain.explain_lineup(lineup_result(prob=0.5))


def test_no_opponent_means_no_win_probability_and_says_why():
    e = explain.explain_lineup(lineup_result(opponent=False))
    assert e.steps[-1].label == "Opponent" and "no win probability" in e.steps[-1].why


def test_the_swap_from_the_points_lineup_is_explained():
    r = lineup_result()
    r.update(differs_from_points_max=True, points_max_total=30.5, swaps_from_points_max=[{"starts": "A", "slot": "FLEX", "over": "B"}])
    last = explain.explain_lineup(r).steps[-1]
    assert last.formula == "A over B" and last.value == "30.5 pts for the alternative"


@pytest.fixture
def trade(conn, monkeypatch):
    from tests.test_integration import trade_league

    trade_league(conn, monkeypatch)
    return lambda **kw: valuation.evaluate_trade(
        conn, 2025, 1, kw.get("send", []), kw.get("send_picks", []), kw.get("receive", []), kw.get("receive_picks", []), 0.2)


def test_a_trade_explanation_reproduces_every_total_and_discounts_a_later_pick(conn, trade):
    r = trade(send=["mine"], receive=["theirs"], receive_picks=[(2028, 1)])
    e = explain.explain_trade(conn, r)
    by_label = {s.label: s for s in e.steps}
    pick = by_label["Pick price: 2028 round 1"]
    assert "(1 - 20%)^1" in pick.formula and pick.value == f"{r['received']['picks'][0]['model_value']:.0f}"
    assert by_label["Net market value"].value == f"{r['market_value_delta']:+.0f}"
    assert by_label["Market value you receive"].value == f"{r['received']['market_value_total']:.0f}"
    assert by_label["Net win-now (players only)"].value == f"{r['win_now_delta']:+.1f}"
    assert by_label["Fit with your posture"].value == r["fit"]
    assert any("not dollars" in line for line in e.limits)


def test_a_trade_total_that_does_not_reproduce_is_never_explained(conn, trade):
    r = trade(send=["mine"], receive=["theirs"])
    r["received"]["market_value_total"] += 50
    with pytest.raises(explain.ExplanationMismatch, match="market value you receive"):
        explain.explain_trade(conn, r)


def test_an_unpriced_asset_is_named_as_counting_zero(conn, trade):
    conn.execute("DELETE FROM market_values WHERE player_id = 'fa'")
    r = trade(send=["mine"], receive=["fa"])
    assert r["received"]["unpriced"] == ["Player fa"]
    assert any("counts as 0" in line and "Player fa" in line for line in explain.explain_trade(conn, r).limits)


# -- phase C: bids, the verdict and picks -------------------------------------------------


def bid(**kw):
    base = {"player": "Trey Benson", "player_id": "tb", "remaining_budget": 100, "weeks_left": 5, "phase": "regular",
            "base_per_week_budget": 20.0, "lineup_gain": 3.0, "best_available_gain": 3.0, "value_multiplier": 3.0,
            "suggested_bid": 60, "budget_is_default": False, "note": None}
    return {**base, **kw}


def test_a_bid_is_rebuilt_from_pace_gain_and_multiplier():
    e = explain.explain_faab(bid())
    assert [s.value for s in e.steps] == ["$20.00 a week", "+3.0", "3.00", "$60"]
    assert e.subject == {"player_id": "tb", "full_name": "Trey Benson"}


def test_a_player_who_would_not_start_gets_the_token_bid_and_says_so():
    e = explain.explain_faab(bid(lineup_gain=0.0, value_multiplier=0.2, suggested_bid=4))
    assert e.steps[1].value == "none" and "wouldn't start" in e.steps[1].why and e.steps[-1].value == "$4"


def test_a_bid_that_does_not_reproduce_is_never_explained():
    with pytest.raises(explain.ExplanationMismatch, match="suggested bid"):
        explain.explain_faab(bid(suggested_bid=61))


def test_the_verdict_is_ranked_against_the_other_teams_and_reproduced(conn, monkeypatch):
    from tests.test_my_team import league_of

    league_of(conn, monkeypatch, {1: (30, 30), 2: (20, 20), 3: (10, 10)})
    e = explain.explain_verdict(conn, 2025, 1)
    assert e.steps[0].value == "100th percentile" and "you beat 2 of 2" in e.steps[0].why
    assert e.steps[2].value == "CONTEND"
    assert any("other 2 teams only" in line for line in e.limits)


def pick_row(**kw):
    return {"season": 2027, "round": 1, "projected_slot": "1.05", "tier": "Mid", "fantasycalc_price": 3034.0,
            "comparable_value": 2500.0, "comparable_players": ["A", "B"], "advice": "SELL", **kw}


def test_a_pick_call_is_the_price_over_what_the_slot_bought_against_the_bands():
    e = explain.explain_pick(pick_row())
    assert e.steps[-2].value == "1.21" and e.steps[-1].value == "SELL" and "1.15" in e.steps[-2].why


def test_a_pick_call_that_does_not_reproduce_is_never_explained():
    with pytest.raises(explain.ExplanationMismatch, match="pick call"):
        explain.explain_pick(pick_row(advice="BUY"))


@pytest.mark.parametrize("question", [
    "How did you size that bid?",
    "Why did you bench Rashee Rice?",
    "How did you decide I should rebuild?",
    "Why is the verdict rebuild?",
    "How is that win probability calculated?",
    "Why is the net market value so high?",
    "How did you price my 2027 1st?",
])
def test_phase_b_and_c_questions_are_recognized(question):
    assert explain.detect(question, has_recent=True), question


@pytest.mark.parametrize("n, text", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"),
                                     (21, "21st"), (36, "36th"), (82, "82nd"), (83, "83rd"), (100, "100th"), (101, "101st")])
def test_ordinals_read_like_a_person_wrote_them(n, text):
    assert explain.ordinal(n) == text


def test_the_bid_step_shows_the_unrounded_product_so_the_model_never_has_to_work_it_out():
    step = explain.explain_faab(bid(remaining_budget=100, weeks_left=11, base_per_week_budget=100 / 11, lineup_gain=0.0,
                                    value_multiplier=0.2, suggested_bid=2)).steps[-1]
    assert step.formula == "$9.09 x 0.20 = $1.82, rounded to whole dollars and capped at what is left" and step.value == "$2"


@pytest.mark.parametrize("question", ["Why is that a sell?", "Why is that a buy?", "Why is it a hold?"])
def test_why_is_that_a_sell_is_an_explain_question_after_an_answer(question):
    assert explain.detect(question, has_recent=True)
