import os
import time
from types import SimpleNamespace

from nba import bot, jobs, scheduler
from test_jobs import ctx  # noqa: F401  (fixture)


def _update(uid):
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid))


def test_only_allowed_users(ctx):
    ctx.s = ctx.s.__class__(**{**ctx.s.__dict__, "allowed_ids": frozenset({111})})
    assert bot.allowed(ctx, _update(111)) and not bot.allowed(ctx, _update(222))
    assert not bot.allowed(ctx, SimpleNamespace(effective_user=None))


def test_quota(ctx):
    ctx.s = ctx.s.__class__(**{**ctx.s.__dict__, "ask_limit": 3})
    assert [bot.use_quota(ctx, "ask") for _ in range(4)] == [True, True, True, False]
    assert not bot.use_quota(ctx, "whatif")                  # shared limit


def test_commands_answer(ctx):
    jobs.daily(ctx)
    for name, args in [("team", ["PHI"]), ("compare", ["PHI", "CHI"]), ("player", ["Maxey"]), ("status", ["CHI"]),
                       ("game", ["today"]), ("game", ["PHI", "CHI"]), ("scorecard", []), ("whatif", ["PHI", "-Maxey"]),
                       ("plays", ["BOS"]), ("help", [])]:
        out = bot.dispatch(ctx, name, args)
        assert out and "❌" not in out, (name, out)
    assert bot.dispatch(ctx, "nosuch", []) is None
    assert "<tg-spoiler>" in bot.dispatch(ctx, "game", ["PHI", "CHI"])


def test_health(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_PORT", "0")
    assert scheduler.health_problem(tmp_path) == "no scheduler heartbeat yet"
    scheduler.heartbeat(tmp_path / "heartbeat")
    assert scheduler.health_problem(tmp_path) == ""
    old = time.time() - 900
    os.utime(tmp_path / "heartbeat", (old, old))
    assert "min old" in scheduler.health_problem(tmp_path)
