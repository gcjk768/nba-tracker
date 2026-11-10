import json
import re
import subprocess
from datetime import datetime, timezone

import pandas as pd
import pytest

from nba import config
from nba import db
from nba import espn
from nba import jobs
from nba import obsidian_pub as ob
from nba import ratings
from nba import telegram_pub as tp
from conftest import load

NOW = datetime(2026, 11, 9, 7, 15, tzinfo=timezone.utc)         # 15:15 SGT


class FakeClient:
    def __init__(self, dead_summary=False, injuries=None):
        self.dead_summary, self.inj = dead_summary, injuries

    def scoreboard(self, d):
        if d == "20261108":
            return load("scoreboard_20261108.json")
        if d == "20261109":
            return load("scoreboard_20261109.json")
        return {"events": []}

    def summary(self, gid):
        if self.dead_summary:
            raise RuntimeError("blocked")
        j = load("summary_401909945.json")
        j["header"]["id"] = gid
        return j

    def team_schedule(self, team, season, seasontype=None):
        return {"events": []}

    def futures(self, season):
        return load("futures.json")

    def injuries(self):
        return self.inj if self.inj is not None else load("injuries.json")


class FakeHoopr:
    def season_results(self, season):
        return pd.DataFrame(columns=["game_id", "date", "season_type", "home", "away", "home_pts", "away_pts", "home_poss", "away_poss"])

    def games_for_date(self, season, d):
        s = espn.parse_summary(load("summary_401909945.json"))
        return [{"game": s["game"], "team_box": s["team_box"], "player_box": s["player_box"], "plays": [], "odds": {}}]


VERDICT = {"date": "2026-11-09", "summary": ["Line one.", "Line two.", "Line three."],
           "teams": [{"team": "PHI", "verdict": "Good.", "trend": "rising", "confidence": "likely"}]}


def runner_ok(cmd, stdin, timeout):
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps(VERDICT)}), "")


def runner_bad(cmd, stdin, timeout):
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "sorry, no JSON"}), "")


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    s = config.load()
    c = db.connect(":memory:")
    db.migrate(c)
    db.meta_set(c, "prior:2027", json.dumps({t: [0.0, 0.0] for t in ratings.TEAMS}))
    db.meta_set(c, "elo_seed:2027", json.dumps({t: 1505.0 for t in ratings.TEAMS}))
    return jobs.Ctx(s=s, conn=c, client=FakeClient(), tg=tp.NullTelegram(), vault=ob.Vault(tmp_path / "vault"),
                    runner=runner_ok, hoopr=FakeHoopr(), now=lambda: NOW)


def test_daily_sends_once_and_writes_vault(ctx, tmp_path):
    out = jobs.daily(ctx)
    assert out["games"] == 5 and out["verdict"]
    sent = list(ctx.tg.sent)
    assert sent and all(len(m) <= tp.LIMIT for m in sent)
    watch = sent[0].split("Worth watching", 1)[1].split(tp.DIVIDER, 1)[0]
    assert not re.search(r"\d", watch)                                  # spoiler-free
    assert "Line one." in "\n".join(sent)
    assert jobs.daily(ctx) is None and ctx.tg.sent == sent               # same day again: nothing re-sent
    assert ctx.conn.execute("select count(*) from predictions").fetchone()[0] == 8
    assert (tmp_path / "vault/Daily/2026/11/2026-11-09.md").exists()
    assert list((tmp_path / "vault/Games/2026/11").glob("2026-11-08 *.md"))
    assert (tmp_path / "vault/Teams/Philadelphia 76ers.md").exists()


def test_invalid_claude_gives_stats_only(ctx):
    ctx.runner = runner_bad
    out = jobs.daily(ctx)
    assert out["verdict"] is False
    assert "stats-only" in "\n".join(ctx.tg.sent)


def test_espn_dead_falls_back_and_still_sends(ctx):
    ctx.client = FakeClient(dead_summary=True)
    out = jobs.daily(ctx)
    assert out["games"] == 1 and ctx.tg.sent
    assert "hoopR" in ctx.tg.sent[0]


def test_no_game_day(ctx):
    out = jobs.daily(ctx, key="2026-12-25")
    assert out == {"games": 0} and len(ctx.tg.sent) == 1 and "No games" in ctx.tg.sent[0]


def test_injury_check_alerts_once(ctx):
    jobs.daily(ctx)
    n = len(ctx.tg.sent)
    top = jobs.latest_sim(ctx)
    best = max(top, key=lambda t: top[t]["title"])
    st = jobs.state(ctx, "2026-11-09")
    star = max((p for p in st["values"].items() if p[1]["team"] == best), key=lambda p: p[1]["value"], default=None)
    base = espn.parse_injuries(load("injuries.json"))
    ctx.client = FakeClient(injuries=load("injuries.json"))
    jobs.injury_check(ctx, key="t1")                     # first check seeds state silently (daily didn't snapshot)
    if star:
        inj = load("injuries.json")
        inj["injuries"][0]["injuries"].append({"status": "Out", "shortComment": "", "details": {"type": "Knee"},
                                               "athlete": {"displayName": star[1]["name"], "team": {"abbreviation": best},
                                                           "links": [{"href": f"https://x/id/{star[0]}"}]}})
        ctx.client = FakeClient(injuries=inj)
        r1 = jobs.injury_check(ctx, key="t2")
        r2 = jobs.injury_check(ctx, key="t3")
        assert r1["changes"] >= 1 and r2 == {"changes": 0}
        alerts = [m for m in ctx.tg.sent[n:] if "INJURY ALERT" in m]
        assert len(alerts) <= 1


def test_live_cards_each_final_once_then_daily_still_digests(ctx):
    ctx.now = lambda: datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)        # 11:00 SGT, last night's slate ending
    out = jobs.live(ctx)
    assert out["games"] == 5 and len(ctx.tg.sent) == 5
    assert all(m.startswith("📊 <b>FINAL</b> · ") for m in ctx.tg.sent)
    assert jobs.live(ctx) == {"games": 0} and len(ctx.tg.sent) == 5          # already stored: no repeat card
    ctx.now = lambda: NOW
    assert jobs.daily(ctx)["games"] == 5                                       # digest still covers every game
