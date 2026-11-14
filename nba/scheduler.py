"""serve: Telegram bot + APScheduler in one asyncio loop. Pipelines run in a worker thread, one at a time."""
import asyncio
import logging
import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import os
import urllib.request

from . import bot, collector, db, jobs, web

log = logging.getLogger(__name__)
JOB_LOCK = threading.Lock()          # daily / injury / weekly never overlap; bot commands don't take it
DAILY_AT, WEEKLY_AT = (15, 15), (15, 45)


def heartbeat(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now().isoformat(timespec="seconds"))


def health_problem(data_dir: Path, now: float | None = None) -> str:
    """'' when healthy. NAS Doctor reads the printed reason from the container health log."""
    hb = data_dir / "heartbeat"
    if not hb.exists():
        return "no scheduler heartbeat yet"
    age = (now or time.time()) - hb.stat().st_mtime
    if age > 600:
        return f"scheduler heartbeat is {age / 60:.0f} min old"
    try:
        c = db.connect(data_dir / "nba.db")
        c.execute("select 1").fetchone()
        c.close()
    except Exception as e:
        return f"database will not open: {e}"
    port = int(os.environ.get("WEB_PORT", "8000"))
    if port:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read()
        except Exception as e:
            return f"dashboard not answering on :{port} ({type(e).__name__})"
    return ""


def _locked(ctx, fn, *a):
    if jobs.season_over(ctx) and fn is not jobs.daily:
        return None
    with JOB_LOCK:
        return fn(ctx, *a)


def _injury_slot(ctx):
    now = ctx.now_sgt()
    if (now.hour == 15 and now.minute < 30) or (now.hour == 7 and now.minute > 30):
        return None                                  # window is 15:30 .. 07:30 SGT
    return _locked(ctx, jobs.injury_check)


def catch_up(ctx):
    """A run whose slot passed today while the container was down runs once now (job_runs makes it idempotent)."""
    now = ctx.now_sgt()
    if (now.hour, now.minute) >= DAILY_AT:
        _locked(ctx, jobs.daily)
    if now.weekday() == 0 and (now.hour, now.minute) >= WEEKLY_AT:
        _locked(ctx, jobs.weekly)


def startup(ctx):
    """Migrate, backfill missed days, then catch up on any missed run."""
    with JOB_LOCK:
        db.migrate(ctx.conn)
        try:
            collector.refresh_schedule(ctx.conn, ctx.client, ctx.s.espn_season)
            # stop before last night's slate: the live sweep (or catch-up daily) owns it, so a restart never eats its FINAL cards
            last_night = (ctx.now_sgt().date() - timedelta(days=1)).isoformat()
            r = collector.backfill(ctx.conn, ctx.client, ctx.s.espn_season, last_night, ctx.hoopr)
            if r.new_games:
                ctx.vault.activity("📥", "backfill", f"{len(r.new_games)} games")
                jobs.bump_state(ctx)
            jobs.rebuild_history(ctx)            # dashboard history for any day without a sim
        except Exception:
            log.exception("startup backfill failed (the daily run retries)")
    catch_up(ctx)


async def serve(ctx):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:              # Windows dev runs
            pass
    hb = Path(ctx.s.data_dir) / "heartbeat"
    heartbeat(hb)
    sched = AsyncIOScheduler(timezone="Asia/Singapore")
    run = lambda fn: (lambda: loop.run_in_executor(None, _locked, ctx, fn))
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 7200}
    sched.add_job(run(jobs.daily), CronTrigger(hour=DAILY_AT[0], minute=DAILY_AT[1]), id="daily", **common)
    sched.add_job(lambda: loop.run_in_executor(None, _injury_slot, ctx), CronTrigger(hour="15-23,0-7", minute="0,30"),
                  id="injury", **common)
    sched.add_job(run(jobs.live), CronTrigger(hour="1-14", minute="*/15"), id="live", **common)   # US games 01:00-15:00 SGT
    sched.add_job(run(jobs.film), CronTrigger(hour=21, minute=0), id="film", **common)
    sched.add_job(run(jobs.weekly), CronTrigger(day_of_week="mon", hour=WEEKLY_AT[0], minute=WEEKLY_AT[1]), id="weekly", **common)
    sched.add_job(heartbeat, "interval", seconds=60, args=[hb], id="heartbeat", max_instances=1, coalesce=True)
    sched.start()
    if port := int(os.environ.get("WEB_PORT", "8000")):
        web.start(ctx, port)
    app = bot.build_app(ctx) if ctx.s.tg_token else None
    if app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    loop.run_in_executor(None, startup, ctx)
    log.info("serving: bot %s, scheduler up", "on" if app else "off (no token)")
    await stop.wait()
    log.info("stopping: waiting for the running job to reach a safe point")
    sched.shutdown(wait=False)
    if app:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    got = JOB_LOCK.acquire(timeout=50)           # compose stop_grace_period is 60 s
    ctx.conn.close()
    if got:
        JOB_LOCK.release()
