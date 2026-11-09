from collections import defaultdict

from nba import collector, insights, lineups
from test_collector import FakeClient


def _load(conn):
    collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    conn.execute("delete from games where game_id != '401909945'")


def test_win_recipe_sums_to_the_real_margin(conn):
    _load(conn)
    for r in insights._game_rows_one(conn, "401909945"):
        parts = insights.margin_parts(r)
        margin = (r["home_pts"] - r["away_pts"]) * (1 if r["is_home"] else -1)
        assert abs(sum(parts.values()) - margin) < 1e-6, (parts, margin)


def test_lineup_time_adds_up_to_the_game(conn):
    _load(conn)
    S, cov = lineups.stints(conn, 2027)
    sec = defaultdict(float)
    for s in S:
        sec[s["team"]] += s["sec"]
        assert len(s["unit"]) == 5
    assert all(abs(v - 2880) < 1 for v in sec.values()), dict(sec)
    assert cov["share_kept"] if "share_kept" in cov else cov["seconds_kept"] / cov["seconds_total"] > 0.95


def test_schedule_context_and_distance():
    assert 1300 < insights.km("BOS", "CHI") < 1450          # great-circle ~1,370 km
    assert insights.ARENA["DEN"][3] > 1500
