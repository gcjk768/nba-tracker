import pandas as pd
from nba import elo


def test_prob_and_zero_sum():
    assert abs(elo.prob(1500, 1500) - 0.640) < 0.001
    h, a = elo.update(1500, 1500, 110, 100)
    assert h > 1500 > a and abs((h - 1500) - (1500 - a)) < 1e-9


def test_carry_and_replay():
    assert elo.carry({"BOS": 1705})["BOS"] == 0.75 * 1705 + 0.25 * 1505
    res = pd.DataFrame([{"game_id": "1", "date": "2026-10-21", "home": "BOS", "away": "NY", "home_pts": 120, "away_pts": 100}])
    r, pre = elo.replay({"BOS": 1500, "NY": 1500}, res, carry_first=False)
    assert pre == [("1", 1500, 1500)] and r["BOS"] > r["NY"]


def test_seed_maps_codes():
    arc = pd.DataFrame([{"date": "2024-04-14", "season": 2024, "team1": "GSW", "elo1_post": 1600.0},
                        {"date": "2024-04-01", "season": 2024, "team1": "GSW", "elo1_post": 1550.0}])
    assert elo.seed_from_archive(arc, 2024) == {"GS": 1600.0}
