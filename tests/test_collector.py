from nba import collector
from nba import espn
from conftest import load


class FakeClient:
    def scoreboard(self, d):
        return load("scoreboard_20261108.json")

    def summary(self, gid):
        j = load("summary_401909945.json")          # same box for every game id, re-keyed
        j["header"]["id"] = gid
        return j


class DeadClient:
    def scoreboard(self, d):
        raise RuntimeError("blocked")

    def summary(self, gid):
        raise RuntimeError("blocked")


class FakeHoopr:
    def __init__(self):
        self.calls = 0

    def games_for_date(self, season, d):
        self.calls += 1
        s = espn.parse_summary(load("summary_401909945.json"))
        return [{"game": s["game"], "team_box": s["team_box"], "player_box": s["player_box"], "plays": [], "odds": {}}]


def test_collect_idempotent(conn):
    r1 = collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    r2 = collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    assert len(r1.new_games) == 5 and r2.new_games == []
    assert conn.execute("select count(*) from games where source='espn'").fetchone()[0] == 5
    assert conn.execute("select count(*) from games").fetchone()[0] == 7          # 2 live rows kept as schedule
    assert conn.execute("select count(*) from team_box").fetchone()[0] == 10
    assert conn.execute("select count(*) from game_excitement").fetchone()[0] == 5


def test_fallback_when_espn_dead(conn):
    h = FakeHoopr()
    r = collector.collect_date(conn, DeadClient(), "2026-11-08", 2027, hoopr=h)
    assert h.calls == 1 and r.new_games == ["401909945"]
    assert "hoopR" in r.warnings[0]
    assert conn.execute("select source from games").fetchone()[0] == "hoopr"


def test_schedule_never_clobbers_final(conn):
    collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    g = espn.parse_summary(load("summary_401909945.json"))["game"]
    collector.upsert_schedule_row(conn, {**g, "status": "scheduled", "home_pts": None})
    row = conn.execute("select status, home_pts from games where game_id='401909945'").fetchone()
    assert tuple(row) == ("final", 121)


def test_storing_the_box_keeps_the_cup_note(conn):
    parsed = espn.parse_summary(load("summary_401909945.json"))         # ESPN summaries carry no notes
    collector.upsert_schedule_row(conn, {**parsed["game"], "status": "scheduled", "note": "NBA Cup - Group Play"})
    collector.store_game(conn, parsed, "espn")
    assert conn.execute("select note from games where game_id='401909945'").fetchone()[0] == "NBA Cup - Group Play"


def test_collect_tags_cup_games_new_and_already_stored(conn):
    sb = load("scoreboard_20261108.json")
    ev = sb["events"][0]
    ev["competitions"][0]["notes"] = [{"headline": "NBA Cup - Group Play"}]

    class Cup(FakeClient):
        def scoreboard(self, d):
            return sb
    collector.collect_date(conn, Cup(), "2026-11-08", 2027)                   # new final: note comes from the scoreboard
    q = "select note from games where game_id=?"
    assert conn.execute(q, (ev["id"],)).fetchone()[0] == "NBA Cup - Group Play"
    conn.execute("update games set note=null")                                 # a final stored before the fix
    collector.collect_date(conn, Cup(), "2026-11-08", 2027)
    assert conn.execute(q, (ev["id"],)).fetchone()[0] == "NBA Cup - Group Play"
