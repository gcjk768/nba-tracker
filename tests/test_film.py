import json
import subprocess

import pytest

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


def _extractor(cmd, stdin, timeout):
    out = {"items": [{"team": "PHI", "label": "Horns", "kind": "set", "who": "", "summary": "Philly ran horns for Embiid."},
                     {"team": "CHI", "label": "switch", "kind": "coverage", "who": "", "summary": "Chicago switched."},
                     {"team": "BOS", "label": "drop", "kind": "coverage", "who": "", "summary": "Wrong game."},
                     {"team": "CHI", "label": "nonsense", "kind": "vibes", "who": "", "summary": "x"}]}
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(out)}), "")


def verified(extractor, check=None):
    """Fake claude for both film calls: the extractor's reply, and for the verifier call (stdin has "labels") one check
    per label: check(label, text) -> dict, default = supported, with a quote that really is in the text."""
    def runner(cmd, stdin, timeout):
        j = json.loads(stdin)
        if "labels" not in j:
            return extractor(cmd, stdin, timeout)
        runner.seen.append(stdin)
        yes = {"supported": True, "quote": " ".join(j["text"].split()[:6]), "scope_ok": True, "side_ok": True}
        out = {"checks": [{"id": l["id"], **yes, **((check(l, j["text"]) if check else None) or {})} for l in j["labels"]]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(out)}), "")
    runner.seen = []
    return runner


claude_ok = verified(_extractor)
GAME = {"date": "2026-11-08", "home": "PHI", "away": "CHI"}


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


def test_set_to_plays_uses_real_play_names():
    from nba import playbook_kb as kb, plays
    for label, ps in kb.SET_TO_PLAYS.items():
        assert ps and all(p in plays.PLAY_ORDER for p in ps), (label, [p for p in ps if p not in plays.PLAY_ORDER])


def test_defense_schemes_are_complete():
    from nba import playbook_kb as kb, plays
    for k, ps in kb.COVERAGE_GIVES.items():
        assert all(p in plays.PLAY_ORDER for p in ps), k
    for k in kb.DEF_GROUP:
        assert k in kb.PLAYS, k
    for k in ("2-3 zone", "3-2 zone", "1-3-1 zone", "box-and-one", "drop", "switch", "man-to-man"):
        assert k in kb.COUNTERS, k


# ---------- verifier (second, independent call per chunk) ----------

def _labels(runner, conn=None):
    return {(f["team"], f["label"]) for f in si.extract(TRANSCRIPT, GAME, "https://youtu.be/new1", "t", "m", runner, conn=conn)}


def test_verifier_keeps_supported_labels_and_sees_only_the_labels(conn):
    runner = verified(_extractor)
    assert _labels(runner, conn) == {("PHI", "horns"), ("CHI", "switch")}
    assert si.rejects(conn) == []
    sent = json.loads(runner.seen[0])
    assert len(runner.seen) == 1 and sent["text"] == " ".join(TRANSCRIPT.split())           # one call per chunk, same chunk
    assert [(l["team"], l["label"], l["side"], l["scope"]) for l in sent["labels"]] == [
        ("PHI", "horns", "offence", "game"), ("CHI", "switch", "defence", "game")]
    assert "summary" not in runner.seen[0] and "Philly ran horns" not in runner.seen[0]      # not the extractor's reasoning


def test_verifier_drops_and_records_unsupported_labels(conn):
    no = lambda l, text: {"supported": False, "quote": ""} if l["label"] == "switch" else None
    assert _labels(verified(_extractor, no), conn) == {("PHI", "horns")}
    (r,) = si.rejects(conn)
    assert (r["team"], r["label"], r["kind"], r["reason"], r["url"]) == ("CHI", "switch", "coverage", "not supported by the text", "https://youtu.be/new1")


def test_quote_must_be_in_the_chunk_and_scope_and_side_must_hold(conn):
    answers = {"horns": {"quote": "Philadelphia's whole offense is built on horns"},       # says yes, quote is made up
               "switch": {"quote": "the  BULLS answered by\tswitching everything", "side_ok": False}}   # real quote (spacing, case), wrong side
    assert _labels(verified(_extractor, lambda l, text: answers[l["label"]]), conn) == set()
    assert {(r["label"], r["reason"]) for r in si.rejects(conn)} == {("horns", "quote not in the source"), ("switch", "side not supported")}
    one_word = verified(_extractor, lambda l, text: {"quote": "horns"})                      # in the text, but proves nothing
    assert _labels(one_word) == set()
    system = lambda cmd, stdin, timeout: subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(
        {"items": [{"team": "PHI", "label": "five-out", "kind": "system", "summary": "x"}]})}), "")
    assert _labels(verified(system)) == {("PHI", "5-out")}
    assert _labels(verified(system, lambda l, text: {"scope_ok": l["scope"] != "system"})) == set()   # one-lineup look: dropped


