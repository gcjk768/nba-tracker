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
    conn.execute("""insert into film_items(game_id, date, team, kind, label, summary, url) values ('other', '2026-11-01', 'PHI', 'set', 'floppy', 'x', 'u')""")
    assert "floppy" not in [x["label"] for x in game_log.build(conn, 2027, teams=("PHI",))["PHI"]["games"][0]["offense"]["sets"]]   # another game's set
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


def claude_impact(cmd, stdin, timeout):
    import subprocess
    out = {"items": [{"team": "PHI", "label": "Horn", "kind": "set", "impact": "worked", "effect": "Embiid scored on it three times late",
                      "who": "", "summary": "Philly ran horns."},
                     {"team": "CHI", "label": "switch", "kind": "coverage", "impact": "maybe", "effect": "", "who": "", "summary": "Chicago switched."}]}
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(out)}), "")


def test_game_articles_tie_labels_to_the_game_and_search_each_game_once(conn):
    _setup(conn)
    web = FakeWeb()
    res = si.game_articles(conn, "2026-11-10", "m", ("PHI",), 2027, reach=si.Reach(web, sleep=lambda s: None), claude_runner=claude_impact)
    rows = {(r["game_id"] != "", r["team"], r["label"], r["impact"], r["effect"]) for r in conn.execute("select * from film_items")}
    assert rows == {(True, "PHI", "horns", "worked", "Embiid scored on it three times late"), (True, "CHI", "switch", "", "")}
    assert res["stop"] is None and web.calls == ["search", "search", "read"]   # two angles; old preview and box-score page never read
    again = si.game_articles(conn, "2026-11-10", "m", ("PHI",), 2027, reach=si.Reach(web, sleep=lambda s: None), claude_runner=claude_ok)
    assert again["games"] == 0 and web.calls == ["search", "search", "read"]
    gid = conn.execute("select game_id from games").fetchone()[0]           # a re-read refreshes impact instead of duplicating
    si._store(conn, gid, "2026-11-08", {"team": "PHI", "kind": "set", "label": "horns", "who": "", "summary": "s", "title": "t",
                                        "impact": "failed", "effect": "stopped"}, "https://blog/phi-chi", "blog")
    assert conn.execute("select count(*), max(impact) from film_items where label='horns'").fetchone()[:] == (1, "failed")


def test_play_names_are_normalised_and_land_on_the_right_side():
    assert si.normalise("Horn", "set") == ("horns", "set")
    assert si.normalise("back door", "action") == ("backdoor cuts", "action")
    assert si.normalise("ball screen", "action") == ("pick-and-roll", "action")
    assert si.normalise("scramble", "action") == ("scramble", "coverage")          # a defensive rotation, not an offensive set
    assert si.normalise("2-3", None) == ("2-3 zone", "coverage")
    assert si.normalise("vibes", "mood") == ("vibes", None)
    assert si.normalise("two-man game", "action")[1] is None                       # vague: could be PnR, handoff or iso


def test_tracked_teams_grow_one_at_a_time_by_rating_up_to_the_cap(conn):
    assert game_log.tracked(conn) == list(game_log.FOCUS)
    for i, t in enumerate(["BOS", "LAL", "DEN", "NY"]):            # LAL already pinned: skipped
        conn.execute("insert into ratings(date, team, off, def, net, healthy_net) values ('2026-11-10', ?, 0, 0, 0, ?)", (t, 10 - i))
    assert game_log.grow(conn) == "BOS" and game_log.grow(conn) == "DEN"
    assert game_log.tracked(conn) == list(game_log.FOCUS) + ["BOS", "DEN"]
    game_log.MAX_TEAMS, cap = 6, game_log.MAX_TEAMS
    try:
        assert game_log.grow(conn) is None                         # at the cap
    finally:
        game_log.MAX_TEAMS = cap


def test_horns_variants_and_systems_keep_their_specific_names():
    from nba import playbook_kb as kb
    assert si.normalise("Horns Flare", "set") == ("horns flare", "set")
    assert si.normalise("horn twist", "action") == ("horns twist", "set")
    assert si.normalise("Triangle offense", "system") == ("triangle", "system")
    assert si.normalise("Princeton", "system") == ("princeton", "system")
    assert si.normalise("5-out", "set")[1] is None          # a one-lineup look is not the team's system
    assert si.normalise("spain action", "action") == ("spain pick-and-roll", "set")
    assert all(k in kb.SET_TO_PLAYS for k, v in kb.PLAYS.items() if k.startswith("horns ") or v[0] == "system")
