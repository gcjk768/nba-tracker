import json
import re

from nba import jobs, web
from test_jobs import ctx  # noqa: F401  (fixture)


def test_dashboard_data_and_render(ctx):
    jobs.daily(ctx)
    d = web.data(ctx)
    assert d["title"]["dates"] == ["2026-11-09"] and len(d["rankings"]) == 30
    assert len(d["tonight"]) == 8 and d["last_night"]["games"]
    html = web.render(d)
    blob = re.search(r"const D = (.*?);\n", html).group(1)
    assert json.loads(blob)["season"] == d["season"]
    assert "/*__DATA__*/" not in html and "</script" not in blob


def test_history_fills_past_days(ctx):
    jobs.daily(ctx)
    ctx.now = lambda: __import__("datetime").datetime(2026, 11, 12, 7, 15, tzinfo=__import__("datetime").timezone.utc)
    n = jobs.rebuild_history(ctx, n=500)
    dates = [r[0] for r in ctx.conn.execute("select distinct date from sims where kind='daily' order by date")]
    assert n == 2 and dates == ["2026-11-09", "2026-11-10", "2026-11-11"]
    assert jobs.rebuild_history(ctx, n=500) == 0                         # idempotent


def test_each_conference_filter_is_scoped_to_its_section():
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent.joinpath("nba", "dashboard.html").read_text(encoding="utf-8")
    assert 'querySelectorAll(".seg button")' not in html          # a page-wide selector made two filters fight
    assert '#s-styles .seg button' in html and '#pbSeg button' in html


def test_reveal_observers_do_not_use_ratio_thresholds_on_tall_sections():
    # a ratio threshold never fires on a section taller than viewport / threshold (the 30-card playbook grid)
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent.joinpath("nba", "dashboard.html").read_text(encoding="utf-8")
    assert "{threshold: .08}" not in html and "{threshold: .05}).observe(box)" not in html


def test_cup_table_ranks_groups_and_flags_wild_cards(conn):
    def g(gid, home, away, hp=None, ap=None):
        conn.execute("""insert into games(game_id, date, season, season_type, home, away, tip_utc, status, home_pts, away_pts,
                        source, note) values (?, '2026-11-13', 2027, 2, ?, ?, '2026-11-14T00:00Z', ?, ?, ?, ?, 'NBA Cup - Group Play')""",
                     (gid, home, away, "final" if hp else "scheduled", hp, ap, "espn" if hp else None))
    east1, east2 = ["ATL", "BOS", "CHA", "CHI", "WSH"], ["CLE", "IND", "MIA", "NY", "PHI"]
    for grp in (east1, east2):                                           # a full round robin, nothing played yet
        for i, h in enumerate(grp):
            for a in grp[i + 1:]:
                g(f"{h}{a}", h, a)
    g("x1", "MIL", "TOR")                                                # third East group, linked by one game
    assert all(r["w"] + r["l"] == 0 and not r["flag"] for gr in web.cup_table(conn, 2027)["groups"] for r in gr["rows"])
    conn.execute("update games set status='final', source='espn', home_pts=110, away_pts=100 where game_id in ('ATLBOS','CLEIND','BOSCHA')")
    conn.execute("update games set status='final', source='espn', home_pts=101, away_pts=100 where game_id='ATLCHA'")
    conn.execute("update games set status='final', source='espn', home_pts=110, away_pts=100 where game_id='CHIWSH'")
    conn.execute("update games set home_pts=140 where game_id='BOSCHA'")       # BOS 1-1 +30 vs CHI 1-0 +10
    cup = web.cup_table(conn, 2027)
    assert [gr["name"] for gr in cup["groups"]][:2] == ["East · group 1", "East · group 2"] and len(cup["groups"]) == 3
    a = cup["groups"][0]["rows"]
    assert [r["team"] for r in a[:3]] == ["ATL", "CHI", "BOS"]               # by win %: CHI 1-0 above BOS 1-1
    assert a[0]["flag"] == "leads group" and (a[0]["w"], a[0]["l"]) == (2, 0) and a[0]["pd"] == 11 and a[0]["left"] == 2
    assert a[1]["flag"] == "wild card"                                  # CHI 1-0 beats every other East runner-up
    assert cup["nights"] == ["2026-11-13"]
