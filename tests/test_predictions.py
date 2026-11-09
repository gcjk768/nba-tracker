from datetime import datetime, timedelta, timezone

import pytest

from nba import db
from nba import predictions as P
from nba import scorecard
from nba.tuning import Params

NOW = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)


def _game(conn, gid="g1", tip=NOW + timedelta(hours=2), final=None):
    db.upsert(conn, "games", {"game_id": gid, "date": "2026-11-09", "season": 2027, "season_type": 2, "home": "BOS",
                              "away": "NY", "tip_utc": tip.isoformat(), "status": "final" if final else "scheduled",
                              "home_pts": final[0] if final else None, "away_pts": final[1] if final else None})


def test_refuses_after_tip(conn):
    _game(conn)
    with pytest.raises(P.TipPassed):
        P.store(conn, "g1", {"home_wp": 0.6, "margin": 3, "total": 220}, now=NOW + timedelta(hours=2))


def test_graded_is_last_before_tip(conn):
    _game(conn)
    assert P.store(conn, "g1", {"home_wp": 0.60, "margin": 3, "total": 220}, now=NOW) == 1
    assert P.store(conn, "g1", {"home_wp": 0.601, "margin": 3, "total": 220}, now=NOW) is None   # no real change
    assert P.store(conn, "g1", {"home_wp": 0.45, "margin": -1, "total": 220}, now=NOW + timedelta(hours=1)) == 2
    assert P.graded(conn, "g1")["version"] == 2


def test_project_and_market():
    p = Params()
    r = {"net": 5.0, "off": 3.0, "def": 2.0, "pace": 100.0}
    a = {"net": 0.0, "off": 0.0, "def": 0.0, "pace": 100.0}
    pr = P.project(r, a, 112.0, p)
    assert abs(pr["margin"] - (5 + p.hca)) < 1e-9 and pr["home_wp"] > 0.6
    assert abs(pr["total"] - (112 + 3 + 112 - 2)) < 1e-9
    assert abs(P.market_wp(-150, 130) - (0.6 / (0.6 + 100 / 230))) < 1e-9
    assert P.project(r, a, 112.0, p, b2b_h=True)["margin"] < pr["margin"]


def test_scorecard(conn):
    for i, (wp, res) in enumerate([(0.8, (110, 100)), (0.7, (100, 110)), (0.4, (100, 110))]):
        gid = f"g{i}"
        _game(conn, gid, final=None)
        P.store(conn, gid, {"home_wp": wp, "margin": 0, "total": 220}, now=NOW)
        conn.execute("update games set status='final', home_pts=?, away_pts=? where game_id=?", (*res, gid))
    s = scorecard.grade(conn)
    assert s["n"] == 3 and s["wins"] == 2
    assert abs(s["brier"] - ((0.2 ** 2 + 0.7 ** 2 + 0.4 ** 2) / 3)) < 1e-9
    assert sum(b["n"] for b in s["calibration"]) == 3
