"""Formatters are pure: a result dict in, text out, no database or network.
The live check is the CLI output diff; these pin the edge cases."""

from dynasty_agent import formatters


def test_waiver_targets_empty_and_depth_only():
    assert formatters.format_waiver_targets([]).endswith("none: nobody available has a win-now value this season.")
    bid = {"player": "A", "position": "QB", "target_win_now_value": 12.0, "lineup_gain": 0.0, "suggested_bid": 2, "note": None}
    assert "depth only" in formatters.format_waiver_targets([bid])


def test_lineup_with_no_opponent_says_so_instead_of_a_probability():
    result = {
        "week": 5, "unsupported_slots": [], "empty_slots": ["TE"], "opponent_note": "no matchup set for this week yet",
        "opponent_source": None, "recommended_lineup": [], "recommended_win_probability": None,
        "differs_from_points_max": False, "bench": [],
    }
    text = formatters.format_lineup(result, 2026, 2026)
    assert "week 5 of the 2026 season" in text
    assert "Win probability: n/a" in text
    assert "no eligible player on your roster for TE" in text


def test_pick_price_note_names_the_price_used():
    assert formatters.pick_price_note({"tier": "Mid", "price_label": "2027 1st (Mid)", "season": 2027, "base_season": 2027}) == "  priced as 2027 1st (Mid)"
    assert "tier unknown" in formatters.pick_price_note({"tier": None, "price_label": "2027 1st", "season": 2027, "base_season": 2027})
    assert formatters.pick_price_note({"tier": None, "price_label": "2028 1st", "season": 2028, "base_season": 2027}) == ""
