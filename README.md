# Slayerbot Crownwatch

A King of the Hill style ranking system for Magic: The Gathering Discord communities. Players win
titles, defend them, and try to take them from each other. Ratings use Glicko-2, and a SQLite
database tracks match history, defenses, decklists, and titles.

I built this for a competitive Commander (cEDH) playgroup I'm part of. It started as a simple way to
track who held each title, and grew into a full system with peer verification, ratings, and an admin
undo.

---

## How it works

### Claiming and defending

When you win a pod, you run `/slayed <title>` and the bot posts a verification prompt. Another player
has to click ✅ before anything is saved, so nobody can verify their own match. If you already hold
the title, the same command logs a defense instead.

### Life

Every title has a life total that goes down over time, so a champion can't just sit on it.

| Rule | Value |
|---|---|
| Starting life | 50 |
| Starting life when the title has a bounty | 60 |
| Damage to the champion on a verified draw | 20 |
| Decay | 5 every 12 hours |
| Warning | at 10 life |
| Title is vacated | at 0 or below |

A defense resets life to full, and every third defense in a row adds a +5 Momentum Bonus.

### Draws and Sudden Death

A verified draw damages the champion. If that knocks them out, the title goes into Sudden Death and
only the players from that draw can claim it next.

### Usurping

If the champion isn't at the table, the winner uses `/usurp` instead. Each verified win counts as a
contender win. At 3 wins the champion gets a public warning, and at 4 the usurper takes the title.
A successful defense by the champion wipes all contender wins.

### Ratings

Every verified match updates all four players' ratings with Glicko-2 (starting at 1500 rating, 350 RD,
0.06 volatility). A 4 player pod isn't a 1v1 game, so I split it into pairs: the winner beats all
three other players and the losers draw with each other. Ratings count as provisional until RD drops
below 100.

### Logging from SpellBot

Our group uses SpellBot to start games, so you can right click a SpellBot match message and choose
**Apps → Log Slayer Match**. The bot reads the four players from the embed, asks for an optional
Moxfield deck link, and figures out on its own whether it's a claim, a defense, or a usurp based on
whether the champion was at the table.

---

## Commands

| Command | What it does |
|---|---|
| `/slayed <title> [decklist]` | Claim or defend a title |
| `/usurp <title> [decklist]` | Log a win while the champion was absent |
| `/whoslayer` | Every title and who holds it |
| `/leaderboard` | Top rated players |
| `/profile [user]` | Rating, record, titles, and most played commanders |
| `/server_meta` | Commander and color breakdown for the whole server |
| `/slayerstats [user]` | Claims, defenses, and history for a player |
| `/nemesis [user]` | The player you've beaten the most |
| `/title_history <title>` | Recent holders of a title |
| `/hall_of_fame` | All time server records |
| `/past_champions` | End of month champions |
| `/ironman_leaderboard` | Most claims + defenses this month |
| `/bounties` | Titles with a bounty on them |
| `/slayer_rules` | Rules card |
| `/help` | User guide |

Admin commands (need the Discord administrator permission): `/mint_title`, `/edit_title`,
`/delete_title`, `/grant_title`, `/set_original_holder`, `/set_life`, `/set_defenses`,
`/force_vacate`, `/toggle_bounty`, `/undo_match`, `/force_cycle_reset`,
`/set_announcement_channel`, `/add_admin_role`, `/remove_admin_role`, `/bot_status`

`/undo_match <match id>` rolls back a logged match: the title change, life totals, contender wins,
rivalry record, and ratings all go back to what they were before the match.

At the end of each month the bot archives every reign, announces the month's champions and the Iron
Man, and gives each title back to its original holder.

---

## Project structure

| File | What it does |
|---|---|
| `main.py` | The Discord side: slash commands, the context menu, and the buttons and pickers |
| `db_manager.py` | All database reads and writes, plus the helper logic I wanted to test |
| `rating_engine.py` | Glicko-2, using only the `math` module |
| `moxfield_api.py` | Pulls commander, colors, and art from a Moxfield deck link |
| `test_*.py` | pytest tests |

A few decisions worth pointing out:

- **Testable logic goes in `db_manager.py`.** `main.py` imports discord.py, so the tests can't import
  it. Anything I wanted to unit test, like deciding whether a match is a claim, defense, or usurp, is
  a plain function in `db_manager.py` that `main.py` imports.
- **Every match is logged before anything changes.** The match log stores a snapshot of the title
  first, which is what makes `/undo_match` possible. The undo runs in a single transaction so it
  either fully works or changes nothing.
- **Ratings are saved in one transaction.** If something fails halfway, no player ends up with a
  half updated rating, and a double click can't rate the same match twice.
- **Rows use bracket notation.** Queries return `sqlite3.Row`, which doesn't have `.get()`, so I
  always use `row['column']`.

The database has 10 tables and new columns are added automatically on startup, so updating the bot
never requires wiping the database.

---

## Running it

Requires Python 3.10 or newer.

```bash
pip install discord.py aiohttp python-dotenv tzdata
```

Create a `.env` file next to `main.py`:

```
DISCORD_TOKEN=your_bot_token_here
```

Then run:

```bash
python main.py
```

The database file (`slayer_bot.db`) is created on the first run. The bot needs the **Manage Roles**
permission, and its role has to be above the title roles it hands out. `tzdata` is only needed on
systems without a built in timezone database, like Windows.

## Tests

```bash
python -m pytest -q
```

The tests cover the database layer, the Glicko-2 engine (including Glickman's own worked example from
his paper), the Moxfield parser, and the helper functions. The Discord layer can't be imported by the
tests, so I check it with `python -m py_compile main.py` and by testing it live in Discord.

---

## How I built it

I'm mostly self taught in Python. I wrote this project myself and used Claude to help me work through
roadblocks along the way, like debugging, reviewing edge cases, and checking my Glicko-2 math against
the paper.
