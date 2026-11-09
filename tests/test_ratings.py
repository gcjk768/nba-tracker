import itertools
import random

import pandas as pd

from nba import ratings
from nba.tuning import Params


def test_blend_weight():
    assert ratings.blend_weight(0) == 0
    assert abs(ratings.blend_weight(10) - 0.30) < 1e-9
    assert abs(ratings.blend_weight(55) - 0.80) < 1e-9
    assert ratings.blend_weight(80) == 0.80


def _league(strength, hca_pts=3.0, seed=1, reps=3):
    rnd = random.Random(seed)
    rows, gid = [], 0
    for _ in range(reps):
        for h, a in itertools.permutations(ratings.TEAMS, 2):
            m = strength.get(h, 0) - strength.get(a, 0) + hca_pts + rnd.gauss(0, 6)
            gid += 1
            rows.append({"game_id": str(gid), "date": "2026-11-01", "home": h, "away": a,
                         "home_pts": 110 + m / 2, "away_pts": 110 - m / 2, "home_poss": 100.0, "away_poss": 100.0})
    return pd.DataFrame(rows)


def test_fit_recovers_strength_and_hca():
    df = _league({"BOS": 10})
    off, de, hca, mu = ratings.fit(df, lam=1.0)
    net = {t: off[t] + de[t] for t in ratings.TEAMS}
    assert max(net, key=net.get) == "BOS" and 8 < net["BOS"] - sum(net.values()) / 30 < 12
    assert 2 < hca < 4


def test_current_blends_with_prior():
    df = _league({"BOS": 10}, reps=1)
    prior = {t: (0.0, 0.0) for t in ratings.TEAMS}
    prior["NY"] = (5.0, 5.0)
    cur = ratings.current(df, prior, Params(), "2026-11-02")
    base = ratings.current(df, {t: (0.0, 0.0) for t in ratings.TEAMS}, Params(), "2026-11-02")
    assert cur["BOS"]["blend_w"] == 0.80                     # 58 games each in this synthetic league
    assert abs((cur["NY"]["net"] - base["NY"]["net"]) - 0.2 * 10) < 1e-9   # prior still counts 20%


def test_availability():
    values = {"1": {"team": "BOS", "value": 6.0, "frac": 1.0}, "2": {"team": "BOS", "value": 4.0, "frac": 0.5}}
    assert ratings.availability_delta("BOS", values, {"1": {"status": "out"}}) == -6.0
    assert ratings.availability_delta("BOS", values, {}, healthy=True) == 2.0
    assert ratings.availability_delta("BOS", values, {"2": {"status": "out", "detail": "knee, out for season"}}, healthy=True) == 0.0
