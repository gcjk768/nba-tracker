import json

from nba import game_log, social_intel as si
from test_film import _setup, claude_ok

ARTICLE = ("Philadelphia leaned on horns sets for Embiid and Chicago spent long stretches in a 2-3 zone. " * 30)


def test_game_log_measures_each_game_and_attaches_only_that_games_reports(conn):
    _setup(conn)                                                   # one real game: CHI vs PHI, 2026-11-08
    gid = conn.execute("select game_id from games").fetchone()[0]
    conn.execute("""insert into film_items(game_id, date, team, kind, label, summary, url) values
                    (?, '2026-11-08', 'PHI', 'coverage', '2-3 zone', 'zone late', 'https://a/1'),
                    (?, '2026-11-08', 'PHI', 'set', 'horns', 'horns for Embiid', 'https://a/1'),
                    ('', '2026-11-01', 'PHI', 'coverage', 'drop', 'season piece', 'https://a/2')""", (gid, gid))
    G = game_log.build(conn, 2027, teams=("PHI",))["PHI"]
    g = G["games"][0]
    assert len(G["games"]) == 1 and g["result"] in "WL" and g["offense"]["plays"] and g["defense"]["allowed"]
    labels = {x["label"]: x["source"] for x in g["defense"]["schemes"]}
    assert list(labels)[0] == "man-to-man" and labels["2-3 zone"] == "this game"
    assert labels["drop"] in ("season", "inferred")                # season piece is labelled season, never "this game"
    assert [(x["label"], x["source"]) for x in g["offense"]["sets"]] == [("horns", "this game")]
    assert all(x["counters"] for x in g["defense"]["schemes"]) and G["def_counts"][0] == ("man-to-man", 1)


class FakeWeb:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd[2])
        if cmd[2] == "search":
            return 0, json.dumps({"ok": True, "items": [
                {"title": "How the 76ers beat Chicago's zone", "url": "https://blog/phi-chi", "published_at": "2026-11-09T03:00:00Z"},
                {"title": "76ers season preview", "url": "https://blog/old", "published_at": "2026-10-01T03:00:00Z"},
                {"title": "76ers vs Bulls box score", "url": "https://www.basketball-reference.com/x", "published_at": "2026-11-09"}]})
        return 0, json.dumps({"ok": True, "items": [{"text": ARTICLE}]})


def test_game_articles_tie_labels_to_the_game_and_search_each_game_once(conn):
    _setup(conn)
    web = FakeWeb()
    res = si.game_articles(conn, "2026-11-10", "m", ("PHI",), 2027, reach=si.Reach(web, sleep=lambda s: None), claude_runner=claude_ok)
    rows = {(r["game_id"] != "", r["team"], r["label"]) for r in conn.execute("select * from film_items")}
    assert rows == {(True, "PHI", "horns"), (True, "CHI", "switch")} and res["stop"] is None
    assert web.calls == ["search", "read"]                         # old preview and box-score page never read
    again = si.game_articles(conn, "2026-11-10", "m", ("PHI",), 2027, reach=si.Reach(web, sleep=lambda s: None), claude_runner=claude_ok)
    assert again["games"] == 0 and web.calls == ["search", "read"]


def test_play_names_are_normalised_and_land_on_the_right_side():
    assert si.normalise("Horn", "set") == ("horns", "set")
    assert si.normalise("back door", "action") == ("backdoor cuts", "action")
    assert si.normalise("ball screen", "action") == ("pick-and-roll", "action")
    assert si.normalise("scramble", "action") == ("scramble", "coverage")          # a defensive rotation, not an offensive set
    assert si.normalise("2-3", None) == ("2-3 zone", "coverage")
    assert si.normalise("vibes", "mood") == ("vibes", None)
