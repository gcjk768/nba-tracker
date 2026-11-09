import itertools
import random
from datetime import datetime, timedelta, timezone

import pandas as pd

from nba import db
from nba import predictions as P
from nba import ratings
from nba import review
from nba import tuning


def _season(true_hca, seed=3, reps=2):
    rnd = random.Random(seed)
    strength = {t: rnd.gauss(0, 4) for t in ratings.TEAMS}
    rows, gid = [], 0
    pairs = list(itertools.permutations(ratings.TEAMS, 2)) * reps
    rnd.shuffle(pairs)
    start = datetime(2026, 10, 20)
    for i, (h, a) in enumerate(pairs):
        m = strength[h] - strength[a] + true_hca + rnd.gauss(0, 12)
        gid += 1
        rows.append({"game_id": str(gid), "date": (start + timedelta(days=i // 15)).date().isoformat(), "home": h,
                     "away": a, "home_pts": 110 + m / 2, "away_pts": 110 - m / 2, "home_poss": 100.0, "away_poss": 100.0})
    return pd.DataFrame(rows)


PRIOR = {t: (0.0, 0.0) for t in ratings.TEAMS}


def test_tune_adopts_better_hca(conn):
    df = _season(5.0)
    cur = tuning.Params(half_life=None, hca=1.5, sigma=12.0, b2b=0.0)
    rep = tuning.tune(conn, df, PRIOR, cur)
    assert rep["adopted"] and rep["params"]["hca"] > 1.5 and rep["ll_new"] < rep["ll_old"]
    assert tuning.current(conn).hca == rep["params"]["hca"]


def test_tune_keeps_model_on_noise(conn):
    df = _season(2.5, seed=11, reps=3)
    rep = tuning.tune(conn, df, PRIOR, tuning.Params(half_life=None, hca=2.5, sigma=12.0, b2b=0.0))
    assert not rep["adopted"], rep


def test_aar_causes_sum_to_error(conn):
    now = datetime.now(timezone.utc)
    tip = (now + timedelta(hours=1)).isoformat()
    for gid, d, h, a, hp, ap in [("1", "2026-11-01", "BOS", "NY", 110, 100), ("2", "2026-11-02", "NY", "BOS", 100, 104),
                                 ("3", "2026-11-05", "BOS", "NY", 95, 112)]:
        db.upsert(conn, "games", {"game_id": gid, "date": d, "season": 2027, "season_type": 2, "home": h, "away": a,
                                  "tip_utc": tip, "status": "final", "home_pts": hp, "away_pts": ap, "source": "espn"})
        for team, opp, home, pts, f3m in ((h, a, 1, hp, 12 if gid != "3" else 6), (a, h, 0, ap, 12 if gid != "3" else 20)):
            db.upsert(conn, "team_box", {"game_id": gid, "team": team, "opp": opp, "is_home": home, "pts": pts,
                                         "fgm": 40, "fga": 88, "fg3m": f3m, "fg3a": 35, "ftm": 15, "fta": 20, "oreb": 10,
                                         "dreb": 33, "ast": 25, "stl": 7, "blk": 5, "tov": 13, "pf": 19, "poss": 98.0})
    P.store(conn, "3", {"home_wp": 0.70, "margin": 6.0, "total": 220}, now=now)
    r = review.review_slate(conn, ["3"])[0]
    assert r["missed"] and r["pick"] == "BOS"
    assert abs(sum(r["causes"].values()) - r["error"]) < 0.05
    assert r["main"] == "shooting luck"