def test_verifier_error_stores_nothing_and_the_pass_goes_on(conn):
    _setup(conn)

    def broken(cmd, stdin, timeout):
        if "labels" in json.loads(stdin):
            broken.calls += 1
            if broken.calls % 2:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "sorry, no JSON"}), "")
        return _extractor(cmd, stdin, timeout)
    broken.calls = 0
    res = si.run(conn, "2026-11-10", "m", reach=si.Reach(FakeYT(), sleep=lambda s: None), claude_runner=broken)
    assert res["stop"] is None and res["added"] == [] and broken.calls == 2                 # analyst.run's one retry, then give up
    assert conn.execute("select count(*) from film_items").fetchone()[0] == 0
    assert conn.execute("select status from film_videos where url='https://youtu.be/new1'").fetchone()[0] == "done: 0 labels"
    assert {(r["label"], r["reason"]) for r in si.rejects(conn)} == {("horns", "verifier failed"), ("switch", "verifier failed")}


@pytest.mark.parametrize("dead", ["extractor", "verifier"])
def test_claude_down_stops_the_pass_and_leaves_the_source_unread(conn, dead):
    _setup(conn)

    def down(cmd, stdin, timeout):
        if ("labels" in json.loads(stdin)) == (dead == "verifier"):
            return subprocess.CompletedProcess(cmd, 1, json.dumps({"is_error": True, "result": "Not logged in"}), "")
        return _extractor(cmd, stdin, timeout)
    res = si.run(conn, "2026-11-10", "m", reach=si.Reach(FakeYT(), sleep=lambda s: None), claude_runner=down)
    assert res["stop"].startswith("claude unavailable") and res["added"] == []
    assert conn.execute("select count(*) from film_videos where status like 'done%'").fetchone()[0] == 0   # read again tomorrow
    assert conn.execute("select count(*) from film_games where status='done'").fetchone()[0] == 0


def test_reverify_moves_unsupported_old_labels_and_leaves_unreadable_sources(conn):
    _setup(conn)
    gid = conn.execute("select game_id from games limit 1").fetchone()[0]
    for url, team, label, kind in (("https://a.example/post", "PHI", "horns", "set"), ("https://a.example/post", "CHI", "switch", "coverage"),
                                   ("https://gone.example/x", "PHI", "zone", "coverage")):
        si._store(conn, gid, "2026-11-08", {"team": team, "label": label, "kind": kind, "who": "", "summary": "s", "title": "t", "ato": 0}, url, "host")

    def web(cmd):
        ok = cmd[3].startswith("https://a.example")
        return 0, json.dumps({"ok": True, "items": [{"text": TRANSCRIPT}] if ok else []})
    no = lambda l, text: {"supported": False, "quote": ""} if l["label"] == "switch" else None
    runner = verified(_extractor, no)
    res = si.reverify(conn, "m", reach=si.Reach(web, sleep=lambda s: None), claude_runner=runner)
    assert (res["sources"], res["kept"], res["dropped"], res["unread"], res["stop"]) == (1, 1, 1, ["https://gone.example/x"], None)
    assert {(r["team"], r["label"]) for r in conn.execute("select team, label from film_items")} == {("PHI", "horns"), ("PHI", "zone")}
    assert [(r["label"], r["reason"]) for r in si.rejects(conn)] == [("switch", "not supported by the text")]
    seen = len(runner.seen)
    assert si.reverify(conn, "m", reach=si.Reach(web, sleep=lambda s: None), claude_runner=runner)["sources"] == 0
    assert len(runner.seen) == seen                                      # the checked source is not paid for twice


def test_film_verify_off_is_the_old_single_call(conn, monkeypatch):
    monkeypatch.setenv("FILM_VERIFY", "off")
    runner = verified(_extractor, lambda l, text: {"supported": False})
    assert _labels(runner, conn) == {("PHI", "horns"), ("CHI", "switch")}
    assert runner.seen == [] and si.rejects(conn) == []
