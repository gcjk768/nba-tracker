from nba import espn
from nba import excitement as ex
from conftest import load


def _game():
    return espn.parse_summary(load("summary_401909945.json"))


def test_wpa_sums_to_winner_change():
    s = _game()
    g, plays = s["game"], s["plays"]
    w = ex.wpa(plays, g["home"], g["away"])
    team_of = {p["player_id"]: p["team"] for p in s["player_box"]}
    winner = g["away"]                                    # PHI won 127-121 on the road
    side = lambda k: k[5:] if k.startswith("TEAM:") else team_of.get(k)
    net = sum(v if side(k) == winner else -v for k, v in w.items())
    wps = [p["home_wp"] for p in plays]
    expected = (1 - wps[-1]) - (1 - wps[0])
    assert abs(net - expected) < 1e-9
    assert all(side(k) in (g["home"], g["away"]) for k in w)


def test_score_reasons_are_spoiler_free():
    s = _game()
    best = max(ex.game_score(p) for p in s["player_box"] if not p["dnp"])
    sc, stars, reasons = ex.score(s["plays"], s["game"]["ot"], best)
    assert 0 <= sc <= 100 and 1 <= stars <= 5
    assert set(reasons) <= set(ex.REASONS)


def test_garbage_time():
    mk = lambda per, clk, h, a: {"period": per, "clock_s": clk, "home_score": h, "away_score": a, "home_wp": 0.5}
    plays = [mk(4, 600, 90, 70), mk(4, 290, 100, 79), mk(4, 100, 105, 80)]
    assert ex.garbage_start(plays) == 1
    plays.append(mk(4, 60, 105, 97))                      # comeback under 10 cancels it
    assert ex.garbage_start(plays) is None
    h, a, frac = ex.clean_split(plays[:3], 0)
    assert (h, a) == (100, 79) and 0.89 < frac < 0.91
