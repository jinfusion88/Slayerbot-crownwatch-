import sqlite3
import json
import secrets
import logging
from typing import Iterable, Optional
import datetime
from zoneinfo import ZoneInfo

from rating_engine import (
    DEFAULT_RATING,
    DEFAULT_RD,
    DEFAULT_VOL,
    rate_period,
    build_pod_results,
    build_draw_results,
)

logger = logging.getLogger('discord')

# Usurper settings
USURP_WARNING_WINS = 3  # warn the champion at this many wins
USURP_WINS_TO_OVERTHROW = 4  # overthrow at this many wins

# Life settings
DEFAULT_STARTING_LIFE = 50
BOUNTY_STARTING_LIFE = 60
DRAW_DAMAGE = 20  # life the champion loses on a draw

MATCH_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # left out O/0/I/1 so ids are easy to read
MATCH_ID_LENGTH = 5
MATCH_TYPES = ("claim", "defense", "draw", "usurp")
MATCH_RESULTS = ("win", "loss", "draw")

# RD at or above this means the rating is still provisional.
PROVISIONAL_RD = 100.0


def generate_match_id() -> str:
    """Random match id using secrets."""
    return "".join(secrets.choice(MATCH_ID_ALPHABET) for _ in range(MATCH_ID_LENGTH))


