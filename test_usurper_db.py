"""Tests for the database layer (usurper system, telemetry, undo, ratings, stats).

These use a real temporary SQLite file. main.py isn't imported since it needs discord.py.
"""
import datetime
import json

import pytest

import db_manager
from db_manager import (
    DBManager,
    USURP_WARNING_WINS,
    USURP_WINS_TO_OVERTHROW,
    DEFAULT_STARTING_LIFE,
    BOUNTY_STARTING_LIFE,
    DRAW_DAMAGE,
    MATCH_ID_ALPHABET,
    MATCH_ID_LENGTH,
    generate_match_id,
    upkeep_blocks_elapsed,
)
from rating_engine import DEFAULT_RATING, DEFAULT_RD, DEFAULT_VOL


@pytest.fixture
def dbm(tmp_path):
    manager = DBManager(db_path=str(tmp_path / "test_slayer.db"))
    manager.setup()
    yield manager
    if manager._conn is not None:
        manager._conn.close()


@pytest.fixture
def title_with_champion(dbm):
    """Makes a title held by user 111. Returns (title_id, champion_id)."""
    assert dbm.create_title("KingSlayer", role_id=999)
    title_id = dbm.get_title_by_name("KingSlayer")['id']
    dbm.grant_title(title_id, 111)
    return title_id, 111


def test_constants():
    assert USURP_WARNING_WINS == 3
    assert USURP_WINS_TO_OVERTHROW == 4
    assert DEFAULT_STARTING_LIFE == 50
    assert BOUNTY_STARTING_LIFE == 60
    assert DRAW_DAMAGE == 20


def test_grant_title_non_bounty_starts_at_default_life(dbm):
    assert dbm.create_title("NonBountyTitle", role_id=997)
    title_id = dbm.get_title_by_name("NonBountyTitle")['id']
    dbm.grant_title(title_id, 111)
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['current_life'] == DEFAULT_STARTING_LIFE


def test_grant_title_bounty_starts_at_bounty_life_and_clears_flag(dbm):
    assert dbm.create_title("BountyTitle", role_id=996)
    title_id = dbm.get_title_by_name("BountyTitle")['id']
    dbm.set_bounty(title_id, True)
    dbm.grant_title(title_id, 111)
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['current_life'] == BOUNTY_STARTING_LIFE
    title_row = dbm.get_title_by_id(title_id)
    assert title_row['bounty_active'] == 0


def test_apply_combat_damage_reduces_by_draw_damage(dbm, title_with_champion):
    title_id, _ = title_with_champion
    starting_life = dbm.get_active_reign_for_title(title_id)['current_life']
    new_life = dbm.apply_combat_damage(title_id)
    assert new_life == starting_life - DRAW_DAMAGE
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['current_life'] == starting_life - DRAW_DAMAGE


def test_lifelink_reset_restores_default_life(dbm, title_with_champion):
    title_id, _ = title_with_champion
    dbm.apply_combat_damage(title_id)
    dbm.lifelink_reset(title_id)
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['current_life'] == DEFAULT_STARTING_LIFE


def test_add_contender_win_starts_at_one_and_increments(dbm, title_with_champion):
    title_id, _ = title_with_champion
    assert dbm.add_contender_win(title_id, 222) == 1
    assert dbm.add_contender_win(title_id, 222) == 2
    assert dbm.add_contender_win(title_id, 222) == 3
    # A different challenger has their own count
    assert dbm.add_contender_win(title_id, 333) == 1


def test_get_top_contender_none_when_empty(dbm, title_with_champion):
    title_id, _ = title_with_champion
    assert dbm.get_top_contender(title_id) is None


def test_get_top_contender_returns_highest(dbm, title_with_champion):
    title_id, _ = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 333)
    dbm.add_contender_win(title_id, 333)
    top = dbm.get_top_contender(title_id)
    assert top['discord_user_id'] == 333
    assert top['wins'] == 2


def test_wipe_contenders(dbm, title_with_champion):
    title_id, _ = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 333)
    assert dbm.wipe_contenders(title_id) == 2
    assert dbm.get_top_contender(title_id) is None
    assert dbm.wipe_contenders(title_id) == 0
    # After a wipe the count starts over at 1
    assert dbm.add_contender_win(title_id, 222) == 1


def test_execute_overthrow_archives_crowns_and_wipes(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 333)

    old_reign = dbm.execute_overthrow(title_id, 222)

    assert old_reign is not None
    assert old_reign['discord_user_id'] == champion_id

    history = dbm.get_title_history(title_id)
    assert any(h['discord_user_id'] == champion_id for h in history)

    # New champion starts at bounty life
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['discord_user_id'] == 222
    assert reign['current_life'] == BOUNTY_STARTING_LIFE

    # All contender wins on the title are wiped, including the usurper's
    assert dbm.get_top_contender(title_id) is None


def test_execute_overthrow_grants_bounty_life_regardless_of_bounty_flag(dbm, title_with_champion):
    """An overthrow always gives bounty life, even if the bounty flag wasn't set."""
    title_id, _ = title_with_champion
    dbm.set_bounty(title_id, True)
    dbm.execute_overthrow(title_id, 222)
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['current_life'] == BOUNTY_STARTING_LIFE
    title_row = dbm.get_title_by_id(title_id)
    assert title_row['bounty_active'] == 0


def test_execute_overthrow_on_vacant_title_returns_none(dbm):
    assert dbm.create_title("VacantCrown", role_id=998)
    title_id = dbm.get_title_by_name("VacantCrown")['id']
    old_reign = dbm.execute_overthrow(title_id, 222)
    assert old_reign is None
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['discord_user_id'] == 222
    assert reign['current_life'] == BOUNTY_STARTING_LIFE


