"""Tests for scripts/scrub_xeqgq.py.

That script fixes one bad match (XEQGQ) in the real database. These tests only
ever run against a temporary database made with DBManager.setup().
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import scrub_xeqgq  # noqa: E402  (has to come after the path insert)
from db_manager import DBManager  # noqa: E402


XEQGQ_USURPER = 201
XEQGQ_ABSENT_CHAMPION = 202
OTHER_PLAYER = 203


@pytest.fixture
def dbm(tmp_path):
    manager = DBManager(db_path=str(tmp_path / "test_slayer.db"))
    manager.setup()
    yield manager
    if manager._conn is not None:
        manager._conn.close()


def _insert_match_log(conn, match_id, match_type, title_id=1, actor_id=1, target_id=None):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO match_log (match_id, match_type, title_id, actor_id, target_id, snapshot)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (match_id, match_type, title_id, actor_id, target_id, json.dumps({})),
    )
    conn.commit()
    return cur.lastrowid


def _insert_participant(conn, match_log_id, user_id, result, rating_before=1500.0, rd_before=350.0, vol_before=0.06):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO match_participants
            (match_log_id, user_id, result, rating_before, rd_before, vol_before)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (match_log_id, user_id, result, rating_before, rd_before, vol_before),
    )
    conn.commit()


def _upsert_rating(conn, user_id, rating=1550.0, rd=300.0, vol=0.06):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO player_ratings (user_id, rating, rd, vol) VALUES (?, ?, ?, ?)",
        (user_id, rating, rd, vol),
    )
    conn.commit()


@pytest.fixture
def fabricated_db(dbm):
    """Makes a fake XEQGQ usurp match with the usurper and the absent champion,
    plus a second real match with the usurper and another player, so the
    NOT EXISTS check has something to protect. Returns the database path.
    """
    conn = dbm.get_connection()

    xeqgq_id = _insert_match_log(conn, "XEQGQ", "usurp", title_id=1, actor_id=XEQGQ_USURPER, target_id=XEQGQ_ABSENT_CHAMPION)
    _insert_participant(conn, xeqgq_id, XEQGQ_USURPER, "win")
    _insert_participant(conn, xeqgq_id, XEQGQ_ABSENT_CHAMPION, "loss")
    _upsert_rating(conn, XEQGQ_USURPER, rating=1600.0)
    _upsert_rating(conn, XEQGQ_ABSENT_CHAMPION, rating=1400.0)

    other_id = _insert_match_log(conn, "OTHER1", "reported", title_id=2, actor_id=XEQGQ_USURPER, target_id=OTHER_PLAYER)
    _insert_participant(conn, other_id, XEQGQ_USURPER, "win")
    _insert_participant(conn, other_id, OTHER_PLAYER, "loss")
    # The usurper already has a rating row, so I only add one for the other player.
    _upsert_rating(conn, OTHER_PLAYER, rating=1450.0)

    return dbm.db_path, xeqgq_id, other_id


def _counts(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM match_participants")
    mp = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM player_ratings")
    pr = cur.fetchone()[0]
    return mp, pr


# Commit mode

def test_commit_deletes_xeqgq_participants(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        result = scrub_xeqgq.scrub_xeqgq(conn, commit=True)
        assert result["ok"] is True
        assert result["committed"] is True

        cur = conn.cursor()
        cur.execute("SELECT * FROM match_participants WHERE match_log_id = ?", (xeqgq_id,))
        assert cur.fetchall() == []
    finally:
        conn.close()


def test_commit_deletes_rating_for_user_with_no_other_history(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        scrub_xeqgq.scrub_xeqgq(conn, commit=True)
        cur = conn.cursor()
        cur.execute("SELECT * FROM player_ratings WHERE user_id = ?", (XEQGQ_ABSENT_CHAMPION,))
        assert cur.fetchone() is None
    finally:
        conn.close()


def test_commit_keeps_rating_for_user_with_other_rated_match(fabricated_db):
    """The usurper also played in another match, so their rating row has to stay."""
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        scrub_xeqgq.scrub_xeqgq(conn, commit=True)
        cur = conn.cursor()
        cur.execute("SELECT rating FROM player_ratings WHERE user_id = ?", (XEQGQ_USURPER,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1600.0  # unchanged
    finally:
        conn.close()


def test_commit_leaves_match_log_row_intact(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        scrub_xeqgq.scrub_xeqgq(conn, commit=True)
        cur = conn.cursor()
        cur.execute("SELECT * FROM match_log WHERE match_id = 'XEQGQ'")
        row = cur.fetchone()
        assert row is not None
        cur.execute("SELECT undone FROM match_log WHERE match_id = 'XEQGQ'")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_commit_leaves_unrelated_match_completely_untouched(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        scrub_xeqgq.scrub_xeqgq(conn, commit=True)
        cur = conn.cursor()

        cur.execute("SELECT user_id, result FROM match_participants WHERE match_log_id = ? ORDER BY user_id", (other_id,))
        rows = [tuple(r) for r in cur.fetchall()]
        assert rows == [(XEQGQ_USURPER, "win"), (OTHER_PLAYER, "loss")]

        cur.execute("SELECT rating FROM player_ratings WHERE user_id = ?", (OTHER_PLAYER,))
        assert cur.fetchone()[0] == 1450.0
    finally:
        conn.close()


def test_commit_row_counts_reflect_only_xeqgq_deletions(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        before_mp, before_pr = _counts(conn)
        result = scrub_xeqgq.scrub_xeqgq(conn, commit=True)
        after_mp, after_pr = _counts(conn)

        assert before_mp == 4  # 2 XEQGQ + 2 from the other match
        assert after_mp == 2  # only the other match's 2 are left
        assert before_pr == 3  # usurper, absent champion, other player
        assert after_pr == 2  # only the absent champion's row gets deleted

        assert result["participants_before"] == before_mp
        assert result["participants_after"] == after_mp
        assert result["ratings_before"] == before_pr
        assert result["ratings_after"] == after_pr
    finally:
        conn.close()


# Dry run (the default) shouldn't change anything

def test_dry_run_is_the_default_and_changes_nothing(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        before_mp, before_pr = _counts(conn)
        result = scrub_xeqgq.scrub_xeqgq(conn)  # commit defaults to False
        after_mp, after_pr = _counts(conn)

        assert result["ok"] is True
        assert result["committed"] is False
        assert before_mp == after_mp == 4
        assert before_pr == after_pr == 3

        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM match_participants WHERE match_log_id = ?", (xeqgq_id,))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT COUNT(*) FROM player_ratings WHERE user_id = ?", (XEQGQ_ABSENT_CHAMPION,))
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_dry_run_reports_what_it_would_delete(fabricated_db):
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        result = scrub_xeqgq.scrub_xeqgq(conn, commit=False)
        assert sorted(result["user_ids"]) == [XEQGQ_USURPER, XEQGQ_ABSENT_CHAMPION]
        assert result["ratings_to_delete_user_ids"] == [XEQGQ_ABSENT_CHAMPION]
        assert result["ratings_to_keep_user_ids"] == [XEQGQ_USURPER]
    finally:
        conn.close()


def test_cli_default_has_no_commit_flag_and_writes_nothing(fabricated_db, capsys):
    db_path, xeqgq_id, other_id = fabricated_db
    exit_code = scrub_xeqgq.main([db_path])
    assert exit_code == 0

    conn = sqlite3.connect(db_path)
    try:
        mp, pr = _counts(conn)
        assert mp == 4
        assert pr == 3
    finally:
        conn.close()


# Abort checks

def test_abort_when_match_id_missing(dbm):
    conn = dbm.get_connection()
    before_mp, before_pr = _counts(conn)

    result = scrub_xeqgq.scrub_xeqgq(conn, commit=True)
    assert result["ok"] is False
    assert "XEQGQ" in result["reason"]

    after_mp, after_pr = _counts(conn)
    assert (before_mp, before_pr) == (after_mp, after_pr) == (0, 0)


def test_abort_when_match_type_is_not_usurp(dbm):
    conn = dbm.get_connection()
    xeqgq_id = _insert_match_log(conn, "XEQGQ", "reported", actor_id=XEQGQ_USURPER, target_id=XEQGQ_ABSENT_CHAMPION)
    _insert_participant(conn, xeqgq_id, XEQGQ_USURPER, "win")
    _insert_participant(conn, xeqgq_id, XEQGQ_ABSENT_CHAMPION, "loss")
    before_mp, before_pr = _counts(conn)

    result = scrub_xeqgq.scrub_xeqgq(conn, commit=True)
    assert result["ok"] is False
    assert "reported" in result["reason"]

    after_mp, after_pr = _counts(conn)
    assert before_mp == after_mp
    assert before_pr == after_pr
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM match_participants WHERE match_log_id = ?", (xeqgq_id,))
    assert cur.fetchone()[0] == 2


def test_abort_when_participant_count_is_not_two(dbm):
    conn = dbm.get_connection()
    xeqgq_id = _insert_match_log(conn, "XEQGQ", "usurp", actor_id=XEQGQ_USURPER, target_id=XEQGQ_ABSENT_CHAMPION)
    _insert_participant(conn, xeqgq_id, XEQGQ_USURPER, "win")
    _insert_participant(conn, xeqgq_id, XEQGQ_ABSENT_CHAMPION, "loss")
    _insert_participant(conn, xeqgq_id, OTHER_PLAYER, "loss")
    before_mp, before_pr = _counts(conn)

    result = scrub_xeqgq.scrub_xeqgq(conn, commit=True)
    assert result["ok"] is False
    assert "3" in result["reason"]

    after_mp, after_pr = _counts(conn)
    assert before_mp == after_mp
    assert before_pr == after_pr
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM match_participants WHERE match_log_id = ?", (xeqgq_id,))
    assert cur.fetchone()[0] == 3


def test_cli_exit_code_nonzero_on_guard_failure(dbm):
    exit_code = scrub_xeqgq.main([dbm.db_path, "--commit"])
    assert exit_code == 1


# Don't create a database file if the path doesn't exist

def test_cli_aborts_and_creates_nothing_when_db_path_does_not_exist(tmp_path, capsys):
    missing_path = str(tmp_path / "does_not_exist.db")
    assert not os.path.exists(missing_path)

    exit_code = scrub_xeqgq.main([missing_path])

    assert exit_code == 1
    assert not os.path.exists(missing_path), (
        "sqlite3.connect() must never have been called against this path -- "
        "it would have silently created a stray zero-byte database file"
    )
    captured = capsys.readouterr()
    assert "ABORTED" in captured.out
    assert "Nothing was changed" in captured.out


def test_cli_aborts_on_missing_db_path_even_with_commit_flag(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.db")
    exit_code = scrub_xeqgq.main([missing_path, "--commit"])
    assert exit_code == 1
    assert not os.path.exists(missing_path)


# A partial ratings cleanup has to be flagged, not reported as a success

def test_ratings_cleanup_incomplete_flag_set_when_a_participant_has_other_history(fabricated_db):
    """The usurper has another match, so only one of the two rating rows can be
    deleted. That should get flagged as incomplete.
    """
    db_path, xeqgq_id, other_id = fabricated_db
    conn = sqlite3.connect(db_path)
    try:
        result = scrub_xeqgq.scrub_xeqgq(conn, commit=False)
        assert result["ratings_cleanup_incomplete"] is True
    finally:
        conn.close()


def test_ratings_cleanup_incomplete_warns_loudly_and_fails_exit_code_on_commit(fabricated_db, capsys):
    """A partial cleanup still removes the bad match_participants rows, but
    the exit code shouldn't be 0.
    """
    db_path, xeqgq_id, other_id = fabricated_db

    exit_code = scrub_xeqgq.main([db_path, "--commit"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "INCOMPLETE" in captured.out
    assert "compounded" in captured.out

    # The match_participants rows should still be gone.
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM match_participants WHERE match_log_id = ?", (xeqgq_id,))
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_ratings_cleanup_complete_when_neither_participant_has_other_history(dbm, capsys):
    """Both players only ever played the bad match, so both rows get deleted,
    no warning, and exit code 0.
    """
    conn = dbm.get_connection()
    xeqgq_id = _insert_match_log(conn, "XEQGQ", "usurp", actor_id=XEQGQ_USURPER, target_id=XEQGQ_ABSENT_CHAMPION)
    _insert_participant(conn, xeqgq_id, XEQGQ_USURPER, "win")
    _insert_participant(conn, xeqgq_id, XEQGQ_ABSENT_CHAMPION, "loss")
    _upsert_rating(conn, XEQGQ_USURPER, rating=1600.0)
    _upsert_rating(conn, XEQGQ_ABSENT_CHAMPION, rating=1400.0)

    exit_code = scrub_xeqgq.main([dbm.db_path, "--commit"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "WARNING" not in captured.out
    assert "INCOMPLETE" not in captured.out