def upkeep_blocks_elapsed(timestamp: Optional[str]) -> int:
    """How many 12 hour upkeep blocks have passed since timestamp.

    /undo_match uses this to warn that undoing a match also gives back any
    life decay that happened since. Returns 0 if the timestamp is bad or in the future.
    """
    if not timestamp:
        return 0
    try:
        logged_at = datetime.datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return 0
    elapsed_hours = (datetime.datetime.now(datetime.timezone.utc) - logged_at).total_seconds() / 3600.0
    if elapsed_hours < 0:
        return 0
    return int(elapsed_hours // 12)


def build_contender_string(selected_ids: Iterable[int], author_id: int) -> str:
    """Builds the comma separated list of players allowed into sudden death after a draw.
    Removes duplicates and keeps the order.
    """
    ordered_ids = []
    seen = set()
    for user_id in list(selected_ids) + [author_id]:
        if user_id not in seen:
            seen.add(user_id)
            ordered_ids.append(user_id)
    return ",".join(str(user_id) for user_id in ordered_ids)


def pod_rating_losers(pod_ids, winner_id) -> list:
    """Everyone in the pod except the winner, with no duplicates."""
    if not pod_ids:
        return []

    losers = []
    seen = set()
    for uid in pod_ids:
        if uid and uid != winner_id and uid not in seen:
            seen.add(uid)
            losers.append(uid)
    return losers


def classify_pod_claim(champion_id, pod_ids, actor_id) -> str:
    """Figures out what kind of claim this is: "vacant", "defense", "direct", or "usurp".
    The checks run in that order.
    """
    if not champion_id:
        return "vacant"

    if actor_id == champion_id:
        return "defense"

    if pod_ids and champion_id in pod_ids:
        return "direct"

    return "usurp"


def classify_pod_draw(champion_id, pod_ids) -> str:
    """Same idea as classify_pod_claim but for draws: "vacant", "contested", or "absent".
    If the champion wasn't at the table the draw still gets rated, but it doesn't hurt their title.
    """
    if not champion_id:
        return "vacant"

    if pod_ids and champion_id in pod_ids:
        return "contested"

    # Champion wasn't there, so the draw is rated but doesn't touch the title.
    return "absent"


def build_draw_pod_ids(selected_ids, holder_id, champion_in_pod: bool = True) -> list:
    """The list of players a draw gets rated with.

    If the champion was at the table I add them back in so they can still play
    for their own title in sudden death. If they weren't there I leave them out,
    otherwise they'd get rated for a game they didn't play.
    """
    ordered = list(selected_ids)
    if champion_in_pod and holder_id:
        ordered.append(holder_id)
    return ordered


def promote_draw_to_contested(champion_in_pod: bool, holder_id, selected_ids) -> bool:
    """Checks again if the champion was in the pod, but it can only go from
    absent to contested, never the other way.

    The draw picker lets people fix the pod list, so if someone adds the champion
    back in, the draw should count as contested and the champion takes damage.
    """
    # Already contested, so leave it alone.
    if champion_in_pod:
        return True

    if not holder_id:
        return False

    return holder_id in (selected_ids or [])


def format_rating_deltas(participants, is_draw: bool = False) -> str:
    """Builds the rating change line for a finished match, like:

        🏆 Winner: <@1> (+14) | 💀 Defeated: <@2> (-4), <@3> (-5), <@4> (-5)

    Returns "" if there's nothing to show. This runs after the title change is
    already saved, so it can never raise. I put it here instead of main.py so I can test it.
    """
    if not participants:
        return ""

    def render(entry) -> Optional[str]:
        """One player as "<@id> (+n)", or None if it can't be rendered."""
        try:
            user_id = entry["user_id"]
            # Round first so something like -0.4 shows as +0 and not -0.
            delta = round(float(entry["delta"]))
        except Exception:
            # Catching everything here since round(float('inf')) raises OverflowError.
            return None
        if not user_id:
            return None
        if delta == 0:
            delta = 0
        return f"<@{user_id}> ({delta:+d})"

    # The loop itself is in the try too, in case participants isn't a list.
    try:
        if is_draw:
            rendered = [line for line in (render(entry) for entry in participants) if line]
            if not rendered:
                return ""
            return "🤝 Draw: " + ", ".join(rendered)

        winner_line = ""
        loser_lines = []
        for entry in participants:
            line = render(entry)
            if not line:
                continue
            try:
                result = entry["result"]
            except Exception:
                result = None
            # The first win entry is the winner, everyone else lost.
            if result == "win" and not winner_line:
                winner_line = line
            else:
                loser_lines.append(line)
    except Exception:
        return ""

    halves = []
    if winner_line:
        halves.append(f"🏆 Winner: {winner_line}")
    if loser_lines:
        halves.append("💀 Defeated: " + ", ".join(loser_lines))
    return " | ".join(halves)


class DBManager:
    def __init__(self, db_path='slayer_bot.db'):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def get_connection(self):
        """Returns the shared SQLite connection and opens it the first time.
        The bot runs on one thread so one connection is fine.
        """
        if self._conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def setup(self):
        """Creates the tables if they don't exist yet."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Titles
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS titles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        discord_role_id INTEGER
                    )
                ''')
                
                # Active reigns
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS active_reigns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title_id INTEGER NOT NULL UNIQUE,
                        discord_user_id INTEGER NOT NULL,
                        timestamp_acquired DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_defended_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        defenses_count INTEGER DEFAULT 0,
                        is_lineal_reversion INTEGER DEFAULT 0,
                        FOREIGN KEY (title_id) REFERENCES titles (id)
                    )
                ''')

                # Historical stats
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS historical_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title_id INTEGER NOT NULL,
                        discord_user_id INTEGER NOT NULL,
                        timestamp_acquired DATETIME NOT NULL,
                        timestamp_lost DATETIME DEFAULT CURRENT_TIMESTAMP,
                        total_defenses INTEGER DEFAULT 0,
                        is_lineal_reversion INTEGER DEFAULT 0,
                        archived_by_reset INTEGER DEFAULT 0,
                        FOREIGN KEY (title_id) REFERENCES titles (id)
                    )
                ''')
                
                # Guild configs
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS guild_configs (
                        guild_id INTEGER PRIMARY KEY,
                        announcement_channel_id INTEGER NOT NULL
                    )
                ''')
                
                # Admin roles
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS admin_roles (
                        guild_id INTEGER,
                        role_id INTEGER,
                        PRIMARY KEY (guild_id, role_id)
                    )
                ''')

                # Rivalries (for /nemesis)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS rivalries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        winner_id INTEGER NOT NULL,
                        loser_id INTEGER NOT NULL,
                        wins INTEGER DEFAULT 1,
                        UNIQUE(winner_id, loser_id)
                    )
                ''')

                # Contenders (usurper win counts)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS contenders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title_id INTEGER NOT NULL,
                        discord_user_id INTEGER NOT NULL,
                        wins INTEGER DEFAULT 0,
                        UNIQUE(title_id, discord_user_id),
                        FOREIGN KEY (title_id) REFERENCES titles (id)
                    )
                ''')

                # Match log, with a snapshot of the state before the match so it can be undone
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS match_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_id TEXT NOT NULL UNIQUE,
                        match_type TEXT NOT NULL,
                        title_id INTEGER NOT NULL,
                        actor_id INTEGER NOT NULL,
                        target_id INTEGER,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        snapshot TEXT NOT NULL,
                        undone INTEGER DEFAULT 0,
                        undone_by INTEGER,
                        undone_at DATETIME,
                        FOREIGN KEY (title_id) REFERENCES titles (id)
                    )
                ''')

                # Player ratings, one row per rated player. Players who never played
                # don't get a row, they just use the defaults.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS player_ratings (
                        user_id INTEGER PRIMARY KEY,
                        rating REAL NOT NULL,
                        rd REAL NOT NULL,
                        vol REAL NOT NULL,
                        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Match participants. The rating_before columns can be NULL, which means the
                # player had no rating before this match, so undo deletes their row instead
                # of writing default values back.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS match_participants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        match_log_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        result TEXT NOT NULL,
                        commander_name TEXT,
                        color_identity TEXT,
                        deck_url TEXT,
                        rating_before REAL,
                        rd_before REAL,
                        vol_before REAL,
                        UNIQUE(match_log_id, user_id),
                        FOREIGN KEY (match_log_id) REFERENCES match_log (id)
                    )
                ''')

                # Adds new columns to older databases
                migrations = [
                    "ALTER TABLE active_reigns ADD COLUMN defenses_count INTEGER DEFAULT 0",
                    # Old databases keep the old default of 40, ALTER TABLE can't change that.
                    # grant_title sets the real starting life anyway.
                    f"ALTER TABLE active_reigns ADD COLUMN current_life INTEGER DEFAULT {DEFAULT_STARTING_LIFE}",
                    "ALTER TABLE active_reigns ADD COLUMN last_upkeep_timestamp DATETIME",
                    "ALTER TABLE historical_stats ADD COLUMN is_eom_champ BOOLEAN DEFAULT 0",
                    "ALTER TABLE titles ADD COLUMN original_holder_id INTEGER DEFAULT NULL",
                    "ALTER TABLE titles ADD COLUMN bounty_active BOOLEAN DEFAULT 0",
                    "ALTER TABLE titles ADD COLUMN sudden_death_contenders TEXT DEFAULT NULL",
                    "ALTER TABLE active_reigns ADD COLUMN defense_streak INTEGER DEFAULT 0",
                    "ALTER TABLE active_reigns ADD COLUMN decklist TEXT DEFAULT NULL",
                    "ALTER TABLE historical_stats ADD COLUMN decklist TEXT DEFAULT NULL",
                    # The actor's deck info for the match. Nothing reads these right now,
                    # the stats use match_participants instead.
                    "ALTER TABLE match_log ADD COLUMN commander_name TEXT DEFAULT NULL",
                    "ALTER TABLE match_log ADD COLUMN color_identity TEXT DEFAULT NULL",
                    "ALTER TABLE match_log ADD COLUMN deck_url TEXT DEFAULT NULL",
                    # Marks reigns the monthly reset gave back to the original holder, so they don't count as claims.
                    "ALTER TABLE active_reigns ADD COLUMN is_lineal_reversion INTEGER DEFAULT 0",
                    "ALTER TABLE historical_stats ADD COLUMN is_lineal_reversion INTEGER DEFAULT 0",
                    # Set on every row the monthly reset archives, so undo_match can tell
                    # the reset touched this title.
                    "ALTER TABLE historical_stats ADD COLUMN archived_by_reset INTEGER DEFAULT 0",
                ]
                
                for migration in migrations:
                    try:
                        cursor.execute(migration)
                    except sqlite3.OperationalError:
                        pass  # column already exists
                        
                cursor.execute("UPDATE active_reigns SET last_upkeep_timestamp = CURRENT_TIMESTAMP WHERE last_upkeep_timestamp IS NULL")
                
                conn.commit()
                logger.info("Database schema initialized/verified successfully.")
        except sqlite3.Error:
            logger.exception("Error setting up database")
            raise

    def process_life_upkeeps(self):
        vacated_titles = []
        critical_titles = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id, title_id, current_life, last_upkeep_timestamp FROM active_reigns')
            reigns = cursor.fetchall()
            
            now = datetime.datetime.now(datetime.timezone.utc)
            
            for reign in reigns:
                try:
                    last_upkeep = datetime.datetime.strptime(reign['last_upkeep_timestamp'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
                except ValueError:
                    continue
                
                diff_hours = (now - last_upkeep).total_seconds() / 3600.0
                blocks_passed = int(diff_hours // 12)
                
                if blocks_passed > 0:
                    life_loss = blocks_passed * 5
                    new_life = reign['current_life'] - life_loss
                    
                    new_upkeep_time = last_upkeep + datetime.timedelta(hours=blocks_passed * 12)
                    new_upkeep_str = new_upkeep_time.strftime('%Y-%m-%d %H:%M:%S')
                    
                    if new_life <= 0:
                        vacated_titles.append(reign['title_id'])
                    else:
                        if new_life == 10:
                            critical_titles.append(reign['title_id'])
                            
                        cursor.execute('''
                            UPDATE active_reigns
                            SET current_life = ?, last_upkeep_timestamp = ?
                            WHERE id = ?
                        ''', (new_life, new_upkeep_str, reign['id']))
                        
            conn.commit()
            return {"vacated": vacated_titles, "critical": critical_titles}

    def apply_combat_damage(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE active_reigns
                SET current_life = current_life - ?
                WHERE title_id = ?
            ''', (DRAW_DAMAGE, title_id))
            
            cursor.execute('SELECT current_life FROM active_reigns WHERE title_id = ?', (title_id,))
            result = cursor.fetchone()
            conn.commit()
            return result['current_life'] if result else 0

    def lifelink_reset(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE active_reigns
                SET current_life = ?, last_upkeep_timestamp = CURRENT_TIMESTAMP
                WHERE title_id = ?
            ''', (DEFAULT_STARTING_LIFE, title_id))
            conn.commit()

    def vacate_title(self, title_id: int, is_bounty: bool = False, contenders_list: str = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM active_reigns WHERE title_id = ?', (title_id,))
            reign = cursor.fetchone()

            if reign:
                keys = reign.keys()
                defenses = reign['defenses_count'] if 'defenses_count' in keys else 0
                decklist = reign['decklist'] if 'decklist' in keys else None
                is_lineal_reversion = reign['is_lineal_reversion'] if 'is_lineal_reversion' in keys else 0
                cursor.execute('''
                    INSERT INTO historical_stats
                    (title_id, discord_user_id, timestamp_acquired, timestamp_lost, total_defenses, decklist, is_lineal_reversion)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                ''', (reign['title_id'], reign['discord_user_id'], reign['timestamp_acquired'], defenses, decklist, is_lineal_reversion))

                cursor.execute('DELETE FROM active_reigns WHERE title_id = ?', (title_id,))

            cursor.execute('DELETE FROM contenders WHERE title_id = ?', (title_id,))

            cursor.execute('''
                UPDATE titles
                SET bounty_active = ?, sudden_death_contenders = ?
                WHERE id = ?
            ''', (1 if is_bounty else 0, contenders_list, title_id))

            conn.commit()

    def clear_bounty_and_sudden_death(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE titles SET bounty_active = 0, sudden_death_contenders = NULL WHERE id = ?', (title_id,))
            conn.commit()

    def get_past_champions(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT hs.discord_user_id, t.name as title_name, hs.timestamp_acquired, hs.timestamp_lost, hs.total_defenses
                FROM historical_stats hs
                JOIN titles t ON hs.title_id = t.id
                WHERE hs.is_eom_champ = 1
                ORDER BY hs.timestamp_lost DESC
            ''')
            return cursor.fetchall()

    def get_monthly_iron_man(self):
        now = datetime.datetime.now(ZoneInfo("America/Chicago"))
        start_of_month_str = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT discord_user_id,
                       COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) as claims_this_month,
                       SUM(defenses) as defenses_this_month
                FROM (
                    SELECT discord_user_id, id, timestamp_acquired, defenses_count as defenses, is_lineal_reversion
                    FROM active_reigns
                    UNION ALL
                    SELECT discord_user_id, id, timestamp_acquired, total_defenses as defenses, is_lineal_reversion
                    FROM historical_stats
                )
                WHERE timestamp_acquired >= ?
                GROUP BY discord_user_id
                HAVING (COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) + SUM(defenses)) > 0
                ORDER BY (COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) + SUM(defenses)) DESC
                LIMIT 1
            ''', (start_of_month_str,))
            return cursor.fetchone()

    def get_ironman_standings(self, limit: int = 3):
        """Top users by claims + defenses this month (UTC), for /ironman_leaderboard.

        Note: defenses are counted for reigns claimed this month, not defenses
        that happened this month, since I don't store a timestamp per defense.
        Lineal reversions don't count as claims.
        """
        start_of_month_str = datetime.datetime.now(datetime.timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).strftime('%Y-%m-%d %H:%M:%S')

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT discord_user_id,
                       COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) as claims_this_month,
                       COALESCE(SUM(defenses), 0) as defenses_this_month,
                       COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) + COALESCE(SUM(defenses), 0) as total_score
                FROM (
                    SELECT discord_user_id, id, timestamp_acquired, defenses_count as defenses, is_lineal_reversion
                    FROM active_reigns
                    UNION ALL
                    SELECT discord_user_id, id, timestamp_acquired, total_defenses as defenses, is_lineal_reversion
                    FROM historical_stats
                )
                WHERE timestamp_acquired >= ?
                GROUP BY discord_user_id
                HAVING total_score > 0
                ORDER BY total_score DESC, discord_user_id ASC
                LIMIT ?
            ''', (start_of_month_str, limit))
            return cursor.fetchall()

    def create_title(self, name: str, role_id: int) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('INSERT INTO titles (name, discord_role_id) VALUES (?, ?)', (name, role_id))
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False
            
    def get_titles(self, search_query: str = "", limit: int = 25):
        """Returns up to limit (id, name) title rows, optionally filtered by search.
        Default is 25 because that's Discord's max for autocomplete choices.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if search_query:
                cursor.execute('SELECT id, name FROM titles WHERE name LIKE ? LIMIT ?', (f'%{search_query}%', limit))
            else:
                cursor.execute('SELECT id, name FROM titles LIMIT ?', (limit,))
            return cursor.fetchall()
            
    def get_title_by_name(self, name: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM titles WHERE name = ?', (name,))
            return cursor.fetchone()
            
    def get_title_by_id(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM titles WHERE id = ?', (title_id,))
            return cursor.fetchone()
            
    def grant_title(self, title_id: int, new_user_id: int, decklist: Optional[str] = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('SELECT bounty_active FROM titles WHERE id = ?', (title_id,))
            title_info = cursor.fetchone()
            starting_life = BOUNTY_STARTING_LIFE if (title_info and title_info['bounty_active']) else DEFAULT_STARTING_LIFE

            cursor.execute('UPDATE titles SET bounty_active = 0, sudden_death_contenders = NULL WHERE id = ?', (title_id,))

            cursor.execute('SELECT * FROM active_reigns WHERE title_id = ?', (title_id,))
            current_reign = cursor.fetchone()

            if current_reign:
                keys = current_reign.keys()
                defenses = current_reign['defenses_count'] if 'defenses_count' in keys else 0
                old_decklist = current_reign['decklist'] if 'decklist' in keys else None
                is_lineal_reversion = current_reign['is_lineal_reversion'] if 'is_lineal_reversion' in keys else 0
                cursor.execute('''
                    INSERT INTO historical_stats
                    (title_id, discord_user_id, timestamp_acquired, timestamp_lost, total_defenses, decklist, is_lineal_reversion)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                ''', (
                    current_reign['title_id'],
                    current_reign['discord_user_id'],
                    current_reign['timestamp_acquired'],
                    defenses,
                    old_decklist,
                    is_lineal_reversion
                ))
                cursor.execute('DELETE FROM active_reigns WHERE id = ?', (current_reign['id'],))

            cursor.execute('''
                INSERT INTO active_reigns (title_id, discord_user_id, current_life, last_upkeep_timestamp, decklist)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
            ''', (title_id, new_user_id, starting_life, decklist))

            conn.commit()
            if current_reign:
                return dict(current_reign)
            return None

    def get_active_reign_for_title(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM active_reigns WHERE title_id = ?', (title_id,))
            return cursor.fetchone()

    def log_defense(self, title_id: int):
        """Logs a defense and updates the streak. Every 3rd defense in a row
        adds 5 life (capped at 60). Call this after lifelink_reset.
        Returns a dict with the streak, whether the bonus hit, and current life,
        or None if there's no active reign.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE active_reigns
                SET defenses_count = defenses_count + 1,
                    defense_streak = defense_streak + 1,
                    last_defended_timestamp = CURRENT_TIMESTAMP
                WHERE title_id = ?
            ''', (title_id,))

            if cursor.rowcount == 0:
                conn.commit()
                return None

            cursor.execute('SELECT defense_streak, current_life FROM active_reigns WHERE title_id = ?', (title_id,))
            row = cursor.fetchone()
            streak = row['defense_streak']
            current_life = row['current_life']
            bonus_applied = False

            if streak % 3 == 0:
                bonus_applied = True
                # The cap can't actually be reached right now since life gets reset to 50
                # first, but I left it in case that changes.
                current_life = min(current_life + 5, 60)
                cursor.execute('UPDATE active_reigns SET current_life = ? WHERE title_id = ?', (current_life, title_id))

            conn.commit()
            return {"defense_streak": streak, "bonus_applied": bonus_applied, "current_life": current_life}

    def set_announcement_channel(self, guild_id: int, channel_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO guild_configs (guild_id, announcement_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET announcement_channel_id=excluded.announcement_channel_id
            ''', (guild_id, channel_id))
            conn.commit()
            
    def get_announcement_channels(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT guild_id, announcement_channel_id FROM guild_configs')
            return cursor.fetchall()
            
    def get_all_active_reigns_with_roles(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT ar.id, ar.discord_user_id, t.id as title_id, ar.last_defended_timestamp,
                       t.name as title_name, t.discord_role_id, ar.current_life
                FROM titles t
                LEFT JOIN active_reigns ar ON t.id = ar.title_id
            ''')
            return cursor.fetchall()

    def decay_title(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM active_reigns WHERE title_id = ?', (title_id,))
            reign = cursor.fetchone()
            if reign:
                keys = reign.keys()
                defenses = reign['defenses_count'] if 'defenses_count' in keys else 0
                decklist = reign['decklist'] if 'decklist' in keys else None
                is_lineal_reversion = reign['is_lineal_reversion'] if 'is_lineal_reversion' in keys else 0
                cursor.execute('''
                    INSERT INTO historical_stats
                    (title_id, discord_user_id, timestamp_acquired, timestamp_lost, total_defenses, decklist, is_lineal_reversion)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                ''', (reign['title_id'], reign['discord_user_id'], reign['timestamp_acquired'], defenses, decklist, is_lineal_reversion))
                cursor.execute('DELETE FROM active_reigns WHERE title_id = ?', (title_id,))
                conn.commit()
                return True
            return False

    def get_current_holders(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT t.id as title_id, ar.discord_user_id, t.name as title_name, ar.timestamp_acquired, ar.current_life, ar.defense_streak, t.bounty_active, t.sudden_death_contenders
                FROM titles t
                LEFT JOIN active_reigns ar ON t.id = ar.title_id
                ORDER BY t.name ASC
            ''')
            return cursor.fetchall()
            
    def get_user_stats(self, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) as claims, MAX(defenses_count) as max_def,
                MAX(julianday(CURRENT_TIMESTAMP) - julianday(timestamp_acquired)) as max_days
                FROM active_reigns WHERE discord_user_id = ?
            ''', (user_id,))
            active = cursor.fetchone()

            cursor.execute('''
                SELECT COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) as claims, MAX(total_defenses) as max_def,
                MAX(julianday(timestamp_lost) - julianday(timestamp_acquired)) as max_days
                FROM historical_stats WHERE discord_user_id = ?
            ''', (user_id,))
            historical = cursor.fetchone()
            
            total_claims = (active['claims'] if active['claims'] else 0) + (historical['claims'] if historical['claims'] else 0)
            
            active_def = active['max_def'] if active['max_def'] else 0
            hist_def = historical['max_def'] if historical['max_def'] else 0
            max_defenses = max(active_def, hist_def)
            
            active_days = active['max_days'] if active['max_days'] else 0
            hist_days = historical['max_days'] if historical['max_days'] else 0
            longest_reign_days = max(active_days, hist_days)
            
            return {
                "total_claims": total_claims,
                "max_defenses": max_defenses,
                "longest_reign_days": round(longest_reign_days, 1) if longest_reign_days else 0.0
            }

    def get_global_records(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Lineal reversions don't count as claims, but I still count their defenses and reign time below.
            cursor.execute('''
                SELECT discord_user_id, COUNT(CASE WHEN is_lineal_reversion = 0 THEN id END) as total_claims
                FROM (
                    SELECT discord_user_id, id, is_lineal_reversion FROM active_reigns
                    UNION ALL
                    SELECT discord_user_id, id, is_lineal_reversion FROM historical_stats
                )
                GROUP BY discord_user_id
                ORDER BY total_claims DESC
                LIMIT 1
            ''')
            most_claims = cursor.fetchone()

            cursor.execute('''
                SELECT discord_user_id, defenses as max_defenses
                FROM (
                    SELECT discord_user_id, defenses_count as defenses FROM active_reigns
                    UNION ALL
                    SELECT discord_user_id, total_defenses as defenses FROM historical_stats
                )
                ORDER BY defenses DESC
                LIMIT 1
            ''')
            most_defenses = cursor.fetchone()
            
            cursor.execute('''
                SELECT discord_user_id, duration_days
                FROM (
                    SELECT discord_user_id, julianday(CURRENT_TIMESTAMP) - julianday(timestamp_acquired) as duration_days FROM active_reigns
                    UNION ALL
                    SELECT discord_user_id, julianday(timestamp_lost) - julianday(timestamp_acquired) as duration_days FROM historical_stats
                )
                ORDER BY duration_days DESC
                LIMIT 1
            ''')
            longest_reign = cursor.fetchone()
            
            return {
                "most_claims": dict(most_claims) if most_claims else None,
                "most_defenses": dict(most_defenses) if most_defenses else None,
                "longest_reign": dict(longest_reign) if longest_reign else None
            }

    def reset_all_active_reigns(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute('SELECT * FROM active_reigns')
            active_reigns = cursor.fetchall()

            for reign in active_reigns:
                keys = reign.keys()
                defenses = reign['defenses_count'] if 'defenses_count' in keys else 0
                decklist = reign['decklist'] if 'decklist' in keys else None
                is_lineal_reversion = reign['is_lineal_reversion'] if 'is_lineal_reversion' in keys else 0

                # An automatic reversion only counts as an end of month title if
                # the player actually defended it at least once.
                is_eom_champ = 0 if (is_lineal_reversion and defenses == 0) else 1

                cursor.execute('''
                    INSERT INTO historical_stats
                    (title_id, discord_user_id, timestamp_acquired, timestamp_lost, total_defenses, is_eom_champ, decklist, is_lineal_reversion, archived_by_reset)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, 1)
                ''', (
                    reign['title_id'],
                    reign['discord_user_id'],
                    reign['timestamp_acquired'],
                    defenses,
                    is_eom_champ,
                    decklist,
                    is_lineal_reversion
                ))

            cursor.execute('DELETE FROM active_reigns')
            cursor.execute('DELETE FROM contenders')

            cursor.execute('UPDATE titles SET bounty_active = 0, sudden_death_contenders = NULL')
            
            cursor.execute('SELECT id, original_holder_id FROM titles WHERE original_holder_id IS NOT NULL')
            lineal_champions = cursor.fetchall()
            for champ in lineal_champions:
                cursor.execute('''
                    INSERT INTO active_reigns (title_id, discord_user_id, defenses_count, current_life, last_upkeep_timestamp, is_lineal_reversion)
                    VALUES (?, ?, 0, ?, CURRENT_TIMESTAMP, 1)
                ''', (champ['id'], champ['original_holder_id'], DEFAULT_STARTING_LIFE))
                
            conn.commit()
            return active_reigns

    def add_admin_role(self, guild_id: int, role_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR IGNORE INTO admin_roles (guild_id, role_id) VALUES (?, ?)', (guild_id, role_id))
            conn.commit()
            
    def remove_admin_role(self, guild_id: int, role_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM admin_roles WHERE guild_id = ? AND role_id = ?', (guild_id, role_id))
            conn.commit()
            return cursor.rowcount > 0
            
    def get_admin_roles(self, guild_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT role_id FROM admin_roles WHERE guild_id = ?', (guild_id,))
            return [row['role_id'] for row in cursor.fetchall()]

    def set_original_holder(self, title_id: int, user_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE titles SET original_holder_id = ? WHERE id = ?', (user_id, title_id))
            conn.commit()
            return cursor.rowcount > 0

    def delete_title(self, title_id: int):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM active_reigns WHERE title_id = ?', (title_id,))
            cursor.execute('DELETE FROM historical_stats WHERE title_id = ?', (title_id,))
            cursor.execute('DELETE FROM contenders WHERE title_id = ?', (title_id,))
            # Delete the match history for this title too, or /undo_match could bring back
            # a reign for a title that doesn't exist. Participants first since they point
            # at match_log.
            cursor.execute('''
                DELETE FROM match_participants
                 WHERE match_log_id IN (SELECT id FROM match_log WHERE title_id = ?)
            ''', (title_id,))
            cursor.execute('DELETE FROM match_log WHERE title_id = ?', (title_id,))
            # Delete the title last so rowcount tells me if it existed.
            cursor.execute('DELETE FROM titles WHERE id = ?', (title_id,))
            conn.commit()
            return cursor.rowcount > 0

    def edit_title(self, title_id: int, new_name: Optional[str] = None, new_role_id: Optional[int] = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if new_name is not None and new_role_id is not None:
                cursor.execute('UPDATE titles SET name = ?, discord_role_id = ? WHERE id = ?', (new_name, new_role_id, title_id))
            elif new_name is not None:
                cursor.execute('UPDATE titles SET name = ? WHERE id = ?', (new_name, title_id))
            elif new_role_id is not None:
                cursor.execute('UPDATE titles SET discord_role_id = ? WHERE id = ?', (new_role_id, title_id))
            conn.commit()
            return cursor.rowcount > 0

    def set_reign_stats(self, title_id: int, new_life: Optional[int] = None, new_defenses: Optional[int] = None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if new_life is not None and new_defenses is not None:
                cursor.execute('UPDATE active_reigns SET current_life = ?, defenses_count = ? WHERE title_id = ?', (new_life, new_defenses, title_id))
            elif new_life is not None:
                cursor.execute('UPDATE active_reigns SET current_life = ? WHERE title_id = ?', (new_life, title_id))
            elif new_defenses is not None:
                cursor.execute('UPDATE active_reigns SET defenses_count = ? WHERE title_id = ?', (new_defenses, title_id))
            conn.commit()
            return cursor.rowcount > 0

    def set_bounty(self, title_id: int, status: bool):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE titles SET bounty_active = ? WHERE id = ?', (1 if status else 0, title_id))
            conn.commit()
            return cursor.rowcount > 0

    def record_rivalry_win(self, winner_id: int, loser_id: int):
        """Adds a win for winner_id over loser_id."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO rivalries (winner_id, loser_id, wins)
                VALUES (?, ?, 1)
                ON CONFLICT(winner_id, loser_id) DO UPDATE SET wins = wins + 1
            ''', (winner_id, loser_id))
            conn.commit()

    def get_top_rival(self, user_id: int):
        """The opponent this user has beaten the most, or None."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT loser_id, wins FROM rivalries
                WHERE winner_id = ?
                ORDER BY wins DESC
                LIMIT 1
            ''', (user_id,))
            return cursor.fetchone()

    def get_title_history(self, title_id: int, limit: int = 10):
        """Recent past holders of a title, newest first."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT discord_user_id, total_defenses, decklist, timestamp_lost
                FROM historical_stats
                WHERE title_id = ?
                ORDER BY timestamp_lost DESC
                LIMIT ?
            ''', (title_id, limit))
            return cursor.fetchall()

    def get_bounty_titles(self):
        """Names of all titles with a bounty on them."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT name FROM titles WHERE bounty_active = 1')
            return cursor.fetchall()

    def add_contender_win(self, title_id: int, user_id: int) -> int:
        """Adds a usurper win for this player on this title and returns their new total."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO contenders (title_id, discord_user_id, wins)
                VALUES (?, ?, 1)
                ON CONFLICT(title_id, discord_user_id) DO UPDATE SET wins = wins + 1
            ''', (title_id, user_id))
            cursor.execute('SELECT wins FROM contenders WHERE title_id = ? AND discord_user_id = ?', (title_id, user_id))
            row = cursor.fetchone()
            conn.commit()
            return row['wins']

    def get_top_contender(self, title_id: int):
        """The player with the most usurper wins on this title, or None."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT discord_user_id, wins FROM contenders
                WHERE title_id = ? AND wins >= 1
                ORDER BY wins DESC
                LIMIT 1
            ''', (title_id,))
            return cursor.fetchone()

    def wipe_contenders(self, title_id: int) -> int:
        """Clears all usurper wins on a title. Returns how many rows were deleted."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM contenders WHERE title_id = ?', (title_id,))
            conn.commit()
            return cursor.rowcount

    def execute_overthrow(self, title_id: int, new_champion_id: int):
        """Runs the 4 win overthrow in one transaction: archives the old reign,
        crowns the new champion at bounty life, and clears the contenders.
        Returns the old reign as a dict, or None if the title was vacant.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM active_reigns WHERE title_id = ?', (title_id,))
            current_reign = cursor.fetchone()

            if current_reign:
                keys = current_reign.keys()
                defenses = current_reign['defenses_count'] if 'defenses_count' in keys else 0
                old_decklist = current_reign['decklist'] if 'decklist' in keys else None
                is_lineal_reversion = current_reign['is_lineal_reversion'] if 'is_lineal_reversion' in keys else 0
                cursor.execute('''
                    INSERT INTO historical_stats
                    (title_id, discord_user_id, timestamp_acquired, timestamp_lost, total_defenses, decklist, is_lineal_reversion)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                ''', (
                    current_reign['title_id'],
                    current_reign['discord_user_id'],
                    current_reign['timestamp_acquired'],
                    defenses,
                    old_decklist,
                    is_lineal_reversion
                ))
                cursor.execute('DELETE FROM active_reigns WHERE id = ?', (current_reign['id'],))

            cursor.execute('UPDATE titles SET bounty_active = 0, sudden_death_contenders = NULL WHERE id = ?', (title_id,))

            cursor.execute('''
                INSERT INTO active_reigns (title_id, discord_user_id, current_life, last_upkeep_timestamp)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (title_id, new_champion_id, BOUNTY_STARTING_LIFE))

            cursor.execute('DELETE FROM contenders WHERE title_id = ?', (title_id,))

            conn.commit()
            return dict(current_reign) if current_reign else None

    def log_match(self, match_type: str, title_id: int, actor_id: int, target_id: Optional[int] = None) -> Optional[str]:
        """Saves a snapshot of the title before a match changes it, and returns the match id.

        Call this right before the change so undo_match can put things back.
        Returns None if it fails, and the match should still go through.
        """
        if match_type not in MATCH_TYPES:
            raise ValueError(f"Invalid match_type: {match_type!r}. Must be one of {MATCH_TYPES}.")

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('SELECT * FROM active_reigns WHERE title_id = ?', (title_id,))
                active_reign_row = cursor.fetchone()
                active_reign = dict(active_reign_row) if active_reign_row else None

                cursor.execute('SELECT bounty_active, sudden_death_contenders FROM titles WHERE id = ?', (title_id,))
                title_row = cursor.fetchone()
                title_snapshot = {
                    "bounty_active": title_row['bounty_active'] if title_row else None,
                    "sudden_death_contenders": title_row['sudden_death_contenders'] if title_row else None,
                }

                cursor.execute('SELECT discord_user_id, wins FROM contenders WHERE title_id = ?', (title_id,))
                contenders = [{"discord_user_id": row['discord_user_id'], "wins": row['wins']} for row in cursor.fetchall()]

                rivalry = None
                if target_id is not None:
                    cursor.execute('SELECT winner_id, loser_id, wins FROM rivalries WHERE winner_id = ? AND loser_id = ?', (actor_id, target_id))
                    rivalry_row = cursor.fetchone()
                    if rivalry_row:
                        rivalry = {"winner_id": rivalry_row['winner_id'], "loser_id": rivalry_row['loser_id'], "wins": rivalry_row['wins']}

                cursor.execute('SELECT COALESCE(MAX(id), 0) as max_id FROM historical_stats')
                max_historical_id = cursor.fetchone()['max_id']

                snapshot = {
                    "active_reign": active_reign,
                    "title": title_snapshot,
                    "contenders": contenders,
                    "rivalry": rivalry,
                    "max_historical_id": max_historical_id,
                }
                snapshot_json = json.dumps(snapshot)

                for attempt in range(5):
                    match_id = generate_match_id()
                    try:
                        cursor.execute('''
                            INSERT INTO match_log (match_id, match_type, title_id, actor_id, target_id, snapshot)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (match_id, match_type, title_id, actor_id, target_id, snapshot_json))
                        conn.commit()
                        return match_id
                    except sqlite3.IntegrityError:
                        if attempt == 4:
                            logger.exception("log_match: exhausted match_id collision retries")
                            return None
                        continue
        except sqlite3.Error:
            logger.exception("Error logging match")
            return None

    def get_match(self, match_id: str):
        """Looks up a match by id. Not case sensitive."""
        normalized_id = match_id.strip().upper()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM match_log WHERE match_id = ?', (normalized_id,))
            return cursor.fetchone()

    def get_recent_matches(self, title_id: Optional[int] = None, limit: int = 10):
        """Most recent matches, optionally for one title."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if title_id is not None:
                cursor.execute('SELECT * FROM match_log WHERE title_id = ? ORDER BY id DESC LIMIT ?', (title_id, limit))
            else:
                cursor.execute('SELECT * FROM match_log ORDER BY id DESC LIMIT ?', (limit,))
            return cursor.fetchall()

    def undo_match(self, match_id: str, admin_id: int) -> dict:
        """Puts the game state back to how it was before this match.

        Every match type is undone the same way: restore the reign, title flags,
        contenders, rivalry, and archive rows from the snapshot. Ratings come from
        match_participants instead since those get saved after the snapshot.

        Returns a dict with a "status" of: not_found, already_undone, stale,
        title_missing, unlogged_change, error, or ok.
        It all runs in one transaction so it either fully works or nothing changes.
        """
        normalized_id = match_id.strip().upper()
        row = self.get_match(normalized_id)

        if row is None:
            return {"status": "not_found", "match_id": normalized_id}

        if row['undone']:
            return {
                "status": "already_undone",
                "match_id": normalized_id,
                "undone_by": row['undone_by'],
                "undone_at": row['undone_at'],
            }

        title_id = row['title_id']
        target_id = row['target_id']

        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Only the newest match on a title can be undone, otherwise newer results would get wiped.
                cursor.execute('''
                    SELECT match_id, match_type FROM match_log
                    WHERE title_id = ? AND id > ? AND undone = 0
                    ORDER BY id DESC
                    LIMIT 1
                ''', (title_id, row['id']))
                blocking = cursor.fetchone()
                if blocking:
                    return {
                        "status": "stale",
                        "match_id": normalized_id,
                        "blocking_match_id": blocking['match_id'],
                        "blocking_match_type": blocking['match_type'],
                    }

                cursor.execute('SELECT name, sudden_death_contenders FROM titles WHERE id = ?', (title_id,))
                title_row = cursor.fetchone()
                if title_row is None:
                    # Match left over from a title that got deleted.
                    return {
                        "status": "title_missing",
                        "match_id": normalized_id,
                        "title_id": title_id,
                    }
                title_name = title_row['name']
                current_sudden_death = title_row['sudden_death_contenders']

                snapshot = json.loads(row['snapshot'])
                snapshot_reign = snapshot["active_reign"]

                # If something changed the title without logging a match (the monthly reset,
                # life running out, or an admin command), undoing would delete records that
                # aren't saved anywhere else. So I refuse instead.
                # is_eom_champ is checked too for rows from before archived_by_reset existed.
                cursor.execute(
                    'SELECT COUNT(*) AS n, COALESCE(MAX(is_eom_champ), 0) AS eom, '
                    'COALESCE(MAX(archived_by_reset), 0) AS reset_marker '
                    'FROM historical_stats WHERE title_id = ? AND id > ?',
                    (title_id, snapshot["max_historical_id"])
                )
                archive_row = cursor.fetchone()
                archived_since = archive_row['n']
                archived_by_a_reset = bool(archive_row['reset_marker']) or bool(archive_row['eom'])

                reason = None
                if archived_by_a_reset:
                    reason = (
                        "an end-of-month reset archived a champion on this title after the match was logged"
                    )
                elif archived_since > 1:
                    # These each archive one reign, so a second row came from somewhere else.
                    reason = (
                        f"{archived_since} reigns were archived after the match, but the match itself "
                        "could only have archived one"
                    )
                elif row['match_type'] == "defense" and archived_since > 0:
                    # A defense doesn't archive anything.
                    reason = (
                        "a reign was archived after this defense, and a defense never archives a reign"
                    )

                if reason is not None:
                    return {
                        "status": "unlogged_change",
                        "match_id": normalized_id,
                        "match_type": row['match_type'],
                        "title_id": title_id,
                        "title_name": title_name,
                        "reason": reason,
                    }

                # Save the current state before I change it.
                cursor.execute('SELECT discord_user_id, current_life FROM active_reigns WHERE title_id = ?', (title_id,))
                current_reign = cursor.fetchone()
                reverted_holder_id = current_reign['discord_user_id'] if current_reign else None
                reverted_life = current_reign['current_life'] if current_reign else None

                # 1. Delete the archive rows this match made.
                cursor.execute(
                    'DELETE FROM historical_stats WHERE title_id = ? AND id > ?',
                    (title_id, snapshot["max_historical_id"])
                )

                # 2. Put the reign back exactly how it was.
                cursor.execute('DELETE FROM active_reigns WHERE title_id = ?', (title_id,))
                if snapshot_reign is not None:
                    # Using the snapshot's keys means new columns get restored too.
                    columns = list(snapshot_reign.keys())
                    cursor.execute(
                        'INSERT INTO active_reigns ({}) VALUES ({})'.format(
                            ", ".join(columns),
                            ", ".join("?" for _ in columns)
                        ),
                        [snapshot_reign[column] for column in columns]
                    )

                # 3. Bounty and sudden death flags.
                cursor.execute('''
                    UPDATE titles
                    SET bounty_active = ?, sudden_death_contenders = ?
                    WHERE id = ?
                ''', (snapshot["title"]["bounty_active"], snapshot["title"]["sudden_death_contenders"], title_id))

                # 4. Contenders.
                cursor.execute('DELETE FROM contenders WHERE title_id = ?', (title_id,))
                for contender in snapshot["contenders"]:
                    cursor.execute(
                        'INSERT INTO contenders (title_id, discord_user_id, wins) VALUES (?, ?, ?)',
                        (title_id, contender["discord_user_id"], contender["wins"])
                    )

                # 5. Rivalry: lower the count, or delete it if this match created it.
                if snapshot["rivalry"] is not None:
                    cursor.execute(
                        'UPDATE rivalries SET wins = ? WHERE winner_id = ? AND loser_id = ?',
                        (snapshot["rivalry"]["wins"], snapshot["rivalry"]["winner_id"], snapshot["rivalry"]["loser_id"])
                    )
                elif target_id is not None:
                    cursor.execute(
                        'DELETE FROM rivalries WHERE winner_id = ? AND loser_id = ?',
                        (row['actor_id'], target_id)
                    )

                # 6. Ratings. The pre match values are on the match_participants rows.
                # Ratings are global but the stale check is per title, so a player might
                # have played on another title since. I count those as ratings_stale_for
                # so the admin knows the restore isn't exact.
                cursor.execute('''
                    SELECT user_id, rating_before, rd_before, vol_before
                    FROM match_participants
                    WHERE match_log_id = ?
                ''', (row['id'],))
                participant_rows = cursor.fetchall()
                ratings_stale_for = 0

                for participant in participant_rows:
                    user_id = participant['user_id']

                    # Has this player been in a newer match?
                    cursor.execute(
                        'SELECT 1 FROM match_participants WHERE user_id = ? AND match_log_id > ? LIMIT 1',
                        (user_id, row['id'])
                    )
                    is_stale = cursor.fetchone() is not None

                    if participant['rating_before'] is None:
                        # NULL means they had no rating before this match. I only delete their row
                        # if this was their only match, otherwise I'd wipe out all their other games.
                        # If they have other matches I leave it and count them as stale.
                        cursor.execute('''
                            DELETE FROM player_ratings
                             WHERE user_id = ?
                               AND NOT EXISTS (SELECT 1 FROM match_participants
                                                WHERE user_id = ? AND match_log_id != ?)
                        ''', (user_id, user_id, row['id']))
                        if cursor.rowcount == 0:
                            cursor.execute('SELECT 1 FROM player_ratings WHERE user_id = ? LIMIT 1', (user_id,))
                            if cursor.fetchone() is not None:
                                is_stale = True
                    else:
                        # Fallback in case an old row is half filled in.
                        restored_rd = participant['rd_before'] if participant['rd_before'] is not None else DEFAULT_RD
                        restored_vol = participant['vol_before'] if participant['vol_before'] is not None else DEFAULT_VOL

                        # Writing back an old rating is fine even if they've played since. It'll
                        # correct itself over a few games, and it's better than never allowing undos.
                        cursor.execute('''
                            UPDATE player_ratings
                            SET rating = ?, rd = ?, vol = ?, last_updated = CURRENT_TIMESTAMP
                            WHERE user_id = ?
                        ''', (participant['rating_before'], restored_rd, restored_vol, user_id))
                        if cursor.rowcount == 0:
                            # Row got deleted somehow, so insert it again.
                            cursor.execute('''
                                INSERT INTO player_ratings (user_id, rating, rd, vol, last_updated)
                                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                            ''', (user_id, participant['rating_before'], restored_rd, restored_vol))

                    if is_stale:
                        ratings_stale_for += 1

                # Delete the participant rows so a redo gets rated again.
                cursor.execute('DELETE FROM match_participants WHERE match_log_id = ?', (row['id'],))
                ratings_reverted = len(participant_rows)

                # 7. Mark it undone.
                cursor.execute('''
                    UPDATE match_log
                    SET undone = 1, undone_by = ?, undone_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (admin_id, row['id']))

                conn.commit()

                sudden_death_cleared = bool(current_sudden_death) and (
                    current_sudden_death != snapshot["title"]["sudden_death_contenders"]
                )

                return {
                    "status": "ok",
                    "match_id": normalized_id,
                    "match_type": row['match_type'],
                    "title_id": title_id,
                    "title_name": title_name,
                    "actor_id": row['actor_id'],
                    "target_id": target_id,
                    "timestamp": row['timestamp'],
                    "reverted_holder_id": reverted_holder_id,
                    "reverted_life": reverted_life,
                    "restored_holder_id": snapshot_reign["discord_user_id"] if snapshot_reign else None,
                    "restored_life": snapshot_reign["current_life"] if snapshot_reign else None,
                    "restored_defenses": snapshot_reign["defenses_count"] if snapshot_reign else None,
                    "sudden_death_cleared": sudden_death_cleared,
                    "contenders_restored": len(snapshot["contenders"]),
                    "ratings_reverted": ratings_reverted,
                    "ratings_stale_for": ratings_stale_for,
                }
        except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as e:
            # The transaction already rolled back. I catch the other errors too so a
            # bad snapshot gives an error message instead of a stuck command.
            logger.exception("Error undoing match %s", normalized_id)
            return {"status": "error", "message": str(e)}

    def get_player_rating(self, user_id: int) -> dict:
        """Rating info for one player. If they have no row they get the defaults
        and count as provisional. I don't create a row for them here.
        """
        empty = {
            "user_id": user_id,
            "rating": DEFAULT_RATING,
            "rd": DEFAULT_RD,
            "vol": DEFAULT_VOL,
            "last_updated": None,
            "is_provisional": True,
        }
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT user_id, rating, rd, vol, last_updated FROM player_ratings WHERE user_id = ?', (user_id,))
                row = cursor.fetchone()

            if row is None:
                return empty

            return {
                "user_id": row['user_id'],
                "rating": row['rating'],
                "rd": row['rd'],
                "vol": row['vol'],
                "last_updated": row['last_updated'],
                "is_provisional": row['rd'] >= PROVISIONAL_RD,
            }
        except sqlite3.Error:
            logger.exception("Error reading rating for user %s", user_id)
            return empty

    def get_rating_leaderboard(self, limit: int = 10) -> list:
        """Top rated players, sorted by rating - 2*rd so one lucky game with a
        high RD doesn't put someone at the top. Returns [] if the query fails.
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, rating, rd, vol, (rating - 2 * rd) AS conservative
                    FROM player_ratings
                    ORDER BY conservative DESC
                    LIMIT ?
                ''', (limit,))
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error:
            logger.exception("Error reading rating leaderboard")
            return []

    def get_rating_rank(self, user_id: int) -> dict:
        """This player's rank and the total number of rated players.
        rank is None if they aren't rated.
        """
        empty = {"rank": None, "total": 0}
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) AS n FROM player_ratings')
                total = cursor.fetchone()['n']

                cursor.execute('SELECT rating, rd FROM player_ratings WHERE user_id = ?', (user_id,))
                row = cursor.fetchone()
                if row is None:
                    return {"rank": None, "total": total}

                # Rank is how many players are above them plus one, so ties share a rank.
                conservative = row['rating'] - 2 * row['rd']
                cursor.execute(
                    'SELECT COUNT(*) AS n FROM player_ratings WHERE (rating - 2 * rd) > ?',
                    (conservative,)
                )
                ahead = cursor.fetchone()['n']

            return {"rank": ahead + 1, "total": total}
        except sqlite3.Error:
            logger.exception("Error ranking rating for user %s", user_id)
            return empty

    def set_match_deck_metadata(self, match_id: str, commander_name, color_identity, deck_url) -> bool:
        """Saves the deck info onto a match. Returns True if it updated a row.
        Never raises so a failed deck lookup can't break the match.
        """
        try:
            normalized_id = str(match_id).strip().upper()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE match_log
                    SET commander_name = ?, color_identity = ?, deck_url = ?
                    WHERE match_id = ?
                ''', (commander_name, color_identity, deck_url, normalized_id))
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error:
            logger.exception("Error setting deck metadata for match %s", match_id)
            return False

    def record_match_ratings(self, match_id: str, winner_id, loser_ids: list,
                             is_draw: bool = False, deck_metadata: Optional[dict] = None) -> Optional[dict]:
        """Saves who played in the match and updates everyone's Glicko-2 rating
        in one transaction.

        If is_draw is True, winner_id is ignored and everyone in loser_ids drew.
        Returns a dict with each player's rating change, or None if the match is
        unknown, already rated, has fewer than 2 players, or something fails.
        Never raises because the title change already happened by this point.
        """
        try:
            normalized_id = str(match_id).strip().upper()

            with self.get_connection() as conn:
                # Everything is in one transaction so ratings never half save.
                # Don't call get_player_rating in here, it would commit early.
                cursor = conn.cursor()

                cursor.execute('SELECT id FROM match_log WHERE match_id = ?', (normalized_id,))
                match_row = cursor.fetchone()
                if match_row is None:
                    return None
                match_log_id = match_row['id']

                # Stops the same match from being rated twice if someone double clicks.
                cursor.execute('SELECT COUNT(*) AS n FROM match_participants WHERE match_log_id = ?', (match_log_id,))
                if cursor.fetchone()['n'] > 0:
                    return None

                # Remove duplicates myself so the UNIQUE constraint doesn't fail.
                raw_ids = list(loser_ids or [])
                if is_draw:
                    excluded = set()
                else:
                    excluded = {winner_id}

                ordered_losers = []
                seen = set(excluded)
                for user_id in raw_ids:
                    if user_id is None or user_id in seen:
                        continue
                    seen.add(user_id)
                    ordered_losers.append(user_id)

                if is_draw:
                    player_ids = ordered_losers
                    results = build_draw_results(player_ids)
                    result_by_player = {user_id: "draw" for user_id in player_ids}
                else:
                    if winner_id is None:
                        return None
                    player_ids = [winner_id] + ordered_losers
                    results = build_pod_results(winner_id, ordered_losers)
                    result_by_player = {user_id: "loss" for user_id in ordered_losers}
                    result_by_player[winner_id] = "win"

                if len(player_ids) < 2:
                    # Nothing to rate with one player.
                    return None

                # stored keeps None for unrated players, players has the values actually used.
                stored = {}
                players = {}
                for user_id in player_ids:
                    cursor.execute('SELECT rating, rd, vol FROM player_ratings WHERE user_id = ?', (user_id,))
                    rating_row = cursor.fetchone()
                    stored[user_id] = dict(rating_row) if rating_row else None
                    players[user_id] = stored[user_id] or {
                        "rating": DEFAULT_RATING,
                        "rd": DEFAULT_RD,
                        "vol": DEFAULT_VOL,
                    }

                new_ratings = rate_period(players, results)

                metadata = deck_metadata or {}
                participants = []
                for user_id in player_ids:
                    before = stored[user_id]
                    after = new_ratings[user_id]
                    deck = metadata.get(user_id) or {}

                    cursor.execute('''
                        INSERT INTO match_participants (
                            match_log_id, user_id, result,
                            commander_name, color_identity, deck_url,
                            rating_before, rd_before, vol_before
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        match_log_id,
                        user_id,
                        result_by_player[user_id],
                        deck.get("commander_name"),
                        deck.get("color_identity"),
                        deck.get("deck_url"),
                        before["rating"] if before else None,
                        before["rd"] if before else None,
                        before["vol"] if before else None,
                    ))

                    cursor.execute('''
                        INSERT INTO player_ratings (user_id, rating, rd, vol, last_updated)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id) DO UPDATE SET
                            rating = excluded.rating,
                            rd = excluded.rd,
                            vol = excluded.vol,
                            last_updated = CURRENT_TIMESTAMP
                    ''', (user_id, after["rating"], after["rd"], after["vol"]))

                    # Using the effective rating here so the delta works for unrated players too.
                    participants.append({
                        "user_id": user_id,
                        "result": result_by_player[user_id],
                        "rating_before": players[user_id]["rating"],
                        "rating_after": after["rating"],
                        "delta": after["rating"] - players[user_id]["rating"],
                    })

                conn.commit()

                return {
                    "match_id": normalized_id,
                    "match_log_id": match_log_id,
                    "participants": participants,
                }
        except Exception:
            # Catching everything since the title change already saved. The
            # transaction rolls back so nothing half saves.
            logger.exception("Error recording match ratings for %s", match_id)
            return None

    def get_match_participants(self, match_log_id: int) -> list:
        """All participant rows for a match_log id (the number, not the match id string), as dicts."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM match_participants WHERE match_log_id = ? ORDER BY id', (match_log_id,))
            return [dict(row) for row in cursor.fetchall()]

    # Stats queries for /profile and /server_meta. These only read data, and each one
    # returns an empty result if it fails instead of raising.

    def get_player_match_record(self, user_id: int) -> dict:
        """A player's wins, losses, draws and win rate."""
        empty = {"matches": 0, "wins": 0, "losses": 0, "draws": 0, "win_rate": 0.0}
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # Join match_log so undone matches don't count.
                cursor.execute('''
                    SELECT mp.result AS result, COUNT(*) AS n
                    FROM match_participants mp
                    JOIN match_log ml ON mp.match_log_id = ml.id
                    WHERE mp.user_id = ? AND ml.undone = 0
                    GROUP BY mp.result
                ''', (user_id,))
                counts = {row['result']: row['n'] for row in cursor.fetchall()}

            wins = counts.get("win", 0)
            losses = counts.get("loss", 0)
            draws = counts.get("draw", 0)
            matches = wins + losses + draws
            win_rate = round(wins / matches * 100, 1) if matches else 0.0

            return {
                "matches": matches,
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "win_rate": win_rate,
            }
        except sqlite3.Error:
            logger.exception("Error aggregating match record for user %s", user_id)
            return empty

    def get_player_commanders(self, user_id: int, limit: int = 5) -> list:
        """Each commander a player has used, most played first."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # Skip undone matches. MAX just picks the color identity since it's the same for every row.
                cursor.execute('''
                    SELECT mp.commander_name AS commander_name,
                           MAX(mp.color_identity) AS color_identity,
                           COUNT(*) AS matches,
                           SUM(CASE WHEN mp.result = 'win' THEN 1 ELSE 0 END) AS wins
                    FROM match_participants mp
                    JOIN match_log ml ON mp.match_log_id = ml.id
                    WHERE mp.user_id = ?
                      AND ml.undone = 0
                      AND mp.commander_name IS NOT NULL
                      AND mp.commander_name != ''
                    GROUP BY mp.commander_name
                    ORDER BY matches DESC,
                             (CAST(wins AS REAL) / matches) DESC,
                             mp.commander_name ASC
                    LIMIT ?
                ''', (user_id, limit))
                rows = cursor.fetchall()

            results = []
            for row in rows:
                matches = row['matches']
                wins = row['wins']
                win_rate = round(wins / matches * 100, 1) if matches else 0.0
                results.append({
                    "commander_name": row['commander_name'],
                    "color_identity": row['color_identity'],
                    "matches": matches,
                    "wins": wins,
                    "win_rate": win_rate,
                })
            return results
        except sqlite3.Error:
            logger.exception("Error aggregating commander breakdown for user %s", user_id)
            return []

    def get_player_title_summary(self, user_id: int) -> dict:
        """Active titles, lifetime defenses, and how many titles the player has held."""
        empty = {"active_titles": [], "lifetime_defenses": 0, "titles_held": 0}
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT ar.title_id AS title_id, t.name AS title_name,
                           ar.defenses_count AS defenses, ar.current_life AS current_life
                    FROM active_reigns ar
                    JOIN titles t ON ar.title_id = t.id
                    WHERE ar.discord_user_id = ?
                    ORDER BY t.name ASC
                ''', (user_id,))
                active_titles = [
                    {
                        "title_id": row['title_id'],
                        "title_name": row['title_name'],
                        "defenses": row['defenses'],
                        "current_life": row['current_life'],
                    }
                    for row in cursor.fetchall()
                ]

                cursor.execute(
                    'SELECT COALESCE(SUM(defenses_count), 0) AS n FROM active_reigns WHERE discord_user_id = ?',
                    (user_id,)
                )
                active_defenses = cursor.fetchone()['n']

                cursor.execute(
                    'SELECT COALESCE(SUM(total_defenses), 0) AS n FROM historical_stats WHERE discord_user_id = ?',
                    (user_id,)
                )
                historical_defenses = cursor.fetchone()['n']

                cursor.execute('''
                    SELECT COUNT(DISTINCT title_id) AS n FROM (
                        SELECT title_id FROM active_reigns WHERE discord_user_id = ?
                        UNION
                        SELECT title_id FROM historical_stats WHERE discord_user_id = ?
                    )
                ''', (user_id, user_id))
                titles_held = cursor.fetchone()['n']

            return {
                "active_titles": active_titles,
                "lifetime_defenses": active_defenses + historical_defenses,
                "titles_held": titles_held,
            }
        except sqlite3.Error:
            logger.exception("Error aggregating title summary for user %s", user_id)
            return empty

    def get_server_meta(self, limit: int = 5) -> dict:
        """Server wide stats: top commanders, color breakdown, and match/player counts.
        Colors are returned as letters, main.py turns them into names.
        """
        empty = {
            "top_commanders": [],
            "color_breakdown": [],
            "total_matches": 0,
            "attributed_matches": 0,
            "active_players": 0,
            "rated_players": 0,
        }
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Undone matches are skipped in all of these.
                cursor.execute('''
                    SELECT mp.commander_name AS commander_name,
                           MAX(mp.color_identity) AS color_identity,
                           COUNT(*) AS matches,
                           SUM(CASE WHEN mp.result = 'win' THEN 1 ELSE 0 END) AS wins
                    FROM match_participants mp
                    JOIN match_log ml ON mp.match_log_id = ml.id
                    WHERE ml.undone = 0
                      AND mp.commander_name IS NOT NULL
                      AND mp.commander_name != ''
                    GROUP BY mp.commander_name
                    ORDER BY matches DESC,
                             (CAST(wins AS REAL) / matches) DESC,
                             mp.commander_name ASC
                    LIMIT ?
                ''', (limit,))
                top_commander_rows = cursor.fetchall()

                cursor.execute('''
                    SELECT mp.color_identity AS color_identity, COUNT(*) AS matches
                    FROM match_participants mp
                    JOIN match_log ml ON mp.match_log_id = ml.id
                    WHERE ml.undone = 0
                      AND mp.commander_name IS NOT NULL
                      AND mp.commander_name != ''
                    GROUP BY mp.color_identity
                    ORDER BY matches DESC
                ''')
                color_rows = cursor.fetchall()

                # Count matches, not participant rows.
                cursor.execute('''
                    SELECT COUNT(DISTINCT ml.id) AS n
                    FROM match_log ml
                    JOIN match_participants mp ON mp.match_log_id = ml.id
                    WHERE ml.undone = 0
                ''')
                total_matches = cursor.fetchone()['n']

                cursor.execute('''
                    SELECT COUNT(*) AS n
                    FROM match_participants mp
                    JOIN match_log ml ON mp.match_log_id = ml.id
                    WHERE ml.undone = 0
                      AND mp.commander_name IS NOT NULL
                      AND mp.commander_name != ''
                ''')
                attributed_matches = cursor.fetchone()['n']

                cursor.execute('''
                    SELECT COUNT(DISTINCT mp.user_id) AS n
                    FROM match_participants mp
                    JOIN match_log ml ON mp.match_log_id = ml.id
                    WHERE ml.undone = 0
                ''')
                active_players = cursor.fetchone()['n']

                cursor.execute('SELECT COUNT(*) AS n FROM player_ratings')
                rated_players = cursor.fetchone()['n']

            top_commanders = []
            for row in top_commander_rows:
                matches = row['matches']
                wins = row['wins']
                win_rate = round(wins / matches * 100, 1) if matches else 0.0
                top_commanders.append({
                    "commander_name": row['commander_name'],
                    "color_identity": row['color_identity'],
                    "matches": matches,
                    "wins": wins,
                    "win_rate": win_rate,
                })

            color_breakdown = []
            for row in color_rows:
                matches = row['matches']
                share = round(matches / attributed_matches * 100, 1) if attributed_matches else 0.0
                color_breakdown.append({
                    "color_identity": row['color_identity'],
                    "matches": matches,
                    "share": share,
                })

            return {
                "top_commanders": top_commanders,
                "color_breakdown": color_breakdown,
                "total_matches": total_matches,
                "attributed_matches": attributed_matches,
                "active_players": active_players,
                "rated_players": rated_players,
            }
        except sqlite3.Error:
            logger.exception("Error aggregating server meta")
            return empty

db = DBManager()
