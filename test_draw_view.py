"""Tests for the pure helpers the draw and claim views in main.py use.

These live in db_manager.py because main.py imports discord.py, which means the
tests can't import main.py.
"""
from db_manager import (
    build_contender_string,
    pod_rating_losers,
    classify_pod_claim,
    classify_pod_draw,
    build_draw_pod_ids,
    promote_draw_to_contested,
    format_rating_deltas,
)


def test_build_contender_string_includes_submitter():
    assert build_contender_string([222, 333], 111) == "222,333,111"


def test_build_contender_string_dedupes_submitter_already_selected():
    # Submitter picked themselves too, shouldn't show up twice.
    assert build_contender_string([111, 222], 111) == "111,222"


def test_build_contender_string_dedupes_repeated_selection():
    # UserSelect shouldn't give duplicates, but just in case.
    assert build_contender_string([222, 222, 333], 111) == "222,333,111"


def test_build_contender_string_preserves_selection_order():
    assert build_contender_string([333, 222], 111) == "333,222,111"


def test_build_contender_string_single_selection():
    assert build_contender_string([222], 111) == "222,111"


def test_pod_rating_losers_basic():
    assert pod_rating_losers([1, 2, 3, 4], 1) == [2, 3, 4]


def test_pod_rating_losers_winner_outside_pod():
    # Winner isn't in the pod, so all four are losers.
    assert pod_rating_losers([1, 2, 3, 4], 9) == [1, 2, 3, 4]


def test_pod_rating_losers_deduplicates():
    # Duplicate 2 only shows up once.
    assert pod_rating_losers([2, 2, 3], 1) == [2, 3]


def test_pod_rating_losers_drops_falsy():
    # None and 0 get dropped.
    assert pod_rating_losers([2, None, 0, 3], 1) == [2, 3]


def test_pod_rating_losers_none_pod_ids():
    assert pod_rating_losers(None, 1) == []


def test_pod_rating_losers_empty_pod_ids():
    assert pod_rating_losers([], 1) == []


def test_pod_rating_losers_single_member():
    assert pod_rating_losers([1], 1) == []


def test_classify_pod_claim_vacant_none_champion():
    assert classify_pod_claim(None, [1, 2, 3, 4], 1) == "vacant"


def test_classify_pod_claim_vacant_zero_champion():
    assert classify_pod_claim(0, [1, 2, 3, 4], 1) == "vacant"


def test_classify_pod_claim_defense_champion_not_in_pod():
    # Champion is the one logging it, so it's a defense even if they aren't in the pod list.
    assert classify_pod_claim(5, [1, 2, 3, 4], 5) == "defense"


def test_classify_pod_claim_defense_champion_in_pod():
    assert classify_pod_claim(1, [1, 2, 3, 4], 1) == "defense"


def test_classify_pod_claim_direct():
    assert classify_pod_claim(4, [1, 2, 3, 4], 1) == "direct"


def test_classify_pod_claim_usurp():
    assert classify_pod_claim(9, [1, 2, 3, 4], 1) == "usurp"


def test_classify_pod_claim_usurp_none_pod():
    assert classify_pod_claim(9, None, 1) == "usurp"


def test_classify_pod_claim_usurp_empty_pod():
    assert classify_pod_claim(9, [], 1) == "usurp"


def test_classify_pod_draw_vacant_none_champion():
    assert classify_pod_draw(None, [1, 2, 3, 4]) == "vacant"


def test_classify_pod_draw_vacant_zero_champion():
    assert classify_pod_draw(0, [1, 2, 3, 4]) == "vacant"


def test_classify_pod_draw_contested():
    assert classify_pod_draw(4, [1, 2, 3, 4]) == "contested"


def test_classify_pod_draw_absent():
    assert classify_pod_draw(9, [1, 2, 3, 4]) == "absent"


def test_classify_pod_draw_absent_none_pod():
    assert classify_pod_draw(9, None) == "absent"


def test_classify_pod_draw_absent_empty_pod():
    assert classify_pod_draw(9, []) == "absent"


def test_build_draw_pod_ids_appends_champion_by_default():
    # By default the champion gets added so they can play in sudden death.
    assert build_draw_pod_ids([2, 3, 4], 1) == [2, 3, 4, 1]


def test_build_draw_pod_ids_contested_explicit():
    assert build_draw_pod_ids([2, 3, 4], 1, True) == [2, 3, 4, 1]


def test_build_draw_pod_ids_absent_champion_excluded():
    # An absent champion should never be added, they'd get rated for a game they didn't play.
    assert build_draw_pod_ids([1, 2, 3, 4], 9, False) == [1, 2, 3, 4]


