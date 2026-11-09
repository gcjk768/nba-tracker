from nba import espn
from conftest import load


def test_scoreboard_final_games():
    games = espn.parse_scoreboard(load("scoreboard_20261108.json"))
    assert len(games) == 7
    # fixture saved 12:24 SGT while 2 games were still in the 4th quarter
    assert [g["status"] for g in games].count("final") == 5
    assert all(g["status"] in ("final", "live") and g["date"] == "2026-11-08" for g in games)
    assert all(g["home_pts"] is None for g in games if g["status"] == "live")
    assert all(g["home"] in espn.TEAM_IDS and g["away"] in espn.TEAM_IDS for g in games)


def test_next_slate_scheduled():
    games = espn.parse_scoreboard(load("scoreboard_20261109.json"))
    assert len(games) == 8 and all(g["status"] == "scheduled" and g["home_pts"] is None for g in games)


def test_summary():
    s = espn.parse_summary(load("summary_401909945.json"))
    g = s["game"]
    assert (g["home"], g["away"], g["home_pts"], g["away_pts"]) == ("CHI", "PHI", 121, 127)
    assert {t["team"] for t in s["team_box"]} == {"CHI", "PHI"}
    phi = next(t for t in s["team_box"] if t["team"] == "PHI")
    assert phi["fga"] == 99 and phi["oreb"] == 20 and 90 < phi["poss"] < 115
    played = [p for p in s["player_box"] if not p["dnp"]]
    assert sum(p["starter"] for p in played) == 10
    assert sum(p["pts"] for p in played if p["team"] == "PHI") == 127
    assert len(s["plays"]) == 476 and all(p["home_wp"] is not None for p in s["plays"])
    assert s["plays"][-1]["home_wp"] == 0.0
    assert s["odds"]["spread"] == 9.5


def test_injuries():
    rows = espn.parse_injuries(load("injuries.json"))
    assert len(rows) > 50
    assert {r["status"] for r in rows} <= {"out", "questionable", "doubtful", "probable", "suspended"}
    assert all(r["team"] in espn.TEAM_IDS and r["player_id"].isdigit() for r in rows)


def test_futures_and_schedule():
    f = espn.parse_futures(load("futures.json"))
    assert len(f) == 30 and min(f, key=f.get) == "SA"
    sched = espn.parse_team_schedule(load("schedule_BOS.json"))
    assert len(sched) >= 80 and all("BOS" in (g["home"], g["away"]) for g in sched)


def test_helpers():
    assert espn.clock_seconds("10:34") == 634 and espn.clock_seconds("45.3") == 45.3
    assert espn.team_code("gsw") == "GS" and espn.team_code("Celtics") == "BOS" and espn.team_code("xx") is None
    assert espn.injury_status("Day-To-Day", "X is doubtful for Monday") == "doubtful"
    assert espn.injury_status("Out", "") == "out"
