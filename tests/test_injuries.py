from nba import injuries
from nba import injury_alert as ia

ROW = {"player_id": "1", "name": "Star One", "team": "BOS", "status": "questionable", "detail": "ankle"}


def test_first_snapshot_is_silent_then_diffs(conn):
    assert injuries.snapshot(conn, [ROW]) == []
    assert injuries.snapshot(conn, [ROW]) == []                       # same list twice: nothing new
    ch = injuries.snapshot(conn, [{**ROW, "status": "out"}])
    assert [(c["old"], c["new"]) for c in ch] == [("questionable", "out")]
    ch = injuries.snapshot(conn, [])
    assert [(c["old"], c["new"]) for c in ch] == [("out", "available")]
    assert len(injuries.pending_changes(conn)) == 2


def test_one_alert_per_change(conn):
    injuries.snapshot(conn, [])
    injuries.snapshot(conn, [{**ROW, "status": "out"}])
    pend = injuries.pending_changes(conn)
    injuries.mark(conn, [c["id"] for c in pend], 2)
    assert injuries.pending_changes(conn) == []                       # a second check finds nothing to alert


def test_relevance_and_threshold():
    odds = {t: 0.01 for t in "ATL BKN CHA CHI CLE DAL DEN DET GS HOU".split()}
    odds["BOS"] = 0.2
    values = {"1": {"team": "BOS", "value": 7}, "2": {"team": "BOS", "value": 1}, "3": {"team": "BOS", "value": 2},
              "4": {"team": "BOS", "value": 3}}
    ch = [{"player_id": "1", "team": "BOS"}, {"player_id": "2", "team": "BOS"}, {"player_id": "9", "team": "ATL"}]
    assert [c["player_id"] for c in ia.relevant(ch, odds, values)] == ["1"]
    assert ia.should_alert(0.012, None) and ia.should_alert(0.0, -0.06) and not ia.should_alert(0.005, 0.02)
