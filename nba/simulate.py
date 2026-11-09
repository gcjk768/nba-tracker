"""Monte Carlo of the rest of the regular season, the play-in and the playoffs (numpy, vectorised over runs).

Strength = points of margin per game vs an average team (net * pace / 100). Regular-season games use
`s_reg` (today's availability), playoff series use `s_po` (healthy projected rotation).
"""
import math
from functools import lru_cache

import numpy as np

from . import espn

TEAMS = sorted(espn.TEAM_IDS)
IDX = {t: i for i, t in enumerate(TEAMS)}
CONF = np.array([1 if t in espn.EAST else 0 for t in TEAMS])     # 1 = East
HOME_PATTERN = (1, 1, 0, 0, 1, 0, 1)                              # 2-2-1-1-1, 1 = higher seed at home


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def series_prob(s_hi, s_lo, hca, sigma, wins_hi=0, wins_lo=0) -> float:
    """P(higher seed wins a best-of-7) from the current series score."""
    ph = _phi((s_hi - s_lo + hca) / sigma)     # higher seed at home
    pa = _phi((s_hi - s_lo - hca) / sigma)

    @lru_cache(None)
    def f(a, b):
        if a == 4:
            return 1.0
        if b == 4:
            return 0.0
        p = ph if HOME_PATTERN[a + b] else pa
        return p * f(a + 1, b) + (1 - p) * f(a, b + 1)
    return f(wins_hi, wins_lo)


def _series_table(s, hca, sigma):
    n = len(s)
    P = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i != j:
                P[i, j] = series_prob(s[i], s[j], hca, sigma)
    return P


def simulate(s_reg: dict, s_po: dict, wins: dict, losses: dict, remaining: list, hca: float, sigma: float,
             n=10000, seed=None, series_state=None, playin_done=None, seeds_fixed=None):
    """remaining: [(home, away, neutral)] regular-season games still to play.
    series_state: {(hi, lo): (wins_hi, wins_lo)} for playoff series under way (optional).
    Returns {team: {playoffs, conf_finals, finals, title}} as probabilities."""
    rng = np.random.default_rng(seed)
    sr = np.array([s_reg.get(t, 0.0) for t in TEAMS])
    sp = np.array([s_po.get(t, 0.0) for t in TEAMS])
    W = np.tile(np.array([wins.get(t, 0) for t in TEAMS], float), (n, 1))
    if remaining:
        hi = np.array([IDX[h] for h, _, _ in remaining])
        ai = np.array([IDX[a] for _, a, _ in remaining])
        neu = np.array([bool(x) for _, _, x in remaining])
        mu = sr[hi] - sr[ai] + np.where(neu, 0.0, hca)
        home_win = (rng.standard_normal((n, len(remaining))) * sigma + mu > 0).astype(float)
        eye = np.eye(len(TEAMS))
        W += home_win @ eye[hi] + (1 - home_win) @ eye[ai]
    W += rng.random(W.shape) * 1e-3                   # ponytail: random tiebreaks, not the NBA's tiebreak rules
    P = _series_table(sp, hca, sigma)
    G = np.array([[_phi((sp[i] - sp[j] + hca) / sigma) for j in range(len(TEAMS))] for i in range(len(TEAMS))])
    res = {k: np.zeros(len(TEAMS)) for k in ("playoffs", "conf_finals", "finals", "title")}
    conf_champs = []
    for conf in (1, 0):
        members = np.where(CONF == conf)[0]
        order = members[np.argsort(-W[:, members], axis=1)]          # (n, 15) seeds 1..15
        if seeds_fixed and conf in seeds_fixed:
            order = np.tile(np.array([IDX[t] for t in seeds_fixed[conf]]), (n, 1))
        s7, s8, s9, s10 = order[:, 6], order[:, 7], order[:, 8], order[:, 9]
        g = lambda a, b: rng.random(n) < G[a, b]          # single play-in game, first team at home
        if playin_done and conf in playin_done:
            seed7 = np.full(n, IDX[playin_done[conf][0]]); seed8 = np.full(n, IDX[playin_done[conf][1]])
        else:
            w78 = g(s7, s8)
            seed7 = np.where(w78, s7, s8); l78 = np.where(w78, s8, s7)
            w910 = np.where(g(s9, s10), s9, s10)
            seed8 = np.where(g(l78, w910), l78, w910)
        seeds = np.column_stack([order[:, :6], seed7, seed8])          # (n, 8)
        for k in range(8):
            np.add.at(res["playoffs"], seeds[:, k], 1)
        rnd = seeds
        for r in range(3):
            pairs = [(0, 7), (3, 4), (2, 5), (1, 6)] if r == 0 else [(0, 1), (2, 3)] if r == 1 else [(0, 1)]
            nxt = []
            for a, b in pairs:
                ta, tb = rnd[:, a], rnd[:, b]
                better = W[np.arange(n), ta] >= W[np.arange(n), tb]
                hi_t, lo_t = np.where(better, ta, tb), np.where(better, tb, ta)
                p = P[hi_t, lo_t]
                if series_state:
                    for (h_, l_), (wh, wl) in series_state.items():
                        m = (hi_t == IDX[h_]) & (lo_t == IDX[l_])
                        if m.any():
                            p = np.where(m, series_prob(sp[IDX[h_]], sp[IDX[l_]], hca, sigma, wh, wl), p)
                        m2 = (hi_t == IDX[l_]) & (lo_t == IDX[h_])
                        if m2.any():
                            p = np.where(m2, 1 - series_prob(sp[IDX[h_]], sp[IDX[l_]], hca, sigma, wh, wl), p)
                nxt.append(np.where(rng.random(n) < p, hi_t, lo_t))
            rnd = np.column_stack(nxt)
            if r == 1:
                for k in range(2):
                    np.add.at(res["conf_finals"], rnd[:, k], 1)
        conf_champs.append(rnd[:, 0])
        np.add.at(res["finals"], rnd[:, 0], 1)
    e, w = conf_champs
    better = W[np.arange(n), e] >= W[np.arange(n), w]
    hi_t, lo_t = np.where(better, e, w), np.where(better, w, e)
    champ = np.where(rng.random(n) < P[hi_t, lo_t], hi_t, lo_t)
    np.add.at(res["title"], champ, 1)
    return {t: {k: float(res[k][i] / n) for k in res} for i, t in enumerate(TEAMS)}
