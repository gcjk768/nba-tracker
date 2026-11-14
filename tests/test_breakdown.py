import json
import subprocess

from nba import breakdown as bd
from test_film import _setup


def _reply(cmd, obj):
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(obj)}), "")


def staff(calls, offence=None, coach=None):
    """Fake claude for the four agents. The role is read from the system prompt; each quotes a number from its own facts."""
    def runner(cmd, stdin, timeout):
        prompt, facts = cmd[cmd.index("--system-prompt") + 1], json.loads(stdin)
        role = next(r for r in ("offence", "defence", "playbook", "player", "head coach") if r in prompt.split(".")[0])
        if role == "playbook":
            calls.append((role, facts))
            x = facts["named_plays_and_coverages"][0]
            return _reply(cmd, {"headline": f"{facts['team']} lean on {x['name']}", "notes": [
                {"point": f"{x['name']} is working.", "evidence": f"reported in {x['times_reported']} breakdown", "effect": "helped"},
                {"point": "They score 999 a night.", "evidence": "999 points", "effect": "helped"}]})
        calls.append((role, facts))
        if role == "head coach":
            return _reply(cmd, coach(facts) if coach else {
                "headline": f"{facts['team']} {facts['result']} {facts['score']} on shot making",
                "reasons": [{"reason": "The offence got good shots.", "evidence": facts["offence_findings"][0]["evidence"], "side": "offence"}],
                "next_time": "Keep the ball moving."})
        if role == "offence" and offence:
            return _reply(cmd, offence(facts))
        number = facts.get("points_per_shot") or facts.get("opponent_points_per_shot") or facts["players"][0]["points"]
        return _reply(cmd, {"findings": [{"point": f"The {role} did its job.", "evidence": f"{number} this game", "effect": "helped"}]})
    return runner


def test_three_specialists_then_the_coach_and_the_result_is_stored_once(conn):
    _setup(conn)                                                   # one real game: CHI at PHI, 2026-11-08
    calls = []
    res = bd.run(conn, 2027, "fast", "smart", staff(calls), teams=["PHI"])
    assert res == {"done": 1, "left": 0, "failed": 0, "stop": None}
    assert sorted(r for r, _ in calls) == ["defence", "head coach", "offence", "player"] and calls[-1][0] == "head coach"
    seen = dict(calls)
    assert "players" not in seen["offence"] and "how_the_shots_came" not in seen["player"]      # each reads its own slice
    assert seen["head coach"]["offence_findings"] and "points_of_margin_by_factor" in seen["head coach"]
    (body,) = bd.stored(conn, "PHI").values()
    assert body["headline"].startswith("PHI") and body["offence"] and body["defence"] and body["players"] and body["reasons"]
    assert bd.run(conn, 2027, "fast", "smart", staff(calls), teams=["PHI"])["done"] == 0 and len(calls) == 4   # not paid for twice
    gid = conn.execute("select game_id from games").fetchone()[0]
    conn.execute("insert into film_items(game_id, date, team, kind, label, summary, url) values (?, '2026-11-08', 'PHI', 'set', 'horns', 'x', 'u')", (gid,))
    assert bd.run(conn, 2027, "fast", "smart", staff(calls), teams=["PHI"])["done"] == 1                     # a write-up arrived: redo


def test_a_number_that_is_not_in_the_facts_is_dropped(conn):
    _setup(conn)
    made_up = lambda f: {"findings": [{"point": "They shot 99.9% at the rim.", "evidence": "99.9% at the rim", "effect": "helped"},
                                      {"point": "They got to the rim.", "evidence": f"rim share {f['rim_share_pct']}%", "effect": "helped"}]}
    lying_coach = lambda f: {"headline": "Won by 77 on pure effort", "next_time": "Score 140.",
                             "reasons": [{"reason": "Rim pressure.", "evidence": f["offence_findings"][0]["evidence"], "side": "offence"},
                                         {"reason": "They scored 55 in the paint.", "evidence": "55 paint points", "side": "offence"}]}
    calls = []
    bd.run(conn, 2027, "fast", "smart", staff(calls, made_up, lying_coach), teams=["PHI"])
    (body,) = bd.stored(conn, "PHI").values()
    assert [f["point"] for f in body["offence"]] == ["They got to the rim."]                 # the invented finding never reached the coach
    assert [r["reason"] for r in body["reasons"]] == ["Rim pressure."] and body["next_time"] == ""
    assert body["headline"].startswith("PHI ") and "77" not in body["headline"]              # replaced by the plain result
    assert [r for r, _ in calls].count("head coach") == 2                                    # asked once more first


def test_numbers_guard():
    allowed = bd.numbers({"made": 9, "attempts": 20, "pps": 1.06, "score": "123-112", "note": "17% (league 13%)"})
    assert bd.invented("9 of 20 for 1.06 a shot, 17% vs 13%, in a 123-112 win, from 3-point range", allowed) == []
    assert bd.invented("45% on 21 shots", allowed) == ["45", "21"]


def test_claude_down_stores_nothing_and_stops(conn):
    _setup(conn)
    down = lambda cmd, stdin, timeout: subprocess.CompletedProcess(cmd, 1, "", "Not logged in")
    res = bd.run(conn, 2027, "fast", "smart", down, teams=["PHI"])
    assert res["done"] == 0 and res["stop"].startswith("claude unavailable") and bd.stored(conn, "PHI") == {}


def test_playbook_analyst_follows_named_plays_across_games_and_only_when_there_are_some(conn):
    _setup(conn)
    calls = []
    assert bd.playbooks(conn, 2027, "smart", staff(calls)) == {"done": 0, "left": 0, "stop": None} and not calls   # nothing named yet
    gid = conn.execute("select game_id from games").fetchone()[0]
    conn.execute("""insert into film_items(game_id, date, team, kind, label, summary, url, impact, effect) values
                    (?, '2026-11-08', 'PHI', 'set', 'horns', 'horns for Embiid', 'u', 'worked', 'got Embiid to the line'),
                    ('', '2026-11-01', 'PHI', 'coverage', 'drop', 'they sit in drop', 'u2', '', '')""", (gid,))
    assert bd.playbooks(conn, 2027, "smart", staff(calls))["done"] == 1
    F = calls[-1][1]
    horns = next(x for x in F["named_plays_and_coverages"] if x["name"] == "horns")
    assert horns["games"][0]["worked"] == "worked" and horns["games"][0]["result"] in ("won", "lost")
    note = bd.playbook_notes(conn, "PHI")
    assert note["headline"].startswith("PHI lean on ") and len(note["notes"]) == 1 and note["notes"][0]["point"].endswith(" is working.")   # 999 dropped
    assert bd.playbooks(conn, 2027, "smart", staff(calls))["done"] == 0 and bd.playbook_notes(conn, "CHI") is None    # unchanged: no call