def test_build_draw_pod_ids_no_holder():
    assert build_draw_pod_ids([2, 3, 4], None) == [2, 3, 4]
    assert build_draw_pod_ids([2, 3, 4], 0) == [2, 3, 4]


def test_build_draw_pod_ids_does_not_dedupe_contested():
    assert build_draw_pod_ids([1, 2, 3], 1) == [1, 2, 3, 1]


def test_build_draw_pod_ids_copies_input():
    selected = [2, 3, 4]
    assert build_draw_pod_ids(selected, 1) == [2, 3, 4, 1]
    assert selected == [2, 3, 4]
# format_rating_deltas: the rating change line added to match announcements.

def _pod(winner_delta=14.0, loser_deltas=(-4.0, -5.0, -5.0)):
    """Fake 4 player result shaped like record_match_ratings output."""
    participants = [{"user_id": 1, "result": "win", "rating_before": 1500.0,
                     "rating_after": 1500.0 + winner_delta, "delta": winner_delta}]
    for offset, delta in enumerate(loser_deltas, start=2):
        participants.append({"user_id": offset, "result": "loss",
                             "rating_before": 1500.0,
                             "rating_after": 1500.0 + delta, "delta": delta})
    return participants


def test_format_rating_deltas_standard_pod():
    assert format_rating_deltas(_pod()) == (
        "🏆 Winner: <@1> (+14) | "
        "💀 Defeated: <@2> (-4), <@3> (-5), <@4> (-5)"
    )


def test_format_rating_deltas_rounds_to_integers():
    # 13.6 rounds to +14, -4.4 rounds to -4.
    assert format_rating_deltas(_pod(13.6, (-4.4, -5.0, -5.0))) == (
        "🏆 Winner: <@1> (+14) | "
        "💀 Defeated: <@2> (-4), <@3> (-5), <@4> (-5)"
    )


def test_format_rating_deltas_tiny_negative_renders_plus_zero():
    # Should never show "-0".
    line = format_rating_deltas(_pod(14.0, (-0.4, -5.0, -5.0)))
    assert "<@2> (+0)" in line
    assert "-0)" not in line


def test_format_rating_deltas_exact_zero_renders_plus_zero():
    assert "<@2> (+0)" in format_rating_deltas(_pod(14.0, (0.0, -5.0, -5.0)))


def test_format_rating_deltas_draw_lists_every_player():
    draw = [
        {"user_id": 1, "result": "draw", "delta": 2.0},
        {"user_id": 2, "result": "draw", "delta": -1.0},
        {"user_id": 3, "result": "draw", "delta": -1.0},
        {"user_id": 4, "result": "draw", "delta": -0.4},
    ]
    assert format_rating_deltas(draw, is_draw=True) == (
        "🤝 Draw: <@1> (+2), <@2> (-1), <@3> (-1), <@4> (+0)"
    )


def test_format_rating_deltas_empty_returns_blank():
    # "" means don't add anything.
    assert format_rating_deltas([]) == ""
    assert format_rating_deltas(None) == ""
    assert format_rating_deltas([], is_draw=True) == ""
    assert format_rating_deltas(None, is_draw=True) == ""


def test_format_rating_deltas_no_winner_renders_defeated_half_only():
    # Don't make up a winner if there isn't one.
    losers = [entry for entry in _pod() if entry["result"] != "win"]
    assert format_rating_deltas(losers) == (
        "💀 Defeated: <@2> (-4), <@3> (-5), <@4> (-5)"
    )


def test_format_rating_deltas_winner_only_renders_winner_half_only():
    winner = [entry for entry in _pod() if entry["result"] == "win"]
    assert format_rating_deltas(winner) == "🏆 Winner: <@1> (+14)"


def test_format_rating_deltas_preserves_loser_order():
    line = format_rating_deltas(_pod(14.0, (-9.0, -1.0, -5.0)))
    assert line.endswith("<@2> (-9), <@3> (-1), <@4> (-5)")


def test_format_rating_deltas_skips_unrenderable_entries():
    # A broken entry gets skipped instead of crashing.
    participants = _pod() + [{"result": "loss"}, {"user_id": 9, "delta": None}]
    assert format_rating_deltas(participants) == (
        "🏆 Winner: <@1> (+14) | "
        "💀 Defeated: <@2> (-4), <@3> (-5), <@4> (-5)"
    )


def test_format_rating_deltas_all_entries_unrenderable_returns_blank():
    assert format_rating_deltas([{"result": "win"}, {"result": "loss"}]) == ""
    assert format_rating_deltas([{"result": "draw"}], is_draw=True) == ""


