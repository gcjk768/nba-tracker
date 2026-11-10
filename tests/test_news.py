import json
import subprocess

from nba import news
from conftest import load

RSS = """<rss><channel><item><title>Celtics star ruled out vs. Sixers</title><link>https://x/1</link><pubDate>Mon</pubDate>
<description>&lt;p&gt;Jayson Tatum will sit with a sore ankle.&lt;/p&gt;</description></item>
<item><title>Power rankings week 3</title><link>https://x/2</link></item></channel></rss>"""


def fake_claude(cmd, stdin, timeout):
    items = json.loads(stdin)["items"]
    out = []
    for it in items:
        if "ruled out" in it["title"]:
            out.append({"i": it["i"], "keep": True, "kind": "injury", "teams": ["BOS"], "players": ["Jayson Tatum"],
                        "status_implied": "out", "summary": "Tatum out with an ankle issue."})
        else:
            out.append({"i": it["i"], "keep": False, "kind": "other", "teams": [], "players": [], "status_implied": None, "summary": ""})
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps({"items": out})}), "")


class FakeHTTP:
    def get(self, url, **k):
        class R:
            text = RSS
            def json(self_inner):
                return {"articles": [{"type": "Recap", "headline": "Bulls lose", "categories": []},
                                     {"type": "Story", "headline": "Spurs sign veteran guard", "categories": [], "links": {"web": {"href": "u"}}}]}
        return R()


def test_parsers_skip_recaps_and_strip_html():
    assert [x["title"] for x in news.from_rss(RSS)] == ["Celtics star ruled out vs. Sixers", "Power rankings week 3"]
    assert "<p>" not in news.from_rss(RSS)[0]["text"]
    arts = news.from_espn_news(FakeHTTP().get("x").json())
    assert [a["title"] for a in arts] == ["Spurs sign veteran guard"]
    inj = news.from_injuries(load("injuries.json"))
    assert inj and all(x["source"] == "ESPN injury report" and x["hint_team"] for x in inj)


def _game(conn, gid, date, home, away, tip, status="scheduled"):
    conn.execute("insert into games(game_id, date, home, away, tip_utc, status) values (?,?,?,?,?,?)", (gid, date, home, away, tip, status))


def test_collect_tags_flags_and_dedupes(conn):
    _game(conn, "g1", news.sgt_today(), "BOS", "PHI", "2099-01-01T00:00Z")      # BOS plays tonight
    values = {"1": {"name": "Jayson Tatum", "team": "BOS", "value": 4.2}}
    kept = news.collect(conn, "m", None, teams_tonight=["BOS"], values=values, injuries={}, runner=fake_claude, http=FakeHTTP())
    assert len(kept) == 1 and kept[0]["ahead"] == 1 and kept[0]["impact"] == 4.2 and kept[0]["tonight"] == 1
    again = news.collect(conn, "m", None, teams_tonight=["BOS"], values=values, injuries={}, runner=fake_claude, http=FakeHTTP())
    assert again == []                                                       # same items: nothing new
    push = news.to_push(conn, contenders={"BOS"}, top_players={"BOS": {"jayson tatum"}})
    assert len(push) == 1
    news.mark_pushed(conn, [push[0]["id"]])
    assert news.to_push(conn, contenders={"BOS"}, top_players={"BOS": {"jayson tatum"}}) == []


def test_spoiler_guard_and_subject_only():
    assert "hidden" in news.spoiler_safe("Doncic rested in Lakers' win over Pistons; Reaves scored 43 in the 121-116 victory.", "rest", ["Luka Doncic"])
    assert news.spoiler_safe("Jokic has an elbow sprain and may miss Monday.", "injury", ["Nikola Jokic"]).startswith("Jokic")


def test_recent_drops_news_the_player_has_played_through_and_recomputes_tonight(conn):
    ins = """insert into news_items(key, ts, published, source, title, summary, teams, players, kind, status_implied, tonight, kept)
             values (?,?,?,?,?,?,?,?,?,?,1,1)"""
    news.recent(conn)                                                          # creates the table
    now = news._now()
    conn.execute(ins, ("a", now, "2026-11-08T18:19Z", "ESPN", "Luka Doncic (LAL): Out", "Out vs DET.", '["LAL"]', '["Luka Dončić"]', "rest", "out"))
    conn.execute(ins, ("b", now, "2026-11-08T18:00Z", "ESPN", "Tatum out", "Ankle.", '["BOS"]', '["Jayson Tatum"]', "injury", "out"))
    _game(conn, "atl", "2026-11-09", "ATL", "LAL", "2026-11-10T00:00Z", "final")
    conn.execute("insert into player_box(game_id, team, player_id, name, minutes, dnp) values ('atl','LAL','1','Luka Doncic',36,0)")
    rows = news.recent(conn, today="2026-11-10")
    assert [r["key"] for r in rows] == ["b"]                                   # Luka played after the item: gone
    assert rows[0]["tonight"] == 0                                             # stored flag ignored: BOS not on today's slate
    _game(conn, "bos", "2026-11-10", "BOS", "NY", "2026-11-11T00:30Z")
    assert news.recent(conn, today="2026-11-10")[0]["tonight"] == 1
