import json
import subprocess

from nba import collector, social_intel as si
from test_collector import FakeClient

TRANSCRIPT = ("Philly kept running horns for Embiid at the elbow and the Bulls answered by switching everything. "
              "Chicago went to a 2-3 zone late in the third. ") * 40


class FakeYT:
    """Agent Reach stand-in: search -> two videos, read -> dates, subtitle -> transcript; optional 429."""
    def __init__(self, rate_limit_on=None):
        self.calls, self.rate_limit_on = [], rate_limit_on

    def __call__(self, cmd):
        action = cmd[2]
        self.calls.append(action)
        if action == self.rate_limit_on:
            return 1, json.dumps({"ok": False, "error": {"code": "RATE_LIMITED", "message": "HTTP Error 429"}})
        if action == "search":
            return 0, json.dumps({"ok": True, "items": [
                {"title": "76ers vs Bulls breakdown: how Philly beat the switch", "url": "https://youtu.be/new1", "author": "BBALLBREAKDOWN"},
                {"title": "76ers Bulls HIGHLIGHTS", "url": "https://youtu.be/hl", "author": "NBA"},
                {"title": "Old Bulls film from March", "url": "https://youtu.be/old", "author": "Coach Mike"}]})
        if action == "read":
            pub = "2026-11-09" if cmd[3].endswith("new1") else "2026-03-01"
            return 0, json.dumps({"ok": True, "items": [{"published_at": pub}]})
        if action == "subtitle":
            return 0, json.dumps({"ok": True, "items": [{"text": TRANSCRIPT}]})
        return 1, "{}"


def claude_ok(cmd, stdin, timeout):
    out = {"items": [{"team": "PHI", "label": "Horns", "kind": "set", "who": "", "summary": "Philly ran horns for Embiid."},
                     {"team": "CHI", "label": "switch", "kind": "coverage", "who": "", "summary": "Chicago switched."},
                     {"team": "BOS", "label": "drop", "kind": "coverage", "who": "", "summary": "Wrong game."},
                     {"team": "CHI", "label": "nonsense", "kind": "vibes", "who": "", "summary": "x"}]}
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(out)}), "")


def _setup(conn):
    collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    conn.execute("delete from games where game_id != '401909945'")


def test_run_stores_labels_not_transcripts(conn):
    _setup(conn)
    yt = FakeYT()
    res = si.run(conn, "2026-11-10", "m", reach=si.Reach(yt, sleep=lambda s: None), claude_runner=claude_ok)
    rows = [dict(r) for r in conn.execute("select * from film_items")]
    assert {(r["team"], r["label"], r["kind"]) for r in rows} == {("PHI", "horns", "set"), ("CHI", "switch", "coverage")}
    assert all(len(r["summary"]) <= 300 and "Embiid at the elbow and the Bulls" not in r["summary"] for r in rows)
    assert "subtitle" in yt.calls and res["stop"] is None
    status = dict(conn.execute("select url, status from film_videos").fetchall())
    assert status["https://youtu.be/old"].startswith("skipped") and "https://youtu.be/hl" not in status
    again = si.run(conn, "2026-11-10", "m", reach=si.Reach(yt, sleep=lambda s: None), claude_runner=claude_ok)
    assert again["games"] == 0                                   # game marked done: no second pass


def test_rate_limit_stops_the_pass(conn):
    _setup(conn)
    yt = FakeYT(rate_limit_on="search")
    res = si.run(conn, "2026-11-10", "m", reach=si.Reach(yt, sleep=lambda s: None), claude_runner=claude_ok)
    assert res["stop"] and "RATE_LIMITED" in res["stop"] and yt.calls == ["search"]


def test_budget_and_contradictions():
    r = si.Reach(lambda cmd: (0, json.dumps({"ok": True, "items": []})), sleep=lambda s: None)
    for _ in range(si.BUDGET["subtitle"]):
        r("subtitle", "x")
    try:
        r("subtitle", "x")
        assert False, "budget not enforced"
    except si.Stop:
        pass
    clash = si.contradictions([{"team": "CHI", "label": "switch", "url": "u", "summary": ""}],
                              {"CHI": [{"label": "Concedes the mid-range (drop-style)"}]})
    assert clash and clash[0]["inferred"].startswith("Concedes")


def test_pick_explainer_prefers_matching_title_and_channel():
    items = [{"title": "Spain Defense: Guard To Guard Switch", "url": "u0", "author": "X", "raw": {"duration": 14}},
             {"title": "The Basics of Spain Pick & Roll", "url": "u1", "author": "Hooper University", "raw": {"duration": 220}},
             {"title": "Spain pick and roll compilation 2 hours", "url": "u2", "author": "Y", "raw": {"duration": 7200}}]
    assert si.pick_explainer(items, "spain pick-and-roll", "set")["url"] == "u1"
    assert si.pick_explainer(items, "horns", "set") is None


def test_counter_kb_is_consistent():
    from nba import playbook_kb as kb
    for key, (ctrs, why) in kb.COUNTERS.items():
        assert why and ctrs
        assert all(c in kb.PLAYS for c in ctrs), (key, [c for c in ctrs if c not in kb.PLAYS])


def test_pick_explainer_rejects_wrong_sport_and_wrong_play():
    items = [{"title": "How To Switch Stick and User Defend in College Football 27", "url": "f", "author": "G", "raw": {"duration": 484}},
             {"title": "Switching ball screens: NBA defense explained", "url": "ok", "author": "Z", "raw": {"duration": 300}}]
    assert si.pick_explainer(items, "switch", "coverage")["url"] == "ok"
    spain = [{"title": "The Basics of Spain Pick & Roll", "url": "s", "author": "Hooper University", "raw": {"duration": 220}}]
    assert si.pick_explainer(spain, "pick-and-roll", "action") is None


def test_scout_weaknesses_on_real_games(conn):
    from nba import collector, schemes, scout
    collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    P = schemes.team_profiles(conn, 2027, {})
    W = scout.weaknesses(conn, 2027, P)
    from nba import playbook_kb as kb
    for t, ws in W.items():
        for w in ws:
            assert w["area"] and w["exploit"] and w["evidence"] and w["source"].startswith("measured")
            assert all(x in kb.PLAYS for x in w["plays"]), w["plays"]