# These make sure format_rating_deltas never raises, since it runs after
# the title change already saved. Every bad input should just return "".

def test_format_rating_deltas_infinite_delta_returns_blank():
    # round(float('inf')) raises OverflowError, which my original except didn't catch.
    inf_only = [{"user_id": 1, "result": "win", "delta": float("inf")}]
    assert format_rating_deltas(inf_only) == ""
    assert format_rating_deltas(inf_only, is_draw=True) == ""
    assert format_rating_deltas(
        [{"user_id": 1, "result": "win", "delta": float("-inf")}]) == ""
    # NaN was already handled, this just covers it too.
    assert format_rating_deltas(
        [{"user_id": 1, "result": "win", "delta": float("nan")}]) == ""


def test_format_rating_deltas_infinite_delta_drops_only_that_entry():
    # The other players should still show up, just not the broken one.
    participants = _pod() + [{"user_id": 9, "result": "loss",
                              "delta": float("inf")}]
    line = format_rating_deltas(participants)
    assert line == (
        "🏆 Winner: <@1> (+14) | "
        "💀 Defeated: <@2> (-4), <@3> (-5), <@4> (-5)"
    )
    assert "<@9>" not in line


def test_format_rating_deltas_truthy_non_list_returns_blank():
    # A non list value would crash on the for loop itself.
    for bogus in (7, 3.5, object(), True):
        assert format_rating_deltas(bogus) == ""
        assert format_rating_deltas(bogus, is_draw=True) == ""


def test_format_rating_deltas_iterable_that_raises_returns_blank():
    class Exploding:
        def __bool__(self):
            return True

        def __iter__(self):
            raise RuntimeError("boom")

    assert format_rating_deltas(Exploding()) == ""
    assert format_rating_deltas(Exploding(), is_draw=True) == ""


def test_format_rating_deltas_never_returns_none_for_bad_input():
    # Always a string, never None, and no extra newline.
    for bogus in (None, [], 0, 7, object(), [{"result": "win"}],
                  [{"user_id": 1, "delta": float("inf")}]):
        for is_draw in (False, True):
            out = format_rating_deltas(bogus, is_draw=is_draw)
            assert isinstance(out, str)
            if out:
                assert not out.startswith(chr(10)) and not out.endswith(chr(10))


# promote_draw_to_contested: can go from absent to contested if the champion
# gets added back in, but never the other way.


def test_promote_absent_to_contested_when_champion_edited_in():
    # Champion was added back in, so it should count as contested.
    assert promote_draw_to_contested(False, 9, [1, 2, 9]) is True


def test_promote_absent_stays_absent_when_champion_not_edited_in():
    assert promote_draw_to_contested(False, 9, [1, 2, 3, 4]) is False


def test_promote_absent_stays_absent_on_empty_pod():
    assert promote_draw_to_contested(False, 9, []) is False
    assert promote_draw_to_contested(False, 9, None) is False


def test_promote_absent_falsy_holder_never_matches():
    # A falsy holder shouldn't match a falsy id in the list.
    assert promote_draw_to_contested(False, None, [0, None]) is False
    assert promote_draw_to_contested(False, 0, [0]) is False


def test_promote_never_demotes_contested_minus_champion_pod():
    # Important one. The contested path leaves the champion out of selected_ids on
    # purpose, so just re-running classify_pod_draw would say "absent" and skip the damage.
    assert promote_draw_to_contested(True, 9, [1, 2, 3]) is True
    assert classify_pod_draw(9, [1, 2, 3]) == "absent"  # what re-classifying would give


def test_promote_never_demotes_on_empty_or_missing_pod():
    # /slayed's Draw button starts with nothing selected, it shouldn't get demoted.
    assert promote_draw_to_contested(True, 9, []) is True
    assert promote_draw_to_contested(True, 9, None) is True
    assert promote_draw_to_contested(True, None, [1, 2]) is True


def test_promote_result_composes_with_build_draw_pod_ids():
    # Whole flow: the promoted flag goes into build_draw_pod_ids.
    selected = [1, 2, 9]
    promoted = promote_draw_to_contested(False, 9, selected)
    assert promoted is True
    # The champion showing up twice is fine, it gets deduped later.
    assert build_draw_pod_ids(selected, 9, promoted) == [1, 2, 9, 9]


def test_promote_unpromoted_absent_pod_excludes_champion():
    selected = [1, 2, 3, 4]
    promoted = promote_draw_to_contested(False, 9, selected)
    assert promoted is False
    assert build_draw_pod_ids(selected, 9, promoted) == [1, 2, 3, 4]
