from dynasty_agent.weekly import _starting_slot_counts


def test_starting_slot_counts_excludes_bench_taxi_ir():
    roster_positions = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX"] + ["BN"] * 10 + ["TAXI"] * 3 + ["IR"]
    assert _starting_slot_counts(roster_positions) == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1}


def test_starting_slot_counts_handles_a_differently_shaped_league():
    # Not this league's actual shape, just proving it isn't hardcoded to it.
    roster_positions = ["QB", "QB", "RB", "WR", "WR", "TE", "FLEX", "FLEX"] + ["BN"] * 6
    assert _starting_slot_counts(roster_positions) == {"QB": 2, "RB": 1, "WR": 2, "TE": 1, "FLEX": 2}
