"""python -m nba serve | daily | weekly | injury_check | backfill | history | predict | tune | backtest | health"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs every request URL at INFO, and Telegram URLs carry the bot token: keep them out of the logs
for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler.scheduler", "apscheduler.executors.default"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def make_ctx():
    from . import config, db, espn, jobs, obsidian_pub, telegram_pub
    s = config.load()
    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    conn = db.connect(s.db_path)
    db.migrate(conn)
    tg = telegram_pub.Telegram(s.tg_token, s.tg_chat_id, s.tg_thread_id) if s.tg_token else telegram_pub.NullTelegram()
    return jobs.Ctx(s=s, conn=conn, client=espn.Client(), tg=tg,
                    vault=obsidian_pub.Vault(s.vault_dir, s.vault_name, s.vault_prefix))


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "serve"
    if cmd == "health":
        from .scheduler import health_problem
        problem = health_problem(Path(os.environ.get("DATA_DIR", "/app/data")))
        if problem:
            print(problem)
            return 1
        return 0
    from . import collector, jobs, ratings, scheduler, tuning
    ctx = make_ctx()
    if cmd == "serve":
        asyncio.run(scheduler.serve(ctx))
    elif cmd == "daily":                 # python -m nba daily [YYYY-MM-DD]
        print(jobs.daily(ctx, argv[1] if len(argv) > 1 else None))
    elif cmd == "weekly":
        print(jobs.weekly(ctx, argv[1] if len(argv) > 1 else None))
    elif cmd == "injury_check":
        print(jobs.injury_check(ctx))
    elif cmd == "backfill":
        collector.refresh_schedule(ctx.conn, ctx.client, ctx.s.espn_season)
        r = collector.backfill(ctx.conn, ctx.client, ctx.s.espn_season, ctx.now_sgt().date().isoformat())
        print(f"{len(r.new_games)} new games; warnings: {r.warnings}; pending: {r.pending}")
    elif cmd == "predict":
        st = jobs.state(ctx)
        for p in jobs.predict_tonight(ctx, st, ctx.now_sgt().date().isoformat(), why="manual"):
            print(f"{p['away']} @ {p['home']}: home {p['home_wp']:.1%} margin {p['margin']:+.1f} v{p['version']}")
    elif cmd == "tune":
        st = jobs.state(ctx)
        df = ratings.game_frame(ctx.conn, ctx.s.espn_season)
        print(json.dumps(tuning.tune(ctx.conn, df, st["prior"], st["params"]), indent=1, default=str))
    elif cmd == "history":              # fill past days' ratings + title sims for the dashboard
        print(f"{jobs.rebuild_history(ctx)} days written")
    elif cmd == "reverify":             # check the stored film labels against their sources again (social_intel.reverify)
        from . import social_intel
        print(json.dumps(social_intel.reverify(ctx.conn, ctx.s.model_ask, claude_runner=ctx.runner), indent=1))
    elif cmd == "breakdown":            # python -m nba breakdown [N]: analysts' breakdown of the newest N team-games
        from . import breakdown
        print(json.dumps(breakdown.run(ctx.conn, ctx.s.espn_season, ctx.s.model_ask, ctx.s.model_daily, ctx.runner,
                                       **({"limit": int(argv[1])} if len(argv) > 1 else {}))))
    elif cmd == "backtest":
        from . import backtest
        print(json.dumps(backtest.run(ctx), indent=1, default=str))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