def test_get_current_holders_includes_title_id(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    holders = dbm.get_current_holders()
    row = next(h for h in holders if h['discord_user_id'] == champion_id)
    assert row['title_id'] == title_id


def test_get_titles_defaults_to_25_but_honors_an_explicit_limit(dbm):
    # 30 titles so there are more than the default limit of 25.
    for n in range(30):
        assert dbm.create_title(f"Crown{n:02d}", role_id=2000 + n)

    # Has to stay 25, Discord won't accept more than 25 autocomplete choices.
    assert len(dbm.get_titles()) == 25
    assert len(dbm.get_titles("Crown")) == 25

    # The claim picker asks for 26 to check if titles got cut off.
    assert len(dbm.get_titles(limit=26)) == 26
    assert len(dbm.get_titles("Crown", limit=30)) == 30

    assert len(dbm.get_titles(limit=3)) == 3
    assert len(dbm.get_titles("Crown", limit=3)) == 3


def test_get_titles_limit_probe_detects_no_truncation_below_the_cap(dbm):
    # With exactly 25 titles, asking for 26 gives 25, so no truncation warning.
    for n in range(25):
        assert dbm.create_title(f"Crown{n:02d}", role_id=2100 + n)
    assert len(dbm.get_titles(limit=26)) == 25


def test_delete_title_removes_contenders(dbm, title_with_champion):
    title_id, _ = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.delete_title(title_id)
    assert dbm.get_top_contender(title_id) is None


def test_vacate_title_removes_contenders(dbm, title_with_champion):
    title_id, _ = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.vacate_title(title_id)
    assert dbm.get_top_contender(title_id) is None


def test_reset_all_active_reigns_removes_contenders(dbm, title_with_champion):
    title_id, _ = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.reset_all_active_reigns()
    assert dbm.get_top_contender(title_id) is None


def test_get_ironman_standings_orders_and_combines_reign_and_history(dbm):
    # User 203: claims=1, defenses=5, total=6
    assert dbm.create_title("Crown3", role_id=1003)
    title3_id = dbm.get_title_by_name("Crown3")['id']
    dbm.grant_title(title3_id, 203)
    for _ in range(5):
        dbm.log_defense(title3_id)

    # User 201 claims title1 and defends twice, then claims and loses title2.
    # claims=2, defenses=2, total=4
    assert dbm.create_title("Crown1", role_id=1001)
    title1_id = dbm.get_title_by_name("Crown1")['id']
    dbm.grant_title(title1_id, 201)
    dbm.log_defense(title1_id)
    dbm.log_defense(title1_id)

    assert dbm.create_title("Crown2", role_id=1002)
    title2_id = dbm.get_title_by_name("Crown2")['id']
    dbm.grant_title(title2_id, 201)
    # 202 takes title2 from 201
    dbm.grant_title(title2_id, 202)
    dbm.log_defense(title2_id)  # 202: claims=1, defenses=1, total=2

    standings = dbm.get_ironman_standings(limit=2)

    assert len(standings) <= 2
    assert [row['discord_user_id'] for row in standings] == [203, 201]

    top = standings[0]
    assert top['discord_user_id'] == 203
    assert top['claims_this_month'] == 1
    assert top['defenses_this_month'] == 5
    assert top['total_score'] == 6

    combined = standings[1]
    assert combined['discord_user_id'] == 201
    assert combined['claims_this_month'] == 2
    assert combined['defenses_this_month'] == 2
    assert combined['total_score'] == 4

    # 202 gets cut off by limit=2
    assert 202 not in [row['discord_user_id'] for row in standings]


def test_get_ironman_standings_breaks_score_ties_by_user_id(dbm):
    # Two users tied, the user id tiebreaker keeps the order the same every time.
    for user_id, name, role_id in ((302, "TieCrownB", 1102), (301, "TieCrownA", 1101)):
        assert dbm.create_title(name, role_id=role_id)
        dbm.grant_title(dbm.get_title_by_name(name)['id'], user_id)

    standings = dbm.get_ironman_standings(limit=10)
    assert [row['discord_user_id'] for row in standings] == [301, 302]
    assert {row['total_score'] for row in standings} == {1}


def test_get_ironman_standings_excludes_reign_before_current_utc_month(dbm):
    assert dbm.create_title("OldCrown", role_id=1004)
    title_id = dbm.get_title_by_name("OldCrown")['id']
    dbm.grant_title(title_id, 204)
    dbm.log_defense(title_id)

    # Move the reign back to before this month.
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    start_of_month = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    before_this_month = (start_of_month - datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    with dbm.get_connection() as conn:
        conn.execute(
            'UPDATE active_reigns SET timestamp_acquired = ? WHERE title_id = ?',
            (before_this_month, title_id),
        )
        conn.commit()

    standings = dbm.get_ironman_standings(limit=10)
    assert 204 not in [row['discord_user_id'] for row in standings]


def test_get_current_holders_exposes_bounty_and_sudden_death_fields(dbm, title_with_champion):
    held_title_id, champion_id = title_with_champion

    assert dbm.create_title("VacantCrown2", role_id=1005)

    holders = dbm.get_current_holders()

    held_row = next(h for h in holders if h['discord_user_id'] == champion_id)
    assert held_row['bounty_active'] == 0
    assert held_row['sudden_death_contenders'] is None

    # bounty and sudden death come from the titles table, so they show up even when vacant.
    vacant_row = next(h for h in holders if h['title_name'] == "VacantCrown2")
    assert vacant_row['discord_user_id'] is None
    assert vacant_row['bounty_active'] == 0
    assert vacant_row['sudden_death_contenders'] is None


def test_get_current_holders_round_trips_vacated_bounty_and_contenders(dbm, title_with_champion):
    """What vacate_title writes after a draw should be what /whoslayer reads back
    from get_current_holders. I can't test /whoslayer directly, so this checks it here.
    """
    held_title_id, _ = title_with_champion

    dbm.vacate_title(held_title_id, is_bounty=True, contenders_list="111,222")

    row = next(h for h in dbm.get_current_holders() if h['title_name'] == "KingSlayer")
    assert row['discord_user_id'] is None
    assert row['bounty_active'] == 1
    assert row['sudden_death_contenders'] == "111,222"


# --- Match log ---

def test_generate_match_id_length_and_alphabet():
    match_id = generate_match_id()
    assert len(match_id) == MATCH_ID_LENGTH
    assert all(char in MATCH_ID_ALPHABET for char in match_id)


def test_generate_match_id_is_not_constant():
    ids = {generate_match_id() for _ in range(200)}
    assert len(ids) > 1


def test_log_match_and_get_match_round_trip(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("defense", title_id, champion_id)

    assert match_id is not None
    assert len(match_id) == MATCH_ID_LENGTH

    row = dbm.get_match(match_id)
    assert row is not None
    assert row['match_type'] == "defense"
    assert row['title_id'] == title_id
    assert row['actor_id'] == champion_id
    assert row['target_id'] is None
    assert row['undone'] == 0


def test_get_match_is_case_and_whitespace_insensitive(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("defense", title_id, champion_id)

    row = dbm.get_match(f"  {match_id.lower()} ")
    assert row is not None
    assert row['match_id'] == match_id


def test_log_match_snapshot_contains_all_keys_and_active_reign_details(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("defense", title_id, champion_id)

    row = dbm.get_match(match_id)
    snapshot = json.loads(row['snapshot'])

    assert set(snapshot.keys()) == {
        "active_reign", "title", "contenders", "rivalry", "max_historical_id",
    }
    assert snapshot["active_reign"]["discord_user_id"] == champion_id
    assert snapshot["active_reign"]["current_life"] == DEFAULT_STARTING_LIFE


def test_log_match_on_vacant_title_stores_none_active_reign(dbm):
    assert dbm.create_title("VacantForMatchLog", role_id=1201)
    title_id = dbm.get_title_by_name("VacantForMatchLog")['id']

    match_id = dbm.log_match("claim", title_id, 555)
    row = dbm.get_match(match_id)
    snapshot = json.loads(row['snapshot'])
    assert snapshot["active_reign"] is None


def test_log_match_captures_existing_contenders(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 333)
    dbm.add_contender_win(title_id, 333)

    match_id = dbm.log_match("defense", title_id, champion_id)
    row = dbm.get_match(match_id)
    snapshot = json.loads(row['snapshot'])

    contenders_by_id = {c["discord_user_id"]: c["wins"] for c in snapshot["contenders"]}
    assert contenders_by_id == {222: 1, 333: 2}


def test_log_match_invalid_type_raises_value_error(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    with pytest.raises(ValueError):
        dbm.log_match("bogus", title_id, champion_id)


def test_get_match_returns_none_for_unknown_id(dbm):
    assert dbm.get_match("ZZZZZ") is None


# --- Undo ---

def _contender_wins(dbm, title_id):
    """{user_id: wins} for a title from the contenders table."""
    with dbm.get_connection() as conn:
        rows = conn.execute(
            'SELECT discord_user_id, wins FROM contenders WHERE title_id = ?', (title_id,)
        ).fetchall()
    return {row['discord_user_id']: row['wins'] for row in rows}


def test_undo_claim_restores_previous_champion(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    # Grant again with a decklist so there's something to restore. This also
    # leaves one older history row that should survive the undo.
    dbm.grant_title(title_id, champion_id, decklist="https://moxfield.example/a")
    dbm.log_defense(title_id)
    dbm.log_defense(title_id)

    reign_before = dict(dbm.get_active_reign_for_title(title_id))
    history_before = len(dbm.get_title_history(title_id))
    assert history_before == 1

    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)
    dbm.grant_title(title_id, 222)
    dbm.wipe_contenders(title_id)
    dbm.record_rivalry_win(222, champion_id)

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['match_type'] == "claim"
    assert result['title_id'] == title_id
    assert result['title_name'] == "KingSlayer"
    assert result['actor_id'] == 222
    assert result['target_id'] == champion_id
    assert result['reverted_holder_id'] == 222
    assert result['reverted_life'] == DEFAULT_STARTING_LIFE
    assert result['restored_holder_id'] == champion_id
    assert result['restored_life'] == reign_before['current_life']
    assert result['restored_defenses'] == reign_before['defenses_count']

    reign_after = dict(dbm.get_active_reign_for_title(title_id))
    assert reign_after['id'] == reign_before['id']
    assert reign_after['discord_user_id'] == champion_id
    assert reign_after['current_life'] == reign_before['current_life']
    assert reign_after['defenses_count'] == reign_before['defenses_count']
    assert reign_after['timestamp_acquired'] == reign_before['timestamp_acquired']
    assert reign_after['decklist'] == "https://moxfield.example/a"

    # The claim's history row is gone, the older one is still there.
    assert len(dbm.get_title_history(title_id)) == history_before

    # The rivalry row only existed because of the claim.
    assert dbm.get_top_rival(222) is None


def test_undo_claim_on_vacant_title_leaves_it_vacant(dbm):
    assert dbm.create_title("VacantForUndo", role_id=1301)
    title_id = dbm.get_title_by_name("VacantForUndo")['id']

    match_id = dbm.log_match("claim", title_id, actor_id=222)
    dbm.grant_title(title_id, 222)

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['reverted_holder_id'] == 222
    assert result['restored_holder_id'] is None
    assert result['restored_life'] is None
    assert result['restored_defenses'] is None
    assert dbm.get_active_reign_for_title(title_id) is None


def test_undo_claim_restores_consumed_bounty(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.set_bounty(title_id, True)

    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)
    dbm.grant_title(title_id, 222)  # grant_title clears bounty_active
    assert dbm.get_title_by_id(title_id)['bounty_active'] == 0

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert dbm.get_title_by_id(title_id)['bounty_active'] == 1
    assert result['sudden_death_cleared'] is False


def test_undo_defense_restores_life_defenses_and_streak(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.log_defense(title_id)
    dbm.log_defense(title_id)
    dbm.set_reign_stats(title_id, new_life=30)
    dbm.add_contender_win(title_id, 222)

    reign_before = dict(dbm.get_active_reign_for_title(title_id))

    match_id = dbm.log_match("defense", title_id, actor_id=champion_id)
    # Same order as the defense in VerificationView.verify.
    dbm.lifelink_reset(title_id)
    defense_result = dbm.log_defense(title_id)
    dbm.wipe_contenders(title_id)
    # Third defense in a row, so the momentum bonus applies.
    assert defense_result['bonus_applied'] is True
    assert dbm.get_active_reign_for_title(title_id)['current_life'] == DEFAULT_STARTING_LIFE + 5

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    reign_after = dbm.get_active_reign_for_title(title_id)
    assert reign_after['current_life'] == 30
    assert reign_after['defenses_count'] == reign_before['defenses_count']
    assert reign_after['defense_streak'] == reign_before['defense_streak']
    assert _contender_wins(dbm, title_id) == {222: 1}
    assert result['contenders_restored'] == 1


def test_undo_survived_draw_refunds_combat_damage(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    assert dbm.apply_combat_damage(title_id) == DEFAULT_STARTING_LIFE - DRAW_DAMAGE

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['sudden_death_cleared'] is False
    reign_after = dbm.get_active_reign_for_title(title_id)
    assert reign_after['discord_user_id'] == champion_id
    assert reign_after['current_life'] == DEFAULT_STARTING_LIFE
    assert dbm.get_title_by_id(title_id)['sudden_death_contenders'] is None


def test_undo_bled_out_draw_revives_champion_and_clears_sudden_death(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.set_reign_stats(title_id, new_life=DRAW_DAMAGE)
    history_before = len(dbm.get_title_history(title_id))

    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    # Same as DrawSelectView: the champion bleeds out and the title goes into sudden death.
    assert dbm.apply_combat_damage(title_id) == 0
    dbm.vacate_title(title_id, is_bounty=False, contenders_list="1,2,3")
    assert dbm.get_active_reign_for_title(title_id) is None

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['sudden_death_cleared'] is True
    assert result['reverted_holder_id'] is None
    assert result['restored_holder_id'] == champion_id

    reign_after = dbm.get_active_reign_for_title(title_id)
    assert reign_after['discord_user_id'] == champion_id
    assert reign_after['current_life'] == DRAW_DAMAGE
    assert dbm.get_title_by_id(title_id)['sudden_death_contenders'] is None
    # The vacate's history row is gone.
    assert len(dbm.get_title_history(title_id)) == history_before


def test_undo_usurper_win_restores_contender_board(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 333)

    match_id = dbm.log_match("usurp", title_id, actor_id=222, target_id=champion_id)
    assert dbm.add_contender_win(title_id, 222) == 3

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['contenders_restored'] == 2
    assert _contender_wins(dbm, title_id) == {222: 2, 333: 1}
    # Champion never lost the title.
    assert dbm.get_active_reign_for_title(title_id)['discord_user_id'] == champion_id


def test_undo_overthrow_restores_old_champion_and_contenders(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.log_defense(title_id)
    for _ in range(USURP_WINS_TO_OVERTHROW - 1):
        dbm.add_contender_win(title_id, 222)
    dbm.add_contender_win(title_id, 333)

    reign_before = dict(dbm.get_active_reign_for_title(title_id))
    history_before = len(dbm.get_title_history(title_id))

    match_id = dbm.log_match("usurp", title_id, actor_id=222, target_id=champion_id)
    # The winning click hits the overthrow number and crowns them.
    assert dbm.add_contender_win(title_id, 222) == USURP_WINS_TO_OVERTHROW
    dbm.execute_overthrow(title_id, 222)

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['reverted_holder_id'] == 222
    assert result['restored_holder_id'] == champion_id

    reign_after = dict(dbm.get_active_reign_for_title(title_id))
    assert reign_after['discord_user_id'] == champion_id
    assert reign_after['current_life'] == reign_before['current_life']
    assert reign_after['defenses_count'] == reign_before['defenses_count']
    assert reign_after['timestamp_acquired'] == reign_before['timestamp_acquired']
    assert _contender_wins(dbm, title_id) == {222: USURP_WINS_TO_OVERTHROW - 1, 333: 1}
    assert len(dbm.get_title_history(title_id)) == history_before


def test_undo_match_twice_reports_already_undone_and_changes_nothing(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    dbm.apply_combat_damage(title_id)

    first = dbm.undo_match(match_id, admin_id=777)
    assert first['status'] == "ok"
    reign_after_first = dict(dbm.get_active_reign_for_title(title_id))

    second = dbm.undo_match(match_id, admin_id=888)
    assert second['status'] == "already_undone"
    assert second['match_id'] == match_id
    assert second['undone_by'] == 777
    assert second['undone_at'] is not None
    assert dict(dbm.get_active_reign_for_title(title_id)) == reign_after_first


def test_undo_match_is_blocked_by_a_newer_match_on_the_same_title(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    match_one = dbm.log_match("draw", title_id, actor_id=champion_id)
    dbm.apply_combat_damage(title_id)  # 50 -> 30
    match_two = dbm.log_match("draw", title_id, actor_id=champion_id)
    dbm.apply_combat_damage(title_id)  # 30 -> 10

    stale = dbm.undo_match(match_one, admin_id=777)
    assert stale['status'] == "stale"
    assert stale['match_id'] == match_one
    assert stale['blocking_match_id'] == match_two
    assert stale['blocking_match_type'] == "draw"
    assert dbm.get_active_reign_for_title(title_id)['current_life'] == 10

    # Newest first.
    assert dbm.undo_match(match_two, admin_id=777)['status'] == "ok"
    assert dbm.get_active_reign_for_title(title_id)['current_life'] == 30
    assert dbm.undo_match(match_one, admin_id=777)['status'] == "ok"
    assert dbm.get_active_reign_for_title(title_id)['current_life'] == DEFAULT_STARTING_LIFE


def test_undo_match_unknown_id_returns_not_found(dbm):
    result = dbm.undo_match("  zzzzz ", admin_id=777)
    assert result['status'] == "not_found"
    assert result['match_id'] == "ZZZZZ"


def test_undo_claim_rolls_a_pre_existing_rivalry_back_instead_of_deleting_it(dbm, title_with_champion):
    """If the rivalry row already existed, undo should lower the count, not delete it."""
    title_id, champion_id = title_with_champion
    dbm.record_rivalry_win(222, champion_id)
    dbm.record_rivalry_win(222, champion_id)
    assert dbm.get_top_rival(222)['wins'] == 2

    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)
    dbm.grant_title(title_id, 222)
    dbm.record_rivalry_win(222, champion_id)
    assert dbm.get_top_rival(222)['wins'] == 3

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    rival_after = dbm.get_top_rival(222)
    assert rival_after is not None
    assert rival_after['loser_id'] == champion_id
    assert rival_after['wins'] == 2


def test_undo_match_refuses_after_a_monthly_reset_archived_an_eom_champion(dbm, title_with_champion):
    """The monthly reset saves the end of month champion without logging a match,
    so undoing an older match would delete that record. It should refuse.
    """
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)
    dbm.grant_title(title_id, 222)

    # Monthly reset archives the reign as end of month champion.
    dbm.reset_all_active_reigns()
    champions_before = [dict(row) for row in dbm.get_past_champions()]
    assert len(champions_before) == 1
    assert champions_before[0]['discord_user_id'] == 222

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "unlogged_change"
    assert result['match_id'] == match_id
    assert result['title_id'] == title_id
    assert "end-of-month" in result['reason']

    # Nothing got deleted and the match isn't marked undone.
    assert [dict(row) for row in dbm.get_past_champions()] == champions_before
    assert dbm.get_match(match_id)['undone'] == 0


def test_undo_defense_refuses_when_a_reign_was_archived_after_it(dbm, title_with_champion):
    """A defense doesn't archive anything, so a new history row means something else changed the title."""
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("defense", title_id, actor_id=champion_id)
    dbm.lifelink_reset(title_id)
    dbm.log_defense(title_id)

    # Upkeep loop vacates the title without logging a match.
    dbm.vacate_title(title_id)
    history_before = len(dbm.get_title_history(title_id))

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "unlogged_change"
    assert result['match_type'] == "defense"
    assert "defense never archives" in result['reason']
    assert len(dbm.get_title_history(title_id)) == history_before
    assert dbm.get_match(match_id)['undone'] == 0


def test_undo_match_refuses_when_more_reigns_were_archived_than_the_match_could_have(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)
    dbm.grant_title(title_id, 222)  # archives the old champion's reign (1)
    dbm.vacate_title(title_id)  # admin vacate archives another one (2)

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "unlogged_change"
    assert "could only have archived one" in result['reason']
    assert dbm.get_match(match_id)['undone'] == 0


def test_delete_title_purges_its_match_log_rows(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    assert dbm.get_match(match_id) is not None

    assert dbm.delete_title(title_id)

    assert dbm.get_match(match_id) is None
    assert dbm.get_recent_matches() == []


def test_undo_match_on_an_orphan_log_row_refuses_instead_of_resurrecting_a_reign(dbm, title_with_champion):
    """A match left behind by an old delete_title. Undoing it would bring back
    a reign for a title that doesn't exist.
    """
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    dbm.apply_combat_damage(title_id)

    # Delete the title the old way, leaving match_log behind.
    with dbm.get_connection() as conn:
        conn.execute('DELETE FROM active_reigns WHERE title_id = ?', (title_id,))
        conn.execute('DELETE FROM titles WHERE id = ?', (title_id,))
        conn.commit()

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "title_missing"
    assert result['match_id'] == match_id
    assert result['title_id'] == title_id
    with dbm.get_connection() as conn:
        orphans = conn.execute('SELECT COUNT(*) AS n FROM active_reigns WHERE title_id = ?', (title_id,)).fetchone()['n']
    assert orphans == 0


def test_undo_match_with_a_corrupt_snapshot_returns_error_instead_of_raising(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    with dbm.get_connection() as conn:
        conn.execute('UPDATE match_log SET snapshot = ? WHERE match_id = ?', ("{not json", match_id))
        conn.commit()

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "error"
    assert result['message']
    assert dbm.get_match(match_id)['undone'] == 0


def test_upkeep_blocks_elapsed_counts_whole_twelve_hour_blocks():
    now = datetime.datetime.now(datetime.timezone.utc)

    def stamp(hours_ago):
        return (now - datetime.timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')

    assert upkeep_blocks_elapsed(stamp(1)) == 0
    assert upkeep_blocks_elapsed(stamp(11.9)) == 0
    assert upkeep_blocks_elapsed(stamp(12.5)) == 1
    assert upkeep_blocks_elapsed(stamp(26)) == 2


def test_upkeep_blocks_elapsed_is_zero_for_missing_or_unparseable_timestamps():
    assert upkeep_blocks_elapsed(None) == 0
    assert upkeep_blocks_elapsed("") == 0
    assert upkeep_blocks_elapsed("not a timestamp") == 0
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')
    assert upkeep_blocks_elapsed(future) == 0


# --- Glicko-2 ratings ---


@pytest.fixture
def rated_pod(dbm, title_with_champion):
    """4 player claim: 111 beats 222, 333 and 444.
    Returns (title_id, match_id, winner_id, loser_ids).
    """
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    return title_id, match_id, champion_id, [222, 333, 444]


def test_get_player_rating_for_an_unknown_user_returns_the_defaults(dbm):
    rating = dbm.get_player_rating(555)

    assert rating['user_id'] == 555
    assert rating['rating'] == DEFAULT_RATING
    assert rating['rd'] == DEFAULT_RD
    assert rating['vol'] == DEFAULT_VOL
    assert rating['last_updated'] is None
    assert rating['is_provisional'] is True
    # Looking up a rating shouldn't create a row.
    assert dbm.get_rating_rank(555) == {"rank": None, "total": 0}


def test_setup_is_idempotent_and_creates_the_rating_tables(dbm):
    dbm.setup()  # second setup() call should be safe

    with dbm.get_connection() as conn:
        ratings_columns = [row['name'] for row in conn.execute('PRAGMA table_info(player_ratings)')]
        participant_columns = [row['name'] for row in conn.execute('PRAGMA table_info(match_participants)')]

    assert set(ratings_columns) == {"user_id", "rating", "rd", "vol", "last_updated"}
    assert set(participant_columns) == {
        "id", "match_log_id", "user_id", "result",
        "commander_name", "color_identity", "deck_url",
        "rating_before", "rd_before", "vol_before",
    }


def test_record_match_ratings_on_an_unknown_match_id_returns_none(dbm):
    assert dbm.record_match_ratings("ZZZZZ", 111, [222, 333]) is None


def test_record_match_ratings_rates_a_decided_four_player_pod(dbm, rated_pod):
    _, match_id, winner_id, loser_ids = rated_pod

    result = dbm.record_match_ratings(match_id, winner_id, loser_ids)

    assert result['match_id'] == match_id
    assert len(result['participants']) == 4

    by_user = {p['user_id']: p for p in result['participants']}
    assert by_user[winner_id]['rating_after'] > DEFAULT_RATING
    assert by_user[winner_id]['delta'] > 0
    for loser_id in loser_ids:
        assert by_user[loser_id]['rating_after'] < DEFAULT_RATING
        assert by_user[loser_id]['delta'] < 0

    for user_id, participant in by_user.items():
        stored = dbm.get_player_rating(user_id)
        assert stored['rating'] == pytest.approx(participant['rating_after'])
        assert stored['last_updated'] is not None


def test_match_participants_carry_results_and_null_pre_match_ratings(dbm, rated_pod):
    _, match_id, winner_id, loser_ids = rated_pod
    result = dbm.record_match_ratings(match_id, winner_id, loser_ids)

    rows = dbm.get_match_participants(result['match_log_id'])
    by_user = {row['user_id']: row for row in rows}

    assert by_user[winner_id]['result'] == "win"
    for loser_id in loser_ids:
        assert by_user[loser_id]['result'] == "loss"

    # Nobody had played before, so every rating_before is NULL.
    for row in rows:
        assert row['rating_before'] is None
        assert row['rd_before'] is None
        assert row['vol_before'] is None


def test_record_match_ratings_draw_leaves_equal_ratings_equal(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    pod = [champion_id, 222, 333, 444]
    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)

    result = dbm.record_match_ratings(match_id, None, pod, is_draw=True)

    assert len(result['participants']) == 4
    assert all(p['result'] == "draw" for p in result['participants'])

    rows = dbm.get_match_participants(result['match_log_id'])
    assert {row['result'] for row in rows} == {"draw"}

    # Four equal players drawing stay equal, only RD changes.
    ratings_after = {dbm.get_player_rating(user_id)['rating'] for user_id in pod}
    assert len(ratings_after) == 1
    assert ratings_after.pop() == pytest.approx(DEFAULT_RATING)


def test_record_match_ratings_is_idempotent_for_the_same_match(dbm, rated_pod):
    _, match_id, winner_id, loser_ids = rated_pod
    first = dbm.record_match_ratings(match_id, winner_id, loser_ids)
    ratings_after_first = {
        user_id: dbm.get_player_rating(user_id)['rating']
        for user_id in [winner_id] + loser_ids
    }

    # Double clicking verify shouldn't rate the pod twice.
    assert dbm.record_match_ratings(match_id, winner_id, loser_ids) is None

    assert len(dbm.get_match_participants(first['match_log_id'])) == 4
    for user_id, rating in ratings_after_first.items():
        assert dbm.get_player_rating(user_id)['rating'] == pytest.approx(rating)


def test_record_match_ratings_handles_a_two_player_pod(dbm, title_with_champion):
    """The /slayed path: one challenger vs the champion."""
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)

    result = dbm.record_match_ratings(match_id, 222, [champion_id])

    assert len(result['participants']) == 2
    assert dbm.get_player_rating(222)['rating'] > DEFAULT_RATING
    assert dbm.get_player_rating(champion_id)['rating'] < DEFAULT_RATING


def test_record_match_ratings_refuses_a_single_player_pod(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=champion_id)

    assert dbm.record_match_ratings(match_id, champion_id, []) is None

    match_log_id = dbm.get_match(match_id)['id']
    assert dbm.get_match_participants(match_log_id) == []
    assert dbm.get_rating_rank(champion_id) == {"rank": None, "total": 0}


def test_record_match_ratings_persists_deck_metadata_per_participant(dbm, rated_pod):
    _, match_id, winner_id, loser_ids = rated_pod
    deck_metadata = {
        winner_id: {
            "commander_name": "Kinnan, Bonder Prodigy",
            "color_identity": "GU",
            "deck_url": "https://moxfield.com/decks/abc",
        },
        loser_ids[0]: {
            "commander_name": "Rograkh, Son of Rohgahh",
            "color_identity": "R",
            "deck_url": None,
        },
    }

    result = dbm.record_match_ratings(match_id, winner_id, loser_ids, deck_metadata=deck_metadata)

    rows = {row['user_id']: row for row in dbm.get_match_participants(result['match_log_id'])}
    assert rows[winner_id]['commander_name'] == "Kinnan, Bonder Prodigy"
    assert rows[winner_id]['color_identity'] == "GU"
    assert rows[winner_id]['deck_url'] == "https://moxfield.com/decks/abc"
    assert rows[loser_ids[0]]['commander_name'] == "Rograkh, Son of Rohgahh"
    assert rows[loser_ids[0]]['deck_url'] is None
    # Players with no deck submitted get NULLs.
    for absent_id in loser_ids[1:]:
        assert rows[absent_id]['commander_name'] is None
        assert rows[absent_id]['color_identity'] is None


def test_set_match_deck_metadata_writes_the_match_log_row_case_insensitively(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=champion_id)

    assert dbm.set_match_deck_metadata(
        match_id.lower(), "Najeela, the Blade-Blossom", "WUBRG", "https://moxfield.com/decks/xyz"
    ) is True

    row = dbm.get_match(match_id)
    assert row['commander_name'] == "Najeela, the Blade-Blossom"
    assert row['color_identity'] == "WUBRG"
    assert row['deck_url'] == "https://moxfield.com/decks/xyz"

    assert dbm.set_match_deck_metadata("ZZZZZ", "Tymna", "WB", None) is False


def test_get_rating_rank_is_one_based_over_conservative_order(dbm, title_with_champion):
    title_id, _ = title_with_champion
    first = dbm.log_match("claim", title_id, actor_id=111, target_id=222)
    dbm.record_match_ratings(first, 111, [222])
    second = dbm.log_match("claim", title_id, actor_id=333, target_id=222)
    dbm.record_match_ratings(second, 333, [222])

    leaderboard = dbm.get_rating_leaderboard()
    ordered_ids = [entry['user_id'] for entry in leaderboard]
    conservatives = [entry['conservative'] for entry in leaderboard]

    assert len(ordered_ids) == 3
    assert conservatives == sorted(conservatives, reverse=True)
    assert conservatives[0] == pytest.approx(leaderboard[0]['rating'] - 2 * leaderboard[0]['rd'])
    # 222 lost both games so they're last.
    assert ordered_ids[-1] == 222

    for position, user_id in enumerate(ordered_ids, start=1):
        assert dbm.get_rating_rank(user_id) == {"rank": position, "total": 3}

    assert dbm.get_rating_rank(999) == {"rank": None, "total": 3}


def test_undo_match_restores_every_participants_rating(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    # An earlier rated match so two players already have ratings and two don't.
    warmup = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(warmup, champion_id, [222])
    ratings_before = {
        user_id: dbm.get_player_rating(user_id)
        for user_id in (champion_id, 222)
    }

    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    recorded = dbm.record_match_ratings(match_id, champion_id, [222, 333, 444])
    assert len(recorded['participants']) == 4
    assert dbm.get_player_rating(333)['last_updated'] is not None

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['ratings_reverted'] == 4

    for user_id, expected in ratings_before.items():
        restored = dbm.get_player_rating(user_id)
        assert restored['rating'] == pytest.approx(expected['rating'])
        assert restored['rd'] == pytest.approx(expected['rd'])
        assert restored['vol'] == pytest.approx(expected['vol'])

    # 333 and 444 had no rating before, so undo deletes their rows.
    with dbm.get_connection() as conn:
        rows = conn.execute('SELECT user_id FROM player_ratings').fetchall()
    assert {row['user_id'] for row in rows} == {champion_id, 222}
    assert dbm.get_player_rating(333)['last_updated'] is None

    assert dbm.get_match_participants(recorded['match_log_id']) == []


def test_undo_match_ok_dict_keeps_every_existing_key(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=222, target_id=champion_id)
    dbm.grant_title(title_id, 222)
    dbm.record_match_ratings(match_id, 222, [champion_id])

    result = dbm.undo_match(match_id, admin_id=777)

    assert set(result.keys()) == {
        "status", "match_id", "match_type", "title_id", "title_name",
        "actor_id", "target_id", "timestamp",
        "reverted_holder_id", "reverted_life",
        "restored_holder_id", "restored_life", "restored_defenses",
        "sudden_death_cleared", "contenders_restored",
        "ratings_reverted", "ratings_stale_for",
    }
    assert result['status'] == "ok"
    assert result['ratings_reverted'] == 2
    assert result['ratings_stale_for'] == 0
    assert result['restored_holder_id'] == champion_id


def test_delete_title_purges_its_match_participants(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    recorded = dbm.record_match_ratings(match_id, champion_id, [222, 333, 444])
    match_log_id = recorded['match_log_id']

    assert dbm.delete_title(title_id) is True

    assert dbm.get_match_participants(match_log_id) == []
    with dbm.get_connection() as conn:
        remaining = conn.execute('SELECT COUNT(*) AS n FROM match_participants').fetchone()['n']
    assert remaining == 0
    # Ratings are global, not per title, so they stay.
    assert dbm.get_rating_rank(champion_id)['total'] == 4


def test_record_match_ratings_rolls_back_a_partial_write(dbm, rated_pod, monkeypatch):
    """If it fails halfway through, nothing should be saved.

    The fake rate_period drops the last player, so three rows get written and
    then it raises KeyError on the fourth.
    """
    _, match_id, winner_id, loser_ids = rated_pod
    real_rate_period = db_manager.rate_period

    def drop_the_last_player(players, results):
        rated = real_rate_period(players, results)
        rated.pop(loser_ids[-1])
        return rated

    monkeypatch.setattr(db_manager, "rate_period", drop_the_last_player)

    assert dbm.record_match_ratings(match_id, winner_id, loser_ids) is None

    match_log_id = dbm.get_match(match_id)['id']
    assert dbm.get_match_participants(match_log_id) == []
    with dbm.get_connection() as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM player_ratings').fetchone()['n'] == 0

    # The failed attempt shouldn't block rating it again later.
    monkeypatch.setattr(db_manager, "rate_period", real_rate_period)
    retry = dbm.record_match_ratings(match_id, winner_id, loser_ids)
    assert retry is not None
    assert len(retry['participants']) == 4


def test_record_match_ratings_deduplicates_the_pod(dbm, title_with_champion):
    """If the winner is listed as a loser or a loser is repeated, it should still
    be one row per player.
    """
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=111, target_id=222)

    result = dbm.record_match_ratings(match_id, 111, [222, 111, 222])

    assert [p['user_id'] for p in result['participants']] == [111, 222]
    rows = dbm.get_match_participants(result['match_log_id'])
    assert [row['user_id'] for row in rows] == [111, 222]
    assert rows[0]['result'] == "win"
    assert rows[1]['result'] == "loss"


def test_undo_keeps_the_rating_row_of_a_player_with_other_participation(dbm, title_with_champion):
    """A player who wasn't rated before this match but has played since
    shouldn't lose their rating when this match is undone.
    """
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(match_id, champion_id, [222, 333, 444])

    # A later match on a different title, so the per title check doesn't block the undo.
    assert dbm.create_title("SecondTitle", role_id=998)
    other_title_id = dbm.get_title_by_name("SecondTitle")['id']
    later_match = dbm.log_match("claim", other_title_id, actor_id=333, target_id=444)
    dbm.record_match_ratings(later_match, 333, [444])
    ratings_from_the_later_match = {
        user_id: dbm.get_player_rating(user_id)['rating'] for user_id in (333, 444)
    }

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['ratings_reverted'] == 4
    assert result['ratings_stale_for'] == 2

    # 111 and 222 didn't play anything else, so their rows get deleted.
    with dbm.get_connection() as conn:
        rows = conn.execute('SELECT user_id FROM player_ratings').fetchall()
    assert {row['user_id'] for row in rows} == {333, 444}

    # 333 and 444 keep the rating from the later match.
    for user_id, rating in ratings_from_the_later_match.items():
        assert dbm.get_player_rating(user_id)['rating'] == pytest.approx(rating)


def test_undo_reports_a_stale_rating_for_a_player_rated_by_a_later_match(dbm, title_with_champion):
    """The old rating gets written back, but since a later match moved the
    player it gets counted in ratings_stale_for.
    """
    title_id, champion_id = title_with_champion

    warmup = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(warmup, champion_id, [222])
    rating_before_the_undone_match = dbm.get_player_rating(champion_id)['rating']

    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(match_id, champion_id, [222, 333, 444])

    assert dbm.create_title("ThirdTitle", role_id=995)
    other_title_id = dbm.get_title_by_name("ThirdTitle")['id']
    later_match = dbm.log_match("claim", other_title_id, actor_id=champion_id, target_id=555)
    dbm.record_match_ratings(later_match, champion_id, [555])

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert result['ratings_reverted'] == 4
    # Only the champion, since they're the only one with a later match.
    assert result['ratings_stale_for'] == 1
    assert dbm.get_player_rating(champion_id)['rating'] == pytest.approx(rating_before_the_undone_match)
    with dbm.get_connection() as conn:
        rows = conn.execute('SELECT user_id FROM player_ratings').fetchall()
    assert {row['user_id'] for row in rows} == {champion_id, 222, 555}


# Stats queries. The data is built with log_match and record_match_ratings,
# not by inserting rows by hand.

def test_get_player_match_record_for_a_user_with_no_matches_returns_zeros(dbm):
    record = dbm.get_player_match_record(999)
    assert record == {"matches": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0.0}


def test_get_player_match_record_after_one_four_player_win(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(match_id, champion_id, [222, 333, 444])

    winner_record = dbm.get_player_match_record(champion_id)
    assert winner_record == {"matches": 1, "wins": 1, "losses": 0, "draws": 0, "win_rate": 100.0}

    for loser_id in (222, 333, 444):
        loser_record = dbm.get_player_match_record(loser_id)
        assert loser_record["matches"] == 1
        assert loser_record["losses"] == 1
        assert loser_record["win_rate"] == 0.0


def test_get_player_match_record_after_a_win_and_a_loss_is_fifty_percent(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    first = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(first, champion_id, [222])

    second = dbm.log_match("claim", title_id, actor_id=333, target_id=champion_id)
    dbm.record_match_ratings(second, 333, [champion_id])

    record = dbm.get_player_match_record(champion_id)
    assert record["matches"] == 2
    assert record["wins"] == 1
    assert record["losses"] == 1
    assert record["win_rate"] == 50.0


def test_get_player_match_record_draw_counts_but_not_as_a_win(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    win_match = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(win_match, champion_id, [222])

    draw_match = dbm.log_match("draw", title_id, actor_id=champion_id)
    dbm.record_match_ratings(draw_match, None, [champion_id, 333, 444], is_draw=True)

    record = dbm.get_player_match_record(champion_id)
    assert record["matches"] == 2
    assert record["wins"] == 1
    assert record["draws"] == 1
    assert record["losses"] == 0
    assert record["win_rate"] == 50.0


def test_get_player_match_record_excludes_an_undone_match(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    match_id = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(match_id, champion_id, [222])
    assert dbm.get_player_match_record(champion_id)["matches"] == 1

    result = dbm.undo_match(match_id, admin_id=999)
    assert result["status"] == "ok"

    # undo_match deletes the participant rows, so the record goes back to empty.
    assert dbm.get_player_match_record(champion_id) == {
        "matches": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0.0
    }


def test_get_player_commanders_excludes_null_commander_and_orders_by_matches(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    # No deck info, so it's left out of the breakdown.
    unattributed = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(unattributed, champion_id, [222])

    # One match on Rograkh.
    m_rograkh = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=333)
    dbm.record_match_ratings(m_rograkh, champion_id, [333], deck_metadata={
        champion_id: {"commander_name": "Rograkh, Son of Rohgahh", "color_identity": "R", "deck_url": None}
    })

    # Two matches on Kinnan.
    for target in (444, 555):
        m = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=target)
        dbm.record_match_ratings(m, champion_id, [target], deck_metadata={
            champion_id: {"commander_name": "Kinnan, Bonder Prodigy", "color_identity": "GU", "deck_url": None}
        })

    commanders = dbm.get_player_commanders(champion_id)
    names = [c["commander_name"] for c in commanders]
    assert names == ["Kinnan, Bonder Prodigy", "Rograkh, Son of Rohgahh"]
    assert commanders[0]["matches"] == 2
    assert commanders[1]["matches"] == 1
    assert commanders[0]["color_identity"] == "GU"


def test_get_player_commanders_computes_win_rate_across_repeated_matches(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    deck = {"commander_name": "Najeela, the Blade-Blossom", "color_identity": "WUBRG", "deck_url": None}

    m1 = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(m1, champion_id, [222], deck_metadata={champion_id: deck})  # win
    m2 = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=333)
    dbm.record_match_ratings(m2, champion_id, [333], deck_metadata={champion_id: deck})  # win
    m3 = dbm.log_match("claim", title_id, actor_id=444, target_id=champion_id)
    dbm.record_match_ratings(m3, 444, [champion_id], deck_metadata={champion_id: deck})  # loss

    commanders = dbm.get_player_commanders(champion_id)
    assert len(commanders) == 1
    assert commanders[0]["matches"] == 3
    assert commanders[0]["wins"] == 2
    assert commanders[0]["win_rate"] == 66.7


def test_get_player_commanders_tie_break_is_deterministic(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    def logged_win(commander_name, color, opponent_id):
        m = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=opponent_id)
        dbm.record_match_ratings(m, champion_id, [opponent_id], deck_metadata={
            champion_id: {"commander_name": commander_name, "color_identity": color, "deck_url": None}
        })

    def logged_loss(commander_name, color, opponent_id):
        m = dbm.log_match("claim", title_id, actor_id=opponent_id, target_id=champion_id)
        dbm.record_match_ratings(m, opponent_id, [champion_id], deck_metadata={
            champion_id: {"commander_name": commander_name, "color_identity": color, "deck_url": None}
        })

    # All four commanders have 1 match. Zzzzz and Aaaaa have different win rates,
    # Bravo and Charlie tie on everything so only the name sorts them.
    logged_win("Zzzzz Commander", "U", 222)
    logged_loss("Aaaaa Commander", "B", 333)
    logged_win("Charlie Commander", "G", 444)
    logged_win("Bravo Commander", "W", 555)

    commanders = dbm.get_player_commanders(champion_id, limit=10)
    names = [c["commander_name"] for c in commanders]
    assert names == [
        "Bravo Commander", "Charlie Commander", "Zzzzz Commander", "Aaaaa Commander",
    ]


def test_get_player_title_summary_active_and_historical(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    dbm.set_reign_stats(title_id, new_life=42, new_defenses=3)

    # Archive a second title's reign for the same user.
    assert dbm.create_title("SecondTitle", role_id=888)
    second_title_id = dbm.get_title_by_name("SecondTitle")['id']
    dbm.grant_title(second_title_id, champion_id)
    dbm.set_reign_stats(second_title_id, new_defenses=5)
    dbm.vacate_title(second_title_id)  # archives with total_defenses=5

    summary = dbm.get_player_title_summary(champion_id)

    assert summary["titles_held"] == 2
    assert summary["lifetime_defenses"] == 3 + 5
    assert len(summary["active_titles"]) == 1
    active = summary["active_titles"][0]
    assert active["title_id"] == title_id
    assert active["title_name"] == "KingSlayer"
    assert active["defenses"] == 3
    assert active["current_life"] == 42


def test_get_server_meta_on_an_empty_database_returns_the_full_key_set(dbm):
    meta = dbm.get_server_meta()
    assert meta == {
        "top_commanders": [],
        "color_breakdown": [],
        "total_matches": 0,
        "attributed_matches": 0,
        "active_players": 0,
        "rated_players": 0,
    }


def test_get_server_meta_top_commanders_is_capped_and_ordered(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    commanders = [
        ("Commander A", "U", 1),
        ("Commander B", "B", 2),
        ("Commander C", "R", 3),
    ]
    opponent = 900
    for name, color, count in commanders:
        for _ in range(count):
            opponent += 1
            m = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=opponent)
            dbm.record_match_ratings(m, champion_id, [opponent], deck_metadata={
                champion_id: {"commander_name": name, "color_identity": color, "deck_url": None}
            })

    meta = dbm.get_server_meta(limit=2)
    assert len(meta["top_commanders"]) == 2
    assert [c["commander_name"] for c in meta["top_commanders"]] == ["Commander C", "Commander B"]
    assert [c["matches"] for c in meta["top_commanders"]] == [3, 2]


def test_get_server_meta_color_breakdown_shares_sum_to_100(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    decks = [
        ("Commander A", "U", 222),
        ("Commander B", "B", 333),
        ("Commander C", "R", 444),
    ]
    for name, color, opponent in decks:
        m = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=opponent)
        dbm.record_match_ratings(m, champion_id, [opponent], deck_metadata={
            champion_id: {"commander_name": name, "color_identity": color, "deck_url": None}
        })

    meta = dbm.get_server_meta()
    total_share = sum(c["share"] for c in meta["color_breakdown"])
    assert abs(total_share - 100.0) < 0.5


def test_get_server_meta_active_players_counts_distinct_participants(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    # champion_id is in two matches but only counted once.
    m1 = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(m1, champion_id, [222])
    m2 = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=333)
    dbm.record_match_ratings(m2, champion_id, [333])

    meta = dbm.get_server_meta()
    assert meta["active_players"] == 3  # champion_id, 222, 333


def test_get_rating_leaderboard_is_empty_on_a_fresh_database(dbm):
    assert dbm.get_rating_leaderboard() == []


def test_get_rating_leaderboard_orders_by_conservative_rating(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    # 111 wins twice, 333 wins once, 222 loses both. 222 should be last and
    # 111 should be above 333.
    first = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(first, champion_id, [222])
    second = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=333)
    dbm.record_match_ratings(second, champion_id, [333])
    third = dbm.log_match("claim", title_id, actor_id=333, target_id=222)
    dbm.record_match_ratings(third, 333, [222])

    leaderboard = dbm.get_rating_leaderboard()
    ordered_ids = [entry['user_id'] for entry in leaderboard]
    conservatives = [entry['conservative'] for entry in leaderboard]

    assert len(leaderboard) == 3
    assert conservatives == sorted(conservatives, reverse=True)
    assert ordered_ids[0] == champion_id
    assert ordered_ids[-1] == 222

    for entry in leaderboard:
        assert entry['conservative'] == pytest.approx(entry['rating'] - 2 * entry['rd'])


def test_get_rating_leaderboard_respects_limit(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    first = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=222)
    dbm.record_match_ratings(first, champion_id, [222])
    second = dbm.log_match("claim", title_id, actor_id=champion_id, target_id=333)
    dbm.record_match_ratings(second, champion_id, [333])

    assert len(dbm.get_rating_leaderboard(limit=10)) == 3
    assert len(dbm.get_rating_leaderboard(limit=1)) == 1


def test_reset_all_active_reigns_flags_lineal_reversion(dbm):
    """The reign the monthly reset gives back to the lineal champion should be flagged."""
    assert dbm.create_title("LinealCrown", role_id=1200)
    title_id = dbm.get_title_by_name("LinealCrown")['id']
    dbm.set_original_holder(title_id, 401)

    dbm.reset_all_active_reigns()

    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['discord_user_id'] == 401
    assert reign['is_lineal_reversion'] == 1


def test_grant_title_reign_is_not_a_lineal_reversion(dbm, title_with_champion):
    title_id, champion_id = title_with_champion
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['discord_user_id'] == champion_id
    assert reign['is_lineal_reversion'] == 0


def test_get_ironman_standings_excludes_lineal_reversion_claim_but_counts_normal_claim(dbm):
    # Title with a lineal champion. The reset hands it back to 402, that isn't a claim.
    assert dbm.create_title("LinealCrown2", role_id=1201)
    lineal_title_id = dbm.get_title_by_name("LinealCrown2")['id']
    dbm.set_original_holder(lineal_title_id, 402)

    # A normal title claimed by someone else in the same month.
    assert dbm.create_title("ClaimedCrown", role_id=1202)
    claimed_title_id = dbm.get_title_by_name("ClaimedCrown")['id']
    dbm.grant_title(claimed_title_id, 403)

    dbm.reset_all_active_reigns()

    standings = {row['discord_user_id']: row for row in dbm.get_ironman_standings(limit=10)}

    # 402's only reign is the reversion, so they score 0 and don't show up.
    assert 402 not in standings

    # 403's reign was before the reset, so it's a normal claim.
    assert standings[403]['claims_this_month'] == 1


def test_get_ironman_standings_reversion_only_user_cannot_place(dbm):
    """A user whose only reign is a reversion scores 0 and shouldn't show up at all."""
    assert dbm.create_title("LinealCrown3", role_id=1203)
    title_id = dbm.get_title_by_name("LinealCrown3")['id']
    dbm.set_original_holder(title_id, 404)
    dbm.reset_all_active_reigns()

    standings = {row['discord_user_id']: row for row in dbm.get_ironman_standings(limit=10)}
    assert 404 not in standings


def test_get_ironman_standings_empty_when_only_reversions_this_month(dbm):
    """If every reign is a reversion, the standings should be empty."""
    assert dbm.create_title("LinealCrownOnly1", role_id=1213)
    t1 = dbm.get_title_by_name("LinealCrownOnly1")['id']
    dbm.set_original_holder(t1, 420)

    assert dbm.create_title("LinealCrownOnly2", role_id=1214)
    t2 = dbm.get_title_by_name("LinealCrownOnly2")['id']
    dbm.set_original_holder(t2, 421)

    dbm.reset_all_active_reigns()

    assert dbm.get_ironman_standings(limit=10) == []


def test_reset_all_active_reigns_carries_reversion_flag_onto_history_on_second_reset(dbm):
    """A reversion that lasts to the next reset should still be flagged in
    historical_stats, otherwise it starts counting as a claim.
    """
    assert dbm.create_title("LinealCrown4", role_id=1204)
    title_id = dbm.get_title_by_name("LinealCrown4")['id']
    dbm.set_original_holder(title_id, 405)

    dbm.reset_all_active_reigns()  # creates the reversion reign
    dbm.reset_all_active_reigns()  # archives it and creates a new one

    with dbm.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT is_lineal_reversion FROM historical_stats WHERE title_id = ? ORDER BY id DESC LIMIT 1',
            (title_id,),
        )
        archived = cursor.fetchone()
    assert archived['is_lineal_reversion'] == 1

    # The new reign is flagged too.
    reign = dbm.get_active_reign_for_title(title_id)
    assert reign['is_lineal_reversion'] == 1


def test_get_ironman_standings_still_counts_defenses_on_a_reversion_reign(dbm):
    """Defenses on a reversion still count."""
    assert dbm.create_title("LinealCrown5", role_id=1205)
    title_id = dbm.get_title_by_name("LinealCrown5")['id']
    dbm.set_original_holder(title_id, 406)
    dbm.reset_all_active_reigns()

    dbm.log_defense(title_id)
    dbm.log_defense(title_id)
    dbm.log_defense(title_id)

    standings = {row['discord_user_id']: row for row in dbm.get_ironman_standings(limit=10)}
    assert standings[406]['claims_this_month'] == 0
    assert standings[406]['defenses_this_month'] == 3
    assert standings[406]['total_score'] == 3


# --- Overthrow bounty life, and is_lineal_reversion carried through every archive path ---


def test_execute_overthrow_clears_bounty_flag_and_sudden_death_contenders(dbm, title_with_champion):
    """The usurper gets bounty life, but the bounty flag and sudden death list still get cleared."""
    title_id, _ = title_with_champion
    with dbm.get_connection() as conn:
        conn.execute(
            'UPDATE titles SET bounty_active = 1, sudden_death_contenders = ? WHERE id = ?',
            ("111,222", title_id),
        )
        conn.commit()

    dbm.execute_overthrow(title_id, 222)

    title_row = dbm.get_title_by_id(title_id)
    assert title_row['bounty_active'] == 0
    assert title_row['sudden_death_contenders'] is None


def _last_historical_row(dbm, title_id):
    with dbm.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM historical_stats WHERE title_id = ? ORDER BY id DESC LIMIT 1',
            (title_id,),
        )
        return cursor.fetchone()


def test_vacate_title_carries_reversion_flag_onto_history(dbm):
    assert dbm.create_title("LinealCrown6", role_id=1206)
    title_id = dbm.get_title_by_name("LinealCrown6")['id']
    dbm.set_original_holder(title_id, 410)
    dbm.reset_all_active_reigns()  # creates the reversion reign
    assert dbm.get_active_reign_for_title(title_id)['is_lineal_reversion'] == 1

    dbm.vacate_title(title_id)

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 410
    assert archived['is_lineal_reversion'] == 1


def test_grant_title_carries_reversion_flag_onto_history(dbm):
    assert dbm.create_title("LinealCrown7", role_id=1207)
    title_id = dbm.get_title_by_name("LinealCrown7")['id']
    dbm.set_original_holder(title_id, 411)
    dbm.reset_all_active_reigns()
    assert dbm.get_active_reign_for_title(title_id)['is_lineal_reversion'] == 1

    # Someone claims the reverted title mid month.
    dbm.grant_title(title_id, 412)

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 411
    assert archived['is_lineal_reversion'] == 1

    # That's a real claim, not a reversion.
    assert dbm.get_active_reign_for_title(title_id)['is_lineal_reversion'] == 0


def test_decay_title_carries_reversion_flag_onto_history(dbm):
    assert dbm.create_title("LinealCrown8", role_id=1208)
    title_id = dbm.get_title_by_name("LinealCrown8")['id']
    dbm.set_original_holder(title_id, 413)
    dbm.reset_all_active_reigns()
    assert dbm.get_active_reign_for_title(title_id)['is_lineal_reversion'] == 1

    assert dbm.decay_title(title_id) is True

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 413
    assert archived['is_lineal_reversion'] == 1


def test_execute_overthrow_carries_reversion_flag_onto_history(dbm):
    assert dbm.create_title("LinealCrown9", role_id=1209)
    title_id = dbm.get_title_by_name("LinealCrown9")['id']
    dbm.set_original_holder(title_id, 414)
    dbm.reset_all_active_reigns()
    assert dbm.get_active_reign_for_title(title_id)['is_lineal_reversion'] == 1

    dbm.execute_overthrow(title_id, 415)

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 414
    assert archived['is_lineal_reversion'] == 1

    # The usurper isn't a reversion and gets bounty life.
    new_reign = dbm.get_active_reign_for_title(title_id)
    assert new_reign['is_lineal_reversion'] == 0
    assert new_reign['current_life'] == BOUNTY_STARTING_LIFE


def test_get_ironman_standings_excludes_reversion_claim_after_it_is_claimed_away_mid_month(dbm):
    """A reversion that gets claimed away mid month still counts as 0 claims.
    I log a defense first so 416 still shows up in the standings.
    """
    assert dbm.create_title("LinealCrown10", role_id=1210)
    title_id = dbm.get_title_by_name("LinealCrown10")['id']
    dbm.set_original_holder(title_id, 416)
    dbm.reset_all_active_reigns()

    dbm.log_defense(title_id)
    dbm.grant_title(title_id, 417)

    standings = {row['discord_user_id']: row for row in dbm.get_ironman_standings(limit=10)}

    assert 416 in standings
    assert standings[416]['claims_this_month'] == 0
    assert standings[416]['defenses_this_month'] == 1

    assert standings[417]['claims_this_month'] == 1


def _set_timestamp_acquired(dbm, title_id, when: datetime.datetime):
    with dbm.get_connection() as conn:
        conn.execute(
            'UPDATE active_reigns SET timestamp_acquired = ? WHERE title_id = ?',
            (when.strftime('%Y-%m-%d %H:%M:%S'), title_id),
        )
        conn.commit()


def test_get_monthly_iron_man_counts_defenses_but_not_claim_on_a_reversion_reign(dbm, monkeypatch):
    """Same as the ironman standings test but for get_monthly_iron_man, which uses
    the America/Chicago month boundary.

    I freeze datetime.now() for the whole test so the function's "now" and the
    timestamps I insert can't land in different months if the test runs right
    around midnight Chicago time.

    The second claimant has 1 claim and fewer defenses, so the reversion holder
    only wins if defenses are actually added into the score.
    """
    frozen_now = datetime.datetime.now(datetime.timezone.utc)

    class _FrozenDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return frozen_now.astimezone(tz)
            return frozen_now.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(db_manager.datetime, 'datetime', _FrozenDatetime)

    assert dbm.create_title("LinealCrown11", role_id=1211)
    title_id = dbm.get_title_by_name("LinealCrown11")['id']
    dbm.set_original_holder(title_id, 418)
    dbm.reset_all_active_reigns()

    dbm.log_defense(title_id)
    dbm.log_defense(title_id)
    dbm.log_defense(title_id)
    _set_timestamp_acquired(dbm, title_id, frozen_now)

    # Second claimant: 1 claim + 1 defense = 2.
    # Reversion holder: 0 claims + 3 defenses = 3, so they should still win.
    assert dbm.create_title("ClaimedCrown11", role_id=1212)
    second_title_id = dbm.get_title_by_name("ClaimedCrown11")['id']
    dbm.grant_title(second_title_id, 419)
    dbm.log_defense(second_title_id)
    _set_timestamp_acquired(dbm, second_title_id, frozen_now)

    top = dbm.get_monthly_iron_man()
    assert top is not None
    assert top['discord_user_id'] == 418
    assert top['claims_this_month'] == 0
    assert top['defenses_this_month'] == 3


def test_get_monthly_iron_man_none_when_only_reversions_this_month(dbm):
    """Only reversions this month should give None, so the end of month post
    doesn't announce an Iron Man with 0 claims and 0 defenses.
    """
    assert dbm.create_title("LinealCrownOnly3", role_id=1215)
    t1 = dbm.get_title_by_name("LinealCrownOnly3")['id']
    dbm.set_original_holder(t1, 422)

    assert dbm.create_title("LinealCrownOnly4", role_id=1216)
    t2 = dbm.get_title_by_name("LinealCrownOnly4")['id']
    dbm.set_original_holder(t2, 423)

    dbm.reset_all_active_reigns()

    assert dbm.get_monthly_iron_man() is None


# --- /slayerstats: reversions don't count as claims, but defenses and reign time still do ---


def test_get_user_stats_excludes_reversion_only_claim(dbm):
    """A user whose only reign is a reversion has 0 claims."""
    assert dbm.create_title("StatsCrown1", role_id=1300)
    title_id = dbm.get_title_by_name("StatsCrown1")['id']
    dbm.set_original_holder(title_id, 501)
    dbm.reset_all_active_reigns()  # creates the reversion reign

    stats = dbm.get_user_stats(501)
    assert stats['total_claims'] == 0


def test_get_user_stats_counts_normal_claim_but_not_reversion(dbm):
    """One real claim and one reversion should be 1 claim."""
    assert dbm.create_title("StatsCrown2", role_id=1301)
    reversion_title_id = dbm.get_title_by_name("StatsCrown2")['id']
    dbm.set_original_holder(reversion_title_id, 502)
    dbm.reset_all_active_reigns()  # reversion reign for 502

    assert dbm.create_title("StatsCrown2b", role_id=1302)
    claimed_title_id = dbm.get_title_by_name("StatsCrown2b")['id']
    dbm.grant_title(claimed_title_id, 502)  # a real claim

    stats = dbm.get_user_stats(502)
    assert stats['total_claims'] == 1


def test_get_user_stats_still_counts_defenses_on_reversion_reign(dbm):
    """Defenses on a reversion still show up in stats."""
    assert dbm.create_title("StatsCrown3", role_id=1303)
    title_id = dbm.get_title_by_name("StatsCrown3")['id']
    dbm.set_original_holder(title_id, 503)
    dbm.reset_all_active_reigns()

    dbm.log_defense(title_id)
    dbm.log_defense(title_id)

    stats = dbm.get_user_stats(503)
    assert stats['total_claims'] == 0
    assert stats['max_defenses'] == 2


def test_get_user_stats_counts_pre_migration_style_reign(dbm):
    """An old row from before is_lineal_reversion existed defaults to 0 and
    should still count as a claim.
    """
    assert dbm.create_title("StatsCrown4", role_id=1304)
    title_id = dbm.get_title_by_name("StatsCrown4")['id']
    with dbm.get_connection() as conn:
        conn.execute('''
            INSERT INTO historical_stats
            (title_id, discord_user_id, timestamp_acquired, timestamp_lost, total_defenses, is_lineal_reversion)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 0)
        ''', (title_id, 504))
        conn.commit()

    stats = dbm.get_user_stats(504)
    assert stats['total_claims'] == 1


# --- /past_champions shouldn't credit automatic handbacks ---
# A reign only gets is_eom_champ = 0 if it's a reversion AND has 0 defenses.


def test_reset_all_active_reigns_zero_defense_reversion_not_eom_champ(dbm):
    assert dbm.create_title("EomCrown1", role_id=1400)
    title_id = dbm.get_title_by_name("EomCrown1")['id']
    dbm.set_original_holder(title_id, 601)
    dbm.reset_all_active_reigns()  # creates the reversion reign, 0 defenses

    dbm.reset_all_active_reigns()  # archives it at the next month end

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 601
    assert archived['is_lineal_reversion'] == 1
    assert archived['is_eom_champ'] == 0


def test_reset_all_active_reigns_defended_reversion_keeps_eom_champ(dbm):
    assert dbm.create_title("EomCrown2", role_id=1401)
    title_id = dbm.get_title_by_name("EomCrown2")['id']
    dbm.set_original_holder(title_id, 602)
    dbm.reset_all_active_reigns()  # creates the reversion reign

    dbm.log_defense(title_id)  # held it to the cutoff

    dbm.reset_all_active_reigns()  # archives it

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 602
    assert archived['is_lineal_reversion'] == 1
    assert archived['is_eom_champ'] == 1


def test_reset_all_active_reigns_ordinary_zero_defense_reign_keeps_eom_champ(dbm, title_with_champion):
    """A normal reign with 0 defenses is still an end of month champion."""
    title_id, champion_id = title_with_champion

    dbm.reset_all_active_reigns()  # archives the champion's normal reign

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == champion_id
    assert archived['is_lineal_reversion'] == 0
    assert archived['is_eom_champ'] == 1


def test_get_past_champions_excludes_zero_defense_reversion(dbm):
    assert dbm.create_title("EomCrown4", role_id=1403)
    title_id = dbm.get_title_by_name("EomCrown4")['id']
    dbm.set_original_holder(title_id, 604)
    dbm.reset_all_active_reigns()  # creates the reversion reign
    dbm.reset_all_active_reigns()  # archives it with is_eom_champ = 0

    champions = dbm.get_past_champions()
    assert not any(row['discord_user_id'] == 604 for row in champions)


# --- archived_by_reset and the undo_match check it's used for ---

def test_reset_all_active_reigns_stamps_archived_by_reset_on_an_ordinary_reign(dbm, title_with_champion):
    title_id, champion_id = title_with_champion

    dbm.reset_all_active_reigns()  # archives the champion's normal reign

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == champion_id
    assert archived['archived_by_reset'] == 1


def test_reset_all_active_reigns_stamps_archived_by_reset_on_an_undefended_reversion(dbm):
    """An undefended reversion has is_eom_champ = 0 but archived_by_reset should
    still be set, since it just marks that the reset wrote the row.
    """
    assert dbm.create_title("ArchivedByReset1", role_id=1500)
    title_id = dbm.get_title_by_name("ArchivedByReset1")['id']
    dbm.set_original_holder(title_id, 701)
    dbm.reset_all_active_reigns()  # creates the reversion reign, 0 defenses

    dbm.reset_all_active_reigns()  # archives it at the next month end

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 701
    assert archived['is_lineal_reversion'] == 1
    assert archived['is_eom_champ'] == 0
    assert archived['archived_by_reset'] == 1


def test_grant_title_leaves_archived_by_reset_unset(dbm, title_with_champion):
    """The other archive paths log a match, so archived_by_reset stays 0."""
    title_id, champion_id = title_with_champion

    dbm.grant_title(title_id, 222)  # archives champion_id's reign

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == champion_id
    assert archived['archived_by_reset'] == 0


def test_undo_match_refuses_when_reset_archives_an_undefended_reversion_above_the_watermark(dbm):
    """A match is logged, then the next reset archives an undefended reversion
    above the match's watermark. None of the older checks catch this row,
    so without archived_by_reset the undo would delete its only record.
    Undo should return "unlogged_change".
    """
    assert dbm.create_title("ArchivedByReset2", role_id=1501)
    title_id = dbm.get_title_by_name("ArchivedByReset2")['id']
    dbm.set_original_holder(title_id, 702)
    dbm.reset_all_active_reigns()  # creates the reversion reign for 702

    # A draw logged on the reversion reign and not undone yet.
    match_id = dbm.log_match("draw", title_id, actor_id=702)
    dbm.apply_combat_damage(title_id)

    # Next month end: the reversion archives with 0 defenses and no match log.
    dbm.reset_all_active_reigns()

    archived = _last_historical_row(dbm, title_id)
    assert archived['discord_user_id'] == 702
    assert archived['is_lineal_reversion'] == 1
    assert archived['is_eom_champ'] == 0
    assert archived['archived_by_reset'] == 1
    history_before = len(dbm.get_title_history(title_id))

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "unlogged_change"
    assert result['match_id'] == match_id
    assert result['title_id'] == title_id
    # Nothing got deleted and the match isn't marked undone.
    assert len(dbm.get_title_history(title_id)) == history_before
    assert dbm.get_match(match_id)['undone'] == 0


def test_undo_match_with_no_reset_archived_rows_above_the_watermark_still_succeeds(dbm, title_with_champion):
    """Make sure a normal undo still works."""
    title_id, champion_id = title_with_champion

    match_id = dbm.log_match("draw", title_id, actor_id=champion_id)
    dbm.apply_combat_damage(title_id)

    result = dbm.undo_match(match_id, admin_id=777)

    assert result['status'] == "ok"
    assert dbm.get_match(match_id)['undone'] == 1


# --- /hall_of_fame: most_claims skips reversions, most_defenses and
# longest_reign still count them ---


def test_get_global_records_most_claims_excludes_reversion_only_holder(dbm):
    """A lineal champion with only a reversion shouldn't beat a player with a real claim."""
    assert dbm.create_title("HofCrown1", role_id=1600)
    reversion_title_id = dbm.get_title_by_name("HofCrown1")['id']
    dbm.set_original_holder(reversion_title_id, 801)
    dbm.reset_all_active_reigns()  # 801's only reign is a reversion

    assert dbm.create_title("HofCrown2", role_id=1601)
    claimed_title_id = dbm.get_title_by_name("HofCrown2")['id']
    dbm.grant_title(claimed_title_id, 802)  # a real claim

    records = dbm.get_global_records()

    assert records["most_claims"]["discord_user_id"] == 802
    assert records["most_claims"]["total_claims"] == 1


def test_get_global_records_most_claims_ignores_many_reversions_against_few_real_claims(dbm):
    """A lineal champion with lots of reversions shouldn't hold the claims record."""
    assert dbm.create_title("HofCrown3", role_id=1602)
    t1 = dbm.get_title_by_name("HofCrown3")['id']
    dbm.set_original_holder(t1, 803)

    assert dbm.create_title("HofCrown4", role_id=1603)
    t2 = dbm.get_title_by_name("HofCrown4")['id']
    dbm.set_original_holder(t2, 803)

    # Six resets: both titles go back to 803 six times each, none are real claims.
    for _ in range(6):
        dbm.reset_all_active_reigns()

    # Another player with 4 real claims on a third title.
    assert dbm.create_title("HofCrown5", role_id=1604)
    t3 = dbm.get_title_by_name("HofCrown5")['id']
    for claimant in (901, 902, 903, 904):
        dbm.grant_title(t3, claimant)

    records = dbm.get_global_records()

    # 803 has 0 real claims, so they shouldn't be the claims leader.
    assert records["most_claims"]["discord_user_id"] != 803
    assert records["most_claims"]["total_claims"] == 1


def test_get_global_records_most_defenses_still_counts_a_reversion_reign(dbm):
    """Defenses on a reversion still count for the defenses record."""
    assert dbm.create_title("HofCrown6", role_id=1605)
    title_id = dbm.get_title_by_name("HofCrown6")['id']
    dbm.set_original_holder(title_id, 805)
    dbm.reset_all_active_reigns()  # 805's reign is a reversion, 0 defenses

    dbm.log_defense(title_id)
    dbm.log_defense(title_id)
    dbm.log_defense(title_id)

    records = dbm.get_global_records()

    assert records["most_defenses"]["discord_user_id"] == 805
    assert records["most_defenses"]["max_defenses"] == 3


def test_get_global_records_longest_reign_still_counts_a_reversion_reign(dbm):
    """Time held on a reversion still counts for longest reign."""
    assert dbm.create_title("HofCrown7", role_id=1606)
    title_id = dbm.get_title_by_name("HofCrown7")['id']
    dbm.set_original_holder(title_id, 806)
    dbm.reset_all_active_reigns()  # 806's reign is a reversion

    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
    _set_timestamp_acquired(dbm, title_id, long_ago)

    records = dbm.get_global_records()

    assert records["longest_reign"]["discord_user_id"] == 806
    assert records["longest_reign"]["duration_days"] > 399
