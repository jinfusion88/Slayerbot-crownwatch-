"""Tests for rating_engine.py. No discord or database needed."""
import math

import pytest

from rating_engine import (
    DEFAULT_RATING,
    DEFAULT_RD,
    DEFAULT_VOL,
    to_glicko2,
    from_glicko2,
    rate,
    rate_period,
    build_pod_results,
    build_draw_results,
)


def test_glickman_worked_example():
    # Glickman's own worked example from the Glicko-2 paper. If this passes,
    # it's really Glicko-2.
    result = rate(1500, 200, 0.06, [
        (1400, 30, 1.0),
        (1550, 100, 0.0),
        (1700, 300, 0.0),
    ])
    assert result["rating"] == pytest.approx(1464.06, abs=0.01)
    assert result["rd"] == pytest.approx(151.52, abs=0.01)
    assert result["vol"] == pytest.approx(0.05999, abs=0.00001)


def test_scale_round_trip():
    rating, rd = from_glicko2(*to_glicko2(1500, 350))
    assert rating == pytest.approx(1500, abs=1e-9)
    assert rd == pytest.approx(350, abs=1e-9)


def test_default_players_winner_gains_loser_loses_symmetric():
    winner = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [(DEFAULT_RATING, DEFAULT_RD, 1.0)])
    loser = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [(DEFAULT_RATING, DEFAULT_RD, 0.0)])
    winner_delta = winner["rating"] - DEFAULT_RATING
    loser_delta = loser["rating"] - DEFAULT_RATING
    assert winner_delta > 0
    assert loser_delta < 0
    assert winner_delta == pytest.approx(-loser_delta, abs=1e-9)


def test_rated_match_decreases_both_rds():
    winner = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [(DEFAULT_RATING, DEFAULT_RD, 1.0)])
    loser = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [(DEFAULT_RATING, DEFAULT_RD, 0.0)])
    assert winner["rd"] < DEFAULT_RD
    assert loser["rd"] < DEFAULT_RD


def test_rate_no_opponents_leaves_rating_and_vol_unchanged():
    result = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [])
    assert result["rating"] == pytest.approx(DEFAULT_RATING, abs=1e-9)
    assert result["vol"] == pytest.approx(DEFAULT_VOL, abs=1e-9)
    assert result["rd"] >= DEFAULT_RD - 1e-9
    assert result["rd"] <= DEFAULT_RD


def test_rate_no_opponents_lower_rd_grows_but_never_exceeds_default():
    result = rate(1500, 60, 0.06, [])
    assert result["rd"] > 60
    assert result["rd"] <= DEFAULT_RD


def test_draw_between_identical_defaults_leaves_ratings_unchanged():
    result_a = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [(DEFAULT_RATING, DEFAULT_RD, 0.5)])
    result_b = rate(DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL, [(DEFAULT_RATING, DEFAULT_RD, 0.5)])
    assert result_a["rating"] == pytest.approx(DEFAULT_RATING, abs=1e-6)
    assert result_b["rating"] == pytest.approx(DEFAULT_RATING, abs=1e-6)
    assert result_a["rd"] < DEFAULT_RD
    assert result_b["rd"] < DEFAULT_RD


def test_build_pod_results_three_losers():
    results = build_pod_results(1, [2, 3, 4])
    assert len(results) == 6
    assert set(results) == {
        (1, 2, 1.0), (1, 3, 1.0), (1, 4, 1.0),
        (2, 3, 0.5), (2, 4, 0.5), (3, 4, 0.5),
    }


def test_build_pod_results_drops_self_match():
    # Winner is also in the loser list, so it gets dropped.
    results = build_pod_results(1, [1, 2, 3])
    assert len(results) == 3
    assert set(results) == {
        (1, 2, 1.0), (1, 3, 1.0),
        (2, 3, 0.5),
    }


def test_build_draw_results_four_way():
    results = build_draw_results([1, 2, 3, 4])
    assert len(results) == 6
    assert set(results) == {
        (1, 2, 0.5), (1, 3, 0.5), (1, 4, 0.5),
        (2, 3, 0.5), (2, 4, 0.5), (3, 4, 0.5),
    }
    assert all(score == 0.5 for _, _, score in results)


def test_rate_period_pod_winner_rises_losers_fall():
    players = {
        1: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        2: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        3: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        4: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
    }
    results = build_pod_results(1, [2, 3, 4])
    updated = rate_period(players, results)

    assert set(updated.keys()) == {1, 2, 3, 4}
    assert updated[1]["rating"] > DEFAULT_RATING
    assert updated[2]["rating"] < DEFAULT_RATING
    assert updated[3]["rating"] < DEFAULT_RATING
    assert updated[4]["rating"] < DEFAULT_RATING


def test_rate_period_simultaneity_is_order_independent():
    # Shuffling the results shouldn't change anything since everyone is rated
    # against the starting ratings.
    players = {
        1: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        2: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        3: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        4: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
    }
    results = build_pod_results(1, [2, 3, 4])
    forward = rate_period(players, results)
    backward = rate_period(players, list(reversed(results)))

    for user_id in players:
        assert forward[user_id]["rating"] == pytest.approx(backward[user_id]["rating"], abs=1e-9)
        assert forward[user_id]["rd"] == pytest.approx(backward[user_id]["rd"], abs=1e-9)
        assert forward[user_id]["vol"] == pytest.approx(backward[user_id]["vol"], abs=1e-9)


def test_rate_rd_at_or_below_zero_clamps_to_default_rd():
    # rd of 0 should get reset to the default instead of crashing.
    result = rate(1500, 0, 0.06, [(1500, 350, 1.0)])
    assert math.isfinite(result["rating"])
    assert math.isfinite(result["rd"])
    assert math.isfinite(result["vol"])
    assert result["rd"] > 0


def test_rate_vol_at_or_below_zero_clamps_to_default_vol():
    # vol of 0 would make math.log fail, so it should reset to the default.
    result = rate(1500, 350, 0, [(1500, 350, 1.0)])
    assert math.isfinite(result["rating"])
    assert math.isfinite(result["rd"])
    assert math.isfinite(result["vol"])
    assert result["vol"] > 0


def test_rate_extreme_rating_gap_falls_back_to_did_not_compete():
    # A huge rating gap should fall back to the did not compete update.
    result = rate(100000, 1, 0.06, [(0, 1, 1.0)])
    assert result["rating"] == pytest.approx(100000, abs=1e-9)
    assert result["vol"] == pytest.approx(0.06, abs=1e-9)
    assert result["rd"] > 1
    assert result["rd"] <= DEFAULT_RD


def test_rate_period_player_with_no_result_unchanged():
    players = {
        1: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        2: {"rating": DEFAULT_RATING, "rd": DEFAULT_RD, "vol": DEFAULT_VOL},
        3: {"rating": 1600.0, "rd": 80.0, "vol": 0.06},  # not in this pod
    }
    results = [(1, 2, 1.0)]
    updated = rate_period(players, results)
    assert updated[3]["rating"] == pytest.approx(1600.0, abs=1e-9)
