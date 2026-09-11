"""Glicko-2 ratings for 4 player cEDH pods.

Only uses the math module so it can be tested without discord or a database.
I followed Glickman's paper (http://www.glicko.net/glicko/glicko2.pdf) step by step.
Since a pod isn't a 1v1 game, I split each pod into pairwise results first
(build_pod_results / build_draw_results) and then rate everyone at once
with rate_period.
"""
import math

DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOL = 0.06
TAU = 0.5  # system constant, limits how fast volatility changes
EPSILON = 0.000001  # how close the volatility loop has to get before stopping
GLICKO2_SCALE = 173.7178


def to_glicko2(rating: float, rd: float) -> tuple:
    """Glicko-1 scale to Glicko-2 scale."""
    mu = (rating - DEFAULT_RATING) / GLICKO2_SCALE
    phi = rd / GLICKO2_SCALE
    return mu, phi


def from_glicko2(mu: float, phi: float) -> tuple:
    """Glicko-2 scale back to Glicko-1 scale."""
    rating = GLICKO2_SCALE * mu + DEFAULT_RATING
    rd = GLICKO2_SCALE * phi
    return rating, rd


def _g(phi: float) -> float:
    """g() from the paper. Opponents with a high RD count for less."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi ** 2 / math.pi ** 2)


def _e(mu: float, mu_j: float, phi_j: float) -> float:
    """Expected score against one opponent."""
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _not_competed(rating: float, mu: float, phi: float, vol: float) -> dict:
    """Update for a player who didn't play: RD goes up, rating and vol stay the same."""
    phi_star = math.sqrt(phi ** 2 + vol ** 2)
    _, new_rd = from_glicko2(mu, phi_star)
    return {"rating": rating, "rd": min(new_rd, DEFAULT_RD), "vol": vol}


def rate(rating: float, rd: float, vol: float, opponents: list) -> dict:
    """One Glicko-2 rating period for one player.

    opponents is a list of (rating, rd, score) where score is 1.0 win, 0.5 draw, 0.0 loss.
    Returns a dict with rating, rd and vol.
    """
    # Bad values from the database shouldn't crash math.sqrt or math.log, so I reset them.
    if rd <= 0:
        rd = DEFAULT_RD
    if vol <= 0:
        vol = DEFAULT_VOL

    mu, phi = to_glicko2(rating, rd)

    if not opponents:
        return _not_competed(rating, mu, phi, vol)

    # Steps 3 and 4 use the same g and E values so I do them in one loop.
    v_inv = 0.0
    delta_sum = 0.0
    for opp_rating, opp_rd, score in opponents:
        mu_j, phi_j = to_glicko2(opp_rating, opp_rd)
        g_j = _g(phi_j)
        e_j = _e(mu, mu_j, phi_j)
        v_inv += g_j ** 2 * e_j * (1.0 - e_j)
        delta_sum += g_j * (score - e_j)

    # If the rating gap is huge, E rounds to exactly 0 or 1 and v_inv becomes 0,
    # which would divide by zero. In that case I treat it like the player didn't play.
    # You'd need a gap around 100,000 points for this to happen, so it's just a safety net.
    if v_inv <= 0 or not math.isfinite(v_inv):
        return _not_competed(rating, mu, phi, vol)

    v = 1.0 / v_inv
    delta = v * delta_sum

    # Step 5: find the new volatility with the Illinois algorithm.
    a = math.log(vol ** 2)

    def f(x):
        ex = math.exp(x)
        term1 = ex * (delta ** 2 - phi ** 2 - v - ex) / (2.0 * (phi ** 2 + v + ex) ** 2)
        term2 = (x - a) / (TAU ** 2)
        return term1 - term2

    big_a = a
    if delta ** 2 > phi ** 2 + v:
        big_b = math.log(delta ** 2 - phi ** 2 - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        big_b = a - k * TAU

    f_a = f(big_a)
    f_b = f(big_b)

    while abs(big_b - big_a) > EPSILON:
        big_c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = f(big_c)
        if f_c * f_b < 0:
            big_a = big_b
            f_a = f_b
        else:
            f_a = f_a / 2.0
        big_b = big_c
        f_b = f_c

    new_vol = math.exp(big_a / 2.0)

    # Step 6
    phi_star = math.sqrt(phi ** 2 + new_vol ** 2)

    # Step 7: new phi and mu
    new_phi = 1.0 / math.sqrt(1.0 / phi_star ** 2 + 1.0 / v)
    new_mu = mu + new_phi ** 2 * delta_sum

    new_rating, new_rd = from_glicko2(new_mu, new_phi)
    return {"rating": new_rating, "rd": new_rd, "vol": new_vol}


def rate_period(players: dict, results: list) -> dict:
    """Rates every player in a pod at the same time.

    players maps user_id to their rating/rd/vol. results is a list of
    (player_a, player_b, score_for_a); I work out player_b's score myself.
    Everyone is rated against the ratings from before the pod, not updated ones.
    """
    # Copy the starting ratings so nobody gets rated against an already updated value.
    pre_period = {user_id: dict(state) for user_id, state in players.items()}

    opponents_by_player = {user_id: [] for user_id in players}
    for player_a, player_b, score in results:
        state_a = pre_period[player_a]
        state_b = pre_period[player_b]
        opponents_by_player[player_a].append((state_b["rating"], state_b["rd"], score))
        opponents_by_player[player_b].append((state_a["rating"], state_a["rd"], 1.0 - score))

    return {
        user_id: rate(state["rating"], state["rd"], state["vol"], opponents_by_player[user_id])
        for user_id, state in players.items()
    }


def build_pod_results(winner_id: int, loser_ids: list) -> list:
    """Turns a pod win into pairwise results. The winner beats everyone and the
    losers draw with each other, since 2nd through 4th place doesn't really mean anything.
    """
    deduped_losers = []
    seen = {winner_id}
    for loser_id in loser_ids:
        if loser_id not in seen:
            seen.add(loser_id)
            deduped_losers.append(loser_id)

    results = [(winner_id, loser_id, 1.0) for loser_id in deduped_losers]
    for i in range(len(deduped_losers)):
        for j in range(i + 1, len(deduped_losers)):
            results.append((deduped_losers[i], deduped_losers[j], 0.5))
    return results


def build_draw_results(player_ids: list) -> list:
    """Every pair of players draws."""
    deduped = []
    seen = set()
    for player_id in player_ids:
        if player_id not in seen:
            seen.add(player_id)
            deduped.append(player_id)

    results = []
    for i in range(len(deduped)):
        for j in range(i + 1, len(deduped)):
            results.append((deduped[i], deduped[j], 0.5))
    return results
