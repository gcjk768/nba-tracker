"""Pipelines: daily, injury_check, weekly, season wrap. Each scheduled run is recorded in job_runs so a restart
never sends a digest twice and a missed run is caught up once."""
import json
import logging
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import aggregator as agg
from . import analyst
from . import collector
from . import db
from . import elo as elo_mod
from . import espn
from . import fallback
from . import injuries as inj_mod
from . import injury_alert
from . import obsidian_pub as ob
from . import predictions as P
from . import ratings
from . import review
from . import scorecard
from . import simulate
from . import telegram_pub as tp
from . import tuning

log = logging.getLogger(__name__)
SGT = ZoneInfo("Asia/Singapore")
UTC = timezone.utc


@dataclass
class Ctx:
    s: object                      # config.Settings
    conn: object
    client: object = None
    tg: object = None
    vault: object = None
    runner: object = None          # analyst subprocess runner (tests stub it)
    hoopr: object = fallback
    now: object = None             # callable -> aware datetime (tests freeze time)
    cache: dict = field(default_factory=dict)

    def now_sgt(self):
        return (self.now() if self.now else datetime.now(UTC)).astimezone(SGT)


# ---------- exactly-once bookkeeping ----------

def begin(ctx, job, key) -> dict | None:
    """Returns the run row (steps done) or None if this run already finished."""
    with db.LOCK:
        r = ctx.conn.execute("select * from job_runs where job=? and key=?", (job, key)).fetchone()
        if r and r["status"] == "done":
            return None
        if not r:
            ctx.conn.execute("insert into job_runs(job, key, status, started) values (?,?, 'running', ?)",
                             (job, key, ctx.now_sgt().isoformat(timespec="seconds")))
        return dict(ctx.conn.execute("select * from job_runs where job=? and key=?", (job, key)).fetchone())


def steps(ctx, job, key) -> set:
    r = ctx.conn.execute("select steps from job_runs where job=? and key=?", (job, key)).fetchone()
    return set(filter(None, (r["steps"] or "").split(","))) if r else set()


def step_done(ctx, job, key, step):
    with db.LOCK:
        s = steps(ctx, job, key) | {step}
        ctx.conn.execute("update job_runs set steps=? where job=? and key=?", (",".join(sorted(s)), job, key))


def finish(ctx, job, key, status="done", error=None):
    with db.LOCK:
        ctx.conn.execute("update job_runs set status=?, finished=?, error=? where job=? and key=?",
                         (status, ctx.now_sgt().isoformat(timespec="seconds"), error, job, key))


def guarded(ctx, job, key, fn):
    """Run fn(ctx, key) once per (job, key); on failure send one failure card and leave the run retryable."""
    if begin(ctx, job, key) is None:
        log.info("%s %s already done", job, key)
        return None
    try:
        out = fn(ctx, key)
        finish(ctx, job, key)
        return out
    except Exception as e:
        log.exception("%s %s failed", job, key)
        finish(ctx, job, key, "failed", traceback.format_exc()[-1500:])
        try:
            ctx.tg.send(tp.title("fail", f"NBA {job} failed", key) + f"\n\n<code>{tp.e(type(e).__name__)}: {tp.e(str(e)[:300])}</code>"
                        + "\n<i>The next scheduled run retries.</i>")
            ctx.vault.activity("❌", f"{job} failed", f"{type(e).__name__}: {str(e)[:120]}")
        except Exception:
            log.exception("failure alert failed")
        return None


# ---------- model state ----------

def _meta_json(ctx, k):
    v = db.meta_get(ctx.conn, k)
    return json.loads(v) if v else None


def prior(ctx) -> dict:
    k = f"prior:{ctx.s.espn_season}"
    p = _meta_json(ctx, k)
    if p is None:
        last = ctx.hoopr.season_results(ctx.s.espn_season - 1)
        p = ratings.prior_from_season(last[last.season_type == 2])
        db.meta_set(ctx.conn, k, json.dumps(p))
    return {t: tuple(v) for t, v in p.items()}


def elo_now(ctx) -> dict:
    k = f"elo_seed:{ctx.s.espn_season}"
    seed = _meta_json(ctx, k)
    if seed is None:
        path = Path(ctx.s.data_dir) / "nba_elo.csv"
        if not path.exists():
            import httpx
            path.write_bytes(httpx.get(elo_mod.ARCHIVE_URL, timeout=120, follow_redirects=True).content)
        r = elo_mod.seed_from_archive(elo_mod.load_archive(path), 2024)
        for season in range(2025, ctx.s.espn_season):
            r, _ = elo_mod.replay(r, ctx.hoopr.season_results(season))
        seed = r
        db.meta_set(ctx.conn, k, json.dumps(seed))
    import pandas as pd
    rows = [dict(x) for x in ctx.conn.execute("""select game_id, date, home, away, home_pts, away_pts, neutral from games
        where season=? and status='final' and home_pts is not null and season_type in (2,3,5) order by date""",
                                              (ctx.s.espn_season,))]
    r, _ = elo_mod.replay(seed, pd.DataFrame(rows, columns=["game_id", "date", "home", "away", "home_pts", "away_pts", "neutral"]))
    return r


def _is_cup_final(note):
    n = (note or "").lower()
    return "cup" in n and ("championship" in n or "final" in n)


def standings(ctx, before=None):
    wins, losses = {t: 0 for t in espn.TEAM_IDS}, {t: 0 for t in espn.TEAM_IDS}
    q = "select * from games where season=? and season_type=2 and status='final' and home_pts is not null"
    for g in ctx.conn.execute(q + (" and date < ?" if before else ""), (ctx.s.espn_season,) + ((before,) if before else ())):
        if _is_cup_final(g["note"]):
            continue
        w, l = (g["home"], g["away"]) if g["home_pts"] > g["away_pts"] else (g["away"], g["home"])
        wins[w] += 1
        losses[l] += 1
    return wins, losses


def state(ctx, asof: str | None = None, params=None, injuries_override=None) -> dict:
    """Everything the model knows on SGT date `asof` (games before that US date count)."""
    asof = asof or ctx.now_sgt().date().isoformat()
    key = (asof, injuries_override is None and params is None)
    if key[1] and ctx.cache.get("state_key") == (asof, db.meta_get(ctx.conn, "state_version")):
        return ctx.cache["state"]
    p = params or tuning.current(ctx.conn)
    pr = prior(ctx)
    df = ratings.game_frame(ctx.conn, ctx.s.espn_season, before=asof)
    R = ratings.current(df, pr, p, asof)
    pb = ratings.player_frame(ctx.conn, ctx.s.espn_season, before=asof)
    values = ratings.player_values(pb, ratings.games_played(df), p.injury_scale)
    injuries = injuries_override if injuries_override is not None else inj_mod.state(ctx.conn)
    lg_pace = sum(R[t]["pace"] for t in ratings.TEAMS) / 30
    s_reg = {t: R[t]["net"] * lg_pace / 100 + ratings.availability_delta(t, values, injuries) for t in ratings.TEAMS}
    s_po = {t: R[t]["net"] * lg_pace / 100 + ratings.availability_delta(t, values, injuries, healthy=True)
            for t in ratings.TEAMS}
    for t in ratings.TEAMS:
        R[t]["healthy_net"] = s_po[t] * 100 / lg_pace
    st = {"asof": asof, "params": p, "prior": pr, "df": df, "ratings": R, "values": values, "injuries": injuries,
          "s_reg": s_reg, "s_po": s_po, "lg_pace": lg_pace}
    if key[1]:
        ctx.cache.update(state_key=(asof, db.meta_get(ctx.conn, "state_version")), state=st)
    return st


def bump_state(ctx):
    db.meta_set(ctx.conn, "state_version", str(int(db.meta_get(ctx.conn, "state_version", "0")) + 1))


def remaining_games(ctx, asof=None):
    """Regular-season games still to play (as of a past date: every game on or after it, whatever happened later)."""
    if asof:
        rows = ctx.conn.execute("select * from games where season=? and season_type=2 and date >= ? and status!='canceled'",
                                (ctx.s.espn_season, asof))
    else:
        rows = ctx.conn.execute("select * from games where season=? and season_type=2 and status='scheduled'", (ctx.s.espn_season,))
    return [(g["home"], g["away"], g["neutral"]) for g in rows if not _is_cup_final(g["note"])]


def postseason_state(ctx, wins):
    """Play-in winners and playoff series scores from stored games (empty before the postseason)."""
    pi = {}
    for conf in (1, 0):
        teams = {t for t in espn.TEAM_IDS if (t in espn.EAST) == bool(conf)}
        gs = [g for g in ctx.conn.execute("select * from games where season=? and season_type=5 and status='final' order by date",
                                          (ctx.s.espn_season,)) if g["home"] in teams]
        if len(gs) == 3:
            first = sorted(gs[:2], key=lambda g: -(wins[g["home"]] + wins[g["away"]]))
            win = lambda g: g["home"] if g["home_pts"] > g["away_pts"] else g["away"]
            pi[conf] = (win(first[0]), win(gs[2]))
    series = {}
    for g in ctx.conn.execute("select * from games where season=? and season_type=3 and status='final'", (ctx.s.espn_season,)):
        a, b = sorted((g["home"], g["away"]), key=lambda t: -wins[t])
        w = g["home"] if g["home_pts"] > g["away_pts"] else g["away"]
        wa, wb = series.get((a, b), (0, 0))
        series[(a, b)] = (wa + (w == a), wb + (w == b))
    return pi or None, series or None


def run_sim(ctx, st, n=10000, seed=None, injuries_override=None, historical=False):
    s_reg, s_po = st["s_reg"], st["s_po"]
    if injuries_override is not None:
        st2 = state(ctx, st["asof"], injuries_override=injuries_override)
        s_reg, s_po = st2["s_reg"], st2["s_po"]
    asof = st["asof"] if historical else None
    wins, losses = standings(ctx, asof)
    pi, series = (None, None) if historical else postseason_state(ctx, wins)
    p = st["params"]
    return simulate.simulate(s_reg, s_po, wins, losses, remaining_games(ctx, asof), p.hca, p.sigma, n=n, seed=seed,
                             series_state=series, playin_done=pi)


def rebuild_history(ctx, n=5000) -> int:
    """Fill ratings + daily sims for every past day of the season that has none (point in time, no injury data),
    so the dashboard's title race starts on opening night. Returns the number of days written."""
    first = ctx.conn.execute("select min(date) from games where season=? and source is not null", (ctx.s.espn_season,)).fetchone()[0]
    if not first:
        return 0
    today = ctx.now_sgt().date()
    d, done = date.fromisoformat(first) + timedelta(days=1), 0
    while d < today:
        key = d.isoformat()
        if not ctx.conn.execute("select 1 from sims where kind='daily' and date=?", (key,)).fetchone():
            st = state(ctx, key, injuries_override={})
            with db.tx(ctx.conn):
                for t in ratings.TEAMS:
                    r = st["ratings"][t]
                    db.upsert(ctx.conn, "ratings", {"date": key, "team": t, "off": r["off"], "def": r["def"], "net": r["net"],
                                                    "prior_net": r["prior_net"], "blend_w": r["blend_w"],
                                                    "healthy_net": r["healthy_net"], "games": r["games"]})
            store_sim(ctx, key, "daily", run_sim(ctx, st, n=n, seed=int(key.replace("-", "")), historical=True))
            done += 1
        d += timedelta(days=1)
    return done


def store_sim(ctx, d, kind, res):
    with db.tx(ctx.conn):
        for t, v in res.items():
            db.upsert(ctx.conn, "sims", {"date": d, "kind": kind, "team": t, "playoffs": v["playoffs"],
                                         "conf_finals": v["conf_finals"], "finals": v["finals"], "title": v["title"]})


def latest_sim(ctx, kind="daily", before=None) -> dict:
    q = "select max(date) from sims where kind=?" + (" and date < ?" if before else "")
    d = ctx.conn.execute(q, [kind] + ([before] if before else [])).fetchone()[0]
    if not d:
        return {}
    return {r["team"]: dict(r) for r in ctx.conn.execute("select * from sims where kind=? and date=?", (kind, d))}


def market_title(ctx) -> dict:
    d = ctx.conn.execute("select max(date) from futures").fetchone()[0]
    rows = ctx.conn.execute("select team, implied from futures where date=?", (d,)).fetchall() if d else []
    tot = sum(r["implied"] for r in rows) or 1
    return {r["team"]: r["implied"] / tot for r in rows}


def refresh_futures(ctx, d):
    try:
        f = espn.parse_futures(ctx.client.futures(ctx.s.espn_season))
    except Exception as e:
        log.warning("futures failed: %s", e)
        return
    with db.tx(ctx.conn):
        for t, am in f.items():
            db.upsert(ctx.conn, "futures", {"date": d, "team": t, "american": am, "implied": P.american_to_prob(am)})


def tip_sgt(tip_utc):
    return P._parse_ts(tip_utc).astimezone(SGT).strftime("%a %H:%M")


def predict_tonight(ctx, st, us_date, why="daily"):
    odds = {}
    try:
        sb = ctx.client.scoreboard(us_date.replace("-", ""))
        for g in espn.parse_scoreboard(sb):          # tonight's slate even if the schedule feed missed it
            if g["status"] == "scheduled":
                collector.upsert_schedule_row(ctx.conn, g)
        odds = espn.scoreboard_odds(sb)
    except Exception as e:
        log.warning("odds fetch failed: %s", e)
    pctx = {"params": st["params"], "ratings": st["ratings"], "values": st["values"], "injuries": st["injuries"],
            "elo": elo_now(ctx), "odds": odds}
    preds = P.predict_slate(ctx.conn, us_date, pctx, why=why, now=ctx.now_sgt().astimezone(UTC))
    return preds


def slate_view(ctx, us_date):
    """Latest stored prediction for every game on a US date (for the digest and /game today)."""
    out = []
    for g in ctx.conn.execute("select * from games where date=? order by tip_utc", (us_date,)):
        p = P.latest(ctx.conn, g["game_id"])
        if p:
            out.append({"game_id": g["game_id"], "home": g["home"], "away": g["away"], "home_wp": p["home_wp"],
                        "market_wp": p["market_wp"], "margin": p["margin"], "total": p["total"],
                        "tip_sgt": tip_sgt(g["tip_utc"]), "status": g["status"]})
    return out


# ---------- daily ----------

def daily(ctx, key=None):
    key = key or ctx.now_sgt().date().isoformat()
    return guarded(ctx, "daily", key, _daily)


def _daily(ctx, key):
    s, conn = ctx.s, ctx.conn
    today = key                                      # SGT date = tonight's US slate date
    last_night = (date.fromisoformat(key) - timedelta(days=1)).isoformat()
    done = steps(ctx, "daily", key)
    warnings = []
    if "collected" not in done:
        collector.refresh_schedule(conn, ctx.client, s.espn_season)
        if not remaining_games(ctx):
            collector.refresh_schedule(conn, ctx.client, s.espn_season, seasontype=3)
        res = collector.collect_date(conn, ctx.client, last_night, s.espn_season, ctx.hoopr)   # always, even if the schedule feed failed
        more = collector.backfill(conn, ctx.client, s.espn_season, today)
        res.new_games += more.new_games
        res.pending += more.pending
        warnings += res.warnings + more.warnings
        refresh_futures(ctx, today)
        try:                                       # price tonight with today's injury list (first run seeds silently)
            inj_mod.snapshot(conn, espn.parse_injuries(ctx.client.injuries()))
        except Exception as e:
            log.warning("injury snapshot in daily failed: %s", e)
        bump_state(ctx)
        step_done(ctx, "daily", key, "collected")
        if res.pending:
            db.meta_set(conn, "pending_dates", json.dumps(sorted({last_night})))
    games = [dict(g) for g in conn.execute("select * from games where date=? and status='final' and source is not null "
                                           "order by tip_utc", (last_night,))]
    st = state(ctx, today)
    # grade + AAR on the picks we froze before last night's games
    st_pre = state(ctx, last_night)
    aar = review.review_slate(conn, [g["game_id"] for g in games], st_pre["values"], _inj_at_pick(conn, games))
    tune_rep = {"adopted": False}
    if games and "tuned" not in done:
        df_season = ratings.game_frame(conn, s.espn_season, before=today)
        tune_rep = tuning.tune(conn, df_season, st["prior"], st["params"])
        db.meta_set(conn, f"tune:{key}", json.dumps(tune_rep, default=str))
        if tune_rep.get("adopted"):
            bump_state(ctx)
            ctx.cache.clear()
            st = state(ctx, today)
        step_done(ctx, "daily", key, "tuned")
    else:
        tune_rep = _meta_json(ctx, f"tune:{key}") or tune_rep
    with db.tx(conn):
        for t in ratings.TEAMS:
            r = st["ratings"][t]
            db.upsert(conn, "ratings", {"date": today, "team": t, "off": r["off"], "def": r["def"], "net": r["net"],
                                        "prior_net": r["prior_net"], "blend_w": r["blend_w"], "healthy_net": r["healthy_net"],
                                        "games": r["games"]})
        for t, v in elo_now(ctx).items():
            db.upsert(conn, "elo", {"date": today, "team": t, "elo": v})
    prev_sim = latest_sim(ctx, "daily", before=today)
    sim = run_sim(ctx, st, n=10000, seed=int(today.replace("-", "")))
    store_sim(ctx, today, "daily", sim)
    preds = predict_tonight(ctx, st, today)
    tonight = slate_view(ctx, today)
    if not games:
        if "sent" not in done:
            msg = tp.title("digest", "NBA DAILY", _nice(today)) + "\n\n<i>No games last night.</i>"
            if tonight:
                msg += "\n\n" + tp.tonight_block(tonight)
            ctx.tg.send(msg)
            step_done(ctx, "daily", key, "sent")
        ctx.vault.activity("💤", "no games last night", f"{len(tonight)} predicted for tonight", today)
        _write_predictions_note(ctx, today, tonight)
        return {"games": 0}
    briefs = [agg.game_brief(conn, g["game_id"]) for g in games]
    sc_all = scorecard.grade(conn)
    summary = _daily_summary(ctx, st, today, games, briefs, aar, sim, prev_sim, tune_rep)
    verdict, raw = (None, "")
    if "claude" not in done:
        verdict, raw = analyst.run("daily.md", agg.compact(summary), s.model_daily, **({"runner": ctx.runner} if ctx.runner else {}))
        with db.LOCK:
            conn.execute("insert or replace into verdicts(date, kind, json) values (?,?,?)",
                         (today, "daily", verdict.model_dump_json() if verdict else json.dumps({"fallback": True})))
        step_done(ctx, "daily", key, "claude")
    else:
        verdict = _load_verdict(conn, today, "daily")
    notes = {n.game_id: n.note for n in (verdict.aar_notes if verdict else [])}
    for a in aar:
        a["note"] = notes.get(a["game_id"], "")
    daily_rel = f"Daily/{ob.ym(today)}/{today}.md"
    msgs = build_digest(ctx, today, games, briefs, aar, tune_rep, st, sim, prev_sim, verdict, tonight, sc_all, warnings, daily_rel)
    if "sent" not in done:
        ctx.tg.send_many(msgs, buttons=[("🔄 Tonight", "/game today"), ("🎯 Scorecard", "/scorecard")])
        step_done(ctx, "daily", key, "sent")
    _write_daily_vault(ctx, today, last_night, games, briefs, aar, tune_rep, st, sim, verdict, tonight, msgs, daily_rel)
    ctx.vault.activity("📊", "daily digest sent", f"{len(games)} games, {len(aar)} AAR, {len(tonight)} picks tonight"
                       + (" · stats-only (Claude failed)" if verdict is None else ""), today)
    if season_over(ctx):
        season_wrap(ctx)
    return {"games": len(games), "aar": len(aar), "verdict": verdict is not None}


def _nice(d):
    x = date.fromisoformat(d)
    return f"{x:%a} {x.day} {x:%b}"


def _inj_at_pick(conn, games):
    out = {}
    for g in games:
        p = P.graded(conn, g["game_id"])
        if p and p["reason"]:
            out.update((json.loads(p["reason"]) or {}).get("inj") or {})
    return {k: {"status": v} for k, v in out.items()}


def _load_verdict(conn, d, kind):
    r = conn.execute("select json from verdicts where date=? and kind=?", (d, kind)).fetchone()
    if not r:
        return None
    j = json.loads(r[0])
    return None if j.get("fallback") else analyst.Verdict.model_validate(j)


def _daily_summary(ctx, st, today, games, briefs, aar, sim, prev_sim, tune_rep):
    played = sorted({g["home"] for g in games} | {g["away"] for g in games})
    R = st["ratings"]
    seasons = agg.team_season(ctx.conn, ctx.s.espn_season, R, before=today)
    mkt = market_title(ctx)
    teams = {}
    for t in played:
        teams[t] = {"rating": {k: round(R[t][k], 1) for k in ("off", "def", "net", "healthy_net", "prior_net")},
                    "blend_w": round(R[t]["blend_w"], 2), **seasons.get(t, {}),
                    "title": round(sim[t]["title"], 3), "title_prev": round(prev_sim.get(t, {}).get("title", 0), 3),
                    "out": [f"{v['name']} ({v['status']})" for pid, v in st["injuries"].items() if v["team"] == t][:6],
                    "stars": agg.star_workload(ctx.conn, ctx.s.espn_season, t)}
    race = sorted(sim, key=lambda t: -sim[t]["title"])[:8]
    return {"date": today, "games": briefs, "teams": teams,
            "title_race": [{"team": t, "model_pct": round(sim[t]["title"], 3), "market_pct": round(mkt.get(t, 0), 3)} for t in race],
            "aar": [{k: a[k] for k in ("game_id", "pick", "opp", "pick_wp", "pred_margin", "actual_margin", "causes", "main",
                                       "scratches")} for a in aar],
            "tuning": {k: tune_rep.get(k) for k in ("adopted", "changes", "reason", "ll_old", "ll_new")},
            "memory": ctx.vault.recent_activity(days=3, cap=2000)}


def build_digest(ctx, today, games, briefs, aar, tune_rep, st, sim, prev_sim, verdict, tonight, sc_all, warnings, daily_rel):
    sp = tp.Spoiler(ctx.s.spoiler)
    ranked = sorted(zip(games, briefs), key=lambda x: -((x[1].get("excitement") or {}).get("score") or 0))
    watch = [{"label": b["matchup"], "stars": (b.get("excitement") or {}).get("stars", 1),
              "reasons": [r for r in ((b.get("excitement") or {}).get("reasons") or "").split(",") if r]} for _, b in ranked]
    res = []
    for g, b in ranked:
        p = P.graded(ctx.conn, g["game_id"])
        right = None if not p else (p["home_wp"] >= 0.5) == (g["home_pts"] > g["away_pts"])
        f = b.get("factors", {})
        fh, fa = f.get(g["home"], {}), f.get(g["away"], {})
        stats = (f"pace {b.get('pace', 0):.0f} · eFG {fa.get('efg', 0):.0%}/{fh.get('efg', 0):.0%} · "
                 f"TOV {b.get('tov', {}).get(g['away'], 0)}/{b.get('tov', {}).get(g['home'], 0)} · "
                 f"REB {g['home']} {b.get('reb_margin', {}).get(g['home'], 0):+d}") if f else ""
        pick = ""
        if p:
            fav = g["home"] if p["home_wp"] >= 0.5 else g["away"]
            pick = f" · pick {fav} {max(p['home_wp'], 1 - p['home_wp']):.0%}"
        top = b["top_players"][0] if b["top_players"] else None
        res.append({"matchup": b["matchup"], "score": b["score"], "ot": g["ot"], "pick_right": right,
                    "stats_line": stats + pick, "top_line": f"{top['name']} {top['line']}" if top else ""})
    impact = [{"label": b["matchup"], "best": b["wpa_best"], "worst": b["wpa_worst"]} for _, b in ranked if b["wpa_best"]]
    since = (ctx.now_sgt() - timedelta(hours=24)).astimezone(UTC).isoformat(timespec="seconds")
    status = [c for c in inj_mod.recent_changes(ctx.conn, since) if c["player_id"] in st["values"]
              and st["values"][c["player_id"]]["mpg"] >= 20][:8]
    movers = _movers(ctx, today)
    mkt = market_title(ctx)
    race = [{"team": t, "model": sim[t]["title"], "market": mkt.get(t),
             "delta": (sim[t]["title"] - prev_sim[t]["title"]) if t in prev_sim else None}
            for t in sorted(sim, key=lambda t: -sim[t]["title"])[:5]]
    if verdict and verdict.summary:
        vlines = verdict.summary[:5]
    else:
        vlines = ["Claude verdict unavailable today: stats-only digest."]
    tuning_line = ("Model updated: " + "; ".join(tune_rep.get("changes", []))
                   + f" (log loss {tune_rep['ll_old']:.3f}→{tune_rep['ll_new']:.3f})") if tune_rep.get("adopted") else \
        ("Model unchanged: no tweak beat it on this season's games." if aar else None)
    blocks = [("watch", tp.worth_watching(watch)), ("results", tp.results(res, sp)),
              ("aar", tp.aar_block(aar, sp, tuning_line) if aar else ""),
              ("impact", tp.impact_block(impact, sp) if impact else ""),
              ("status", tp.status_block(status) if status else ""),
              ("movers", tp.movers_block(movers, sp) if movers else ""),
              ("title", tp.title_block(race, sp)), ("verdict", tp.verdict_block(vlines, sp)),
              ("tonight", tp.tonight_block(tonight) if tonight else ""), ("record", tp.record_block(sc_all)),
              ("link", f"🗂 <code>{tp.e(ctx.vault.obsidian_url(daily_rel))}</code>")]
    if warnings:
        blocks.insert(0, ("warn", "\n".join(f"{tp.SECTION_TITLES['warn']} <i>{tp.e(w)}</i>" for w in warnings[:3])))
    return tp.fit(blocks, tp.title("digest", "NBA DAILY", f"{_nice(today)} · {len(games)} games"))


def _movers(ctx, today, n=4):
    prev = ctx.conn.execute("select max(date) from ratings where date < ?", (today,)).fetchone()[0]
    if not prev:
        return []
    old = {r["team"]: r["net"] for r in ctx.conn.execute("select team, net from ratings where date=?", (prev,))}
    new = {r["team"]: r["net"] for r in ctx.conn.execute("select team, net from ratings where date=?", (today,))}
    d = sorted(((t, new[t] - old[t], new[t]) for t in new if t in old), key=lambda x: -abs(x[1]))
    return [x for x in d[:n] if abs(x[1]) >= 0.3]


# ---------- vault notes ----------

def _wp_by_quarter(conn, game_id):
    rows = conn.execute("""select period, home_wp from plays where game_id=? and home_wp is not null
                           and seq in (select max(seq) from plays where game_id=? group by period) order by period""",
                        (game_id, game_id)).fetchall()
    return [(f"Q{r['period']}" if r["period"] <= 4 else f"OT{r['period'] - 4}", r["home_wp"]) for r in rows]


def _write_daily_vault(ctx, today, last_night, games, briefs, aar, tune_rep, st, sim, verdict, tonight, msgs, daily_rel):
    v, conn = ctx.vault, ctx.conn
    vg = {x.game_id: x for x in (verdict.games if verdict else [])}
    vt = {x.team: x for x in (verdict.teams if verdict else [])}
    aar_by = {a["game_id"]: a for a in aar}
    for g, b in zip(games, briefs):
        name = ob.game_name(last_night, g["away"], g["home"])
        p = P.graded(conn, g["game_id"])
        ex = b.get("excitement") or {}
        body = [f"# {name}", "", f"{ob.team_link(g['away'])} {g['away_pts']} @ {ob.team_link(g['home'])} {g['home_pts']}"
                + (f" ({g['ot']}OT)" if g["ot"] else "") + f" · source: {g['source']} · daily: [[{today}]]", ""]
        if g["game_id"] in vg:
            x = vg[g["game_id"]]
            body += [f"**{x.headline}** — {x.verdict}", f"Key stat: {x.key_stat}", ""]
        body += [f"## Watchability", f"Score {ex.get('score', 0)} · {tp.stars(ex.get('stars', 1))} · {ex.get('reasons') or 'routine'}", ""]
        wpq = _wp_by_quarter(conn, g["game_id"])
        if wpq:
            body += ["Home win probability at the end of each period:", "",
                     ob.table(["Period"] + [q for q, _ in wpq], [[g["home"]] + [f"{w:.0%}" for _, w in wpq]]), ""]
        if b.get("factors"):
            body += ["## Four factors", "", ob.table(["Team", "eFG%", "TOV%", "ORB%", "FT rate", "3PA rate"],
                     [[t, f"{f['efg']:.1%}", f"{f['tov']:.1%}", f"{f['orb']:.1%}", f"{f['ftr']:.3f}", f"{f['p3a']:.1%}"]
                      for t, f in b["factors"].items()]), ""]
        body += ["## Player impact", "", ob.table(["Player", "Team", "Line", "Game score"],
                 [[x["name"], x["team"], x["line"], x["game_score"]] for x in b["top_players"]]), ""]
        if b["wpa_best"]:
            body += ["Win probability added: " + ", ".join(f"{n} ({t}) {w:+.1%}" for n, t, w in b["wpa_best"] + b["wpa_worst"]), ""]
        box = conn.execute("select * from player_box where game_id=? and dnp=0 order by team, minutes desc", (g["game_id"],)).fetchall()
        body += ["## Box score", "", ob.table(["Player", "Team", "Min", "Pts", "Reb", "Ast", "Stl", "Blk", "TO", "FG", "3P", "FT", "+/-"],
                 [[r["name"], r["team"], int(r["minutes"]), r["pts"], r["reb"], r["ast"], r["stl"], r["blk"], r["tov"],
                   f"{r['fgm']}-{r['fga']}", f"{r['fg3m']}-{r['fg3a']}", f"{r['ftm']}-{r['fta']}", r["plus_minus"]] for r in box]), ""]
        if p:
            body += ["## Prediction", f"Frozen v{p['version']} at {p['created_utc']}: {g['home']} {p['home_wp']:.0%}, margin {p['margin']:+.1f}, "
                     f"total {p['total']:.0f}" + (f" · Elo {p['elo_wp']:.0%}" if p["elo_wp"] is not None else "")
                     + (f" · market {p['market_wp']:.0%}" if p["market_wp"] is not None else ""), ""]
        if g["game_id"] in aar_by:
            a = aar_by[g["game_id"]]
            body += ["## AAR", f"Picked {a['pick']} at {a['pick_wp']:.0%}; predicted {a['pred_margin']:+.1f}, actual {a['actual_margin']:+d} "
                     f"(error {a['error']:+.1f}). Main cause: **{a['main']}**.", "",
                     ob.table(["Cause", "Points"], [[k, f"{x:+.1f}"] for k, x in a["causes"].items()]), "",
                     f"Lesson: {a['lesson']}" + (f" {a.get('note')}" if a.get("note") else ""), ""]
        body += ["## Tactics", "_Phase 2: inferred schemes from play-by-play._", "", "## Film Room", "_Phase 3._"]
        hp = g["home_pts"] - g["away_pts"]
        v.write(ob.game_rel(last_night, g["away"], g["home"]),
                {"date": last_night, "teams": [g["away"], g["home"]], "score": f"{g['away_pts']}-{g['home_pts']}",
                 "margin": hp, "pace": b.get("pace"), "excitement": ex.get("score"), "source": g["source"],
                 "verdict": vg[g["game_id"]].headline if g["game_id"] in vg else "", "tags": ["game"], "updated": today},
                "\n".join(body))
    played = sorted({g["home"] for g in games} | {g["away"] for g in games})
    seasons = agg.team_season(conn, ctx.s.espn_season, st["ratings"], before=today)
    for t in played:
        r, se, vv = st["ratings"][t], seasons.get(t, {}), vt.get(t)
        cur = [f"Record {se.get('record')} (home {se.get('home')}, road {se.get('road')}) · Pythagorean {se.get('pyth_wins')} wins",
               f"Net {r['net']:+.1f} (off {r['off']:+.1f}, def {r['def']:+.1f}) · healthy {r['healthy_net']:+.1f} · "
               f"prior {r['prior_net']:+.1f}, weight on this season {r['blend_w']:.0%}",
               f"Rolling net: last 5 {se.get('roll5')}, last 10 {se.get('roll10')} · SOS played {se.get('sos_played')}",
               f"Title {sim[t]['title']:.1%} · finals {sim[t]['finals']:.1%} · playoffs {sim[t]['playoffs']:.0%}",
               "Luck flags: " + (", ".join(se.get("luck_flags") or []) or "none")]
        if vv:
            cur += ["", f"**Verdict ({vv.confidence}):** {vv.verdict}", f"Trend: {vv.trend} · contender: {vv.contender}",
                    "Strengths: " + "; ".join(vv.strengths), "Weaknesses: " + "; ".join(vv.weaknesses),
                    f"Ceiling: {vv.ceiling} · Floor: {vv.floor}"]
        cur += ["", "## Playbook", "_Phase 2: play-type and scheme profile._"]
        g = next(x for x in games if t in (x["home"], x["away"]))
        v.team_page(t, {"team": t, "record": se.get("record"), "net": round(r["net"], 1), "title": round(sim[t]["title"], 3),
                        "trend": vv.trend if vv else "", "updated": today, "tags": ["team"]},
                    "\n".join(cur), f"- {today} net {r['net']:+.1f} · title {sim[t]['title']:.1%} · "
                                    f"[[{ob.game_name(last_night, g['away'], g['home'])}]]")
    if aar:
        v.write(f"Model/AAR/{ob.ym(today)}/{today}.md", {"date": today, "misses": len(aar), "tags": ["aar"], "updated": today},
                f"# AAR {today}\n\n" + "\n".join(
                    f"- [[{ob.game_name(last_night, *(_teams(conn, a['game_id'])))}]] picked **{a['pick']}** {a['pick_wp']:.0%} · "
                    f"error {a['error']:+.1f} · main cause **{a['main']}** · " + ", ".join(f"{k} {x:+.1f}" for k, x in a["causes"].items())
                    + (f" · {a['note']}" if a.get("note") else "") for a in aar)
                + "\n\n## Model change\n\n" + (("; ".join(tune_rep.get("changes", []))) if tune_rep.get("adopted")
                                                else tune_rep.get("reason", "none")))
    _write_tuning_note(ctx, today)
    _write_scorecard_note(ctx, today)
    _write_predictions_note(ctx, today, tonight)
    v.write(daily_rel, {"date": today, "games": len(games), "aar": len(aar), "claude": verdict is not None,
                        "tags": ["daily"], "updated": today},
            f"# NBA daily {today}\n\n" + "\n".join(f"- [[{ob.game_name(last_night, g['away'], g['home'])}]]" for g in games)
            + "\n\n## Digest as sent\n\n" + "\n\n".join(tp.plain(m) for m in msgs)
            + ("\n\n## Claude verdict (full)\n\n```json\n" + verdict.model_dump_json(indent=1) + "\n```" if verdict else ""))
    v.home(today, ctx.s.season)


def _teams(conn, gid):
    r = conn.execute("select away, home from games where game_id=?", (gid,)).fetchone()
    return r["away"], r["home"]


def _write_tuning_note(ctx, today):
    hist = tuning.history(ctx.conn, 50)
    cur = tuning.current(ctx.conn)
    rows = [[h["version"], h["created_utc"][:16], h["reason"], f"{h['ll_old']:.4f}", f"{h['ll_new']:.4f}"] for h in hist]
    ctx.vault.write("Model/Tuning.md", {"updated": today, "tags": ["model"]},
                    "# Model tuning\n\nParams are re-fit walk-forward after every graded slate and adopted only when they "
                    f"lower this season's log loss by at least {tuning.MIN_GAIN}.\n\nCurrent: `{json.dumps(cur.__dict__)}`\n\n"
                    + (ob.table(["Version", "When (UTC)", "Change", "Log loss before", "after"], rows) if rows else "_No changes yet._"))


def _write_scorecard_note(ctx, today):
    sc = scorecard.grade(ctx.conn)
    if not sc.get("n"):
        return
    calib = ob.table(["Bucket", "Games", "Predicted", "Actual"],
                     [[c["bucket"], c["n"], f"{c['predicted']:.0%}", f"{c['actual']:.0%}"] for c in sc["calibration"]])
    causes = review.cause_totals(ctx.conn)
    ctx.vault.write("Model/Scorecard.md", {"updated": today, "games": sc["n"], "acc": round(sc["acc"], 3),
                                           "brier": round(sc["brier"], 4), "tags": ["model"]},
                    f"# Scorecard\n\nRecord {sc['wins']}-{sc['n'] - sc['wins']} ({sc['acc']:.1%}) · Brier {sc['brier']:.3f} · "
                    f"log loss {sc['logloss']:.3f} · margin MAE {sc['mae_margin']:.1f}\n\n"
                    + (f"Elo on the same games: Brier {sc['elo_brier']:.3f}, accuracy {sc['elo_acc']:.1%}\n\n" if "elo_brier" in sc else "")
                    + (f"Market on {sc['mkt_n']} games: Brier {sc['mkt_brier']:.3f} (model {sc['model_brier_on_mkt']:.3f}), "
                       f"accuracy {sc['mkt_acc']:.1%}\n\n" if "mkt_brier" in sc else "")
                    + "## Calibration\n\n" + calib + "\n\n## Teams the model misses most\n\n"
                    + ", ".join(f"{ob.team_link(t)} {n}" for t, n in sc["most_missed"])
                    + "\n\n## AAR main causes (missed picks)\n\n" + (", ".join(f"{k} {n}" for k, n in causes.items()) or "none yet"))


def _write_predictions_note(ctx, today, tonight):
    if not tonight:
        return
    rows = []
    for p in tonight:
        allv = ctx.conn.execute("select version, created_utc, home_wp from predictions where game_id=? order by version",
                                (p["game_id"],)).fetchall()
        rows.append([f"{p['away']} @ {p['home']}", p["tip_sgt"], f"{p['home_wp']:.0%}", f"{p['margin']:+.1f}", f"{p['total']:.0f}",
                     "–" if p["market_wp"] is None else f"{p['market_wp']:.0%}",
                     " → ".join(f"v{r['version']} {r['home_wp']:.0%} ({r['created_utc'][11:16]}Z)" for r in allv)])
    ctx.vault.write(f"Model/Predictions/{ob.ym(today)}/{today}.md", {"date": today, "games": len(tonight), "tags": ["predictions"],
                                                                      "updated": today},
                    f"# Predictions {today}\n\nFrozen before tip-off; the last version before tip is the one graded.\n\n"
                    + ob.table(["Game", "Tip (SGT)", "Home win", "Margin", "Total", "Market", "Versions"], rows))


# ---------- injury check ----------

def injury_check(ctx, key=None):
    now = ctx.now_sgt()
    key = key or now.strftime("%Y-%m-%dT%H:%M")
    return guarded(ctx, "injury", key, _injury_check)


def _injury_check(ctx, key):
    conn = ctx.conn
    rows = espn.parse_injuries(ctx.client.injuries())
    changes = inj_mod.snapshot(conn, rows)
    today = ctx.now_sgt().date().isoformat()
    for c in changes:
        ctx.vault.append(f"Injuries/{today[:7]}.md", f"- {c['ts'][:16]}Z **{c['name']}** ({c['team']}) {c['old']} → {c['new']}"
                         + (f" · {c['detail']}" if c["detail"] else ""), header=f"# Injury log {today[:7]}\n")
    pend = json.loads(db.meta_get(conn, "pending_dates") or "[]")
    for d in pend:                                             # retry games that had no box score yet
        r = collector.collect_date(conn, ctx.client, d, ctx.s.espn_season)
        if not r.pending:
            db.meta_set(conn, "pending_dates", "[]")
    pending = inj_mod.pending_changes(conn)
    if not pending:
        return {"changes": 0}
    bump_state(ctx)
    ctx.cache.clear()
    st = state(ctx, today)
    tonight_before = {p["game_id"]: p for p in slate_view(ctx, today)}
    teams_hit = {c["team"] for c in pending}
    predict_tonight(ctx, st, today, why="injury update: " + ", ".join(sorted(teams_hit)))
    tonight_after = {p["game_id"]: p for p in slate_view(ctx, today)}
    title_now = {t: v["title"] for t, v in latest_sim(ctx).items()}
    rel = injury_alert.relevant(pending, title_now, st["values"]) if title_now else []
    alerts = []
    if rel:
        old_inj = dict(st["injuries"])
        for c in rel:
            if c["old"] == inj_mod.AVAILABLE:
                old_inj.pop(c["player_id"], None)
            else:
                old_inj[c["player_id"]] = {**old_inj.get(c["player_id"], {}), "status": c["old"], "team": c["team"],
                                           "name": c["name"], "detail": ""}
        seed = 42
        before = run_sim(ctx, st, n=2000, seed=seed, injuries_override=old_inj)
        after = run_sim(ctx, st, n=2000, seed=seed)
        for team in sorted({c["team"] for c in rel}):
            d_title = after[team]["title"] - before[team]["title"]
            g = next((p for p in tonight_after.values() if team in (p["home"], p["away"])), None)
            d_game = None
            if g and g["game_id"] in tonight_before:
                sgn = 1 if team == g["home"] else -1
                d_game = sgn * (g["home_wp"] - tonight_before[g["game_id"]]["home_wp"])
            if injury_alert.should_alert(d_title, d_game):
                alerts.append({"team": team, "changes": [c for c in rel if c["team"] == team], "before": before[team]["title"],
                               "after": after[team]["title"], "game": g, "game_before": tonight_before.get(g["game_id"]) if g else None,
                               "d_game": d_game})
    if alerts:
        ctx.tg.send(_alert_html(alerts))
        ctx.vault.activity("🚨", "injury alert sent", ", ".join(a["team"] for a in alerts))
    sent_ids = {c["id"] for a in alerts for c in a["changes"]}
    inj_mod.mark(conn, [c["id"] for c in pending if c["id"] in sent_ids], 2)
    inj_mod.mark(conn, [c["id"] for c in pending if c["id"] not in sent_ids], 1)
    ctx.vault.activity("🩹", "injury check", f"{len(pending)} change(s), {len(alerts)} alert(s)")
    return {"changes": len(pending), "alerts": len(alerts)}


def _alert_html(alerts):
    lines = [tp.title("alert", "INJURY ALERT", "contender odds moved"), ""]
    for a in alerts:
        d = a["after"] - a["before"]
        mk = "🟢" if d > 0 else "🔴"
        for c in a["changes"]:
            lines.append(f"{'🟢' if c['new'] in ('available', 'probable') else '🔴'} <b>{tp.e(c['name'])}</b> ({tp.e(c['team'])}) · "
                         f"{tp.e(c['old'])} → {tp.e(c['new'])}" + (f" · <i>{tp.e(c['detail'])}</i>" if c.get("detail") else ""))
        lines.append(f"   🏆 Title {a['before']:.1%} → {a['after']:.1%} {mk} {'▲' if d > 0 else '▼'}{abs(100 * d):.1f}")
        g, gb = a["game"], a["game_before"]
        if g and gb:
            side = a["team"]
            wp = lambda p: p["home_wp"] if side == p["home"] else 1 - p["home_wp"]
            lines.append(f"   🔮 Tonight {tp.e(g['away'])} @ {tp.e(g['home'])}: {side} {wp(gb):.0%} → {wp(g):.0%}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------- weekly ----------

def weekly(ctx, key=None):
    now = ctx.now_sgt()
    y, w, _ = now.isocalendar()
    key = key or f"{y}-W{w:02d}"
    return guarded(ctx, "weekly", key, _weekly)


def _weekly(ctx, key):
    conn, today = ctx.conn, ctx.now_sgt().date().isoformat()
    done = steps(ctx, "weekly", key)
    st = state(ctx, today)
    sim = latest_sim(ctx, "daily")
    if not sim or ctx.conn.execute("select max(date) from sims where kind='daily'").fetchone()[0] != today:
        sim = run_sim(ctx, st, n=10000, seed=int(today.replace("-", "")))
        store_sim(ctx, today, "daily", sim)
    el = elo_now(ctx)
    elo_s = {t: (el.get(t, 1505) - 1505) / 28.0 for t in ratings.TEAMS}      # ~28 Elo points per point of margin
    wins, losses = standings(ctx)
    pi, series = postseason_state(ctx, wins)
    p = st["params"]
    elo_sim = simulate.simulate(elo_s, elo_s, wins, losses, remaining_games(ctx), 100 / 28.0, p.sigma, n=10000, seed=7,
                                series_state=series, playin_done=pi)
    store_sim(ctx, today, "elo", elo_sim)
    mkt = market_title(ctx)
    R = st["ratings"]
    seasons = agg.team_season(conn, ctx.s.espn_season, R, before=today)
    sc = scorecard.grade(conn)
    week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    sc_week = scorecard.grade(conn, since=week_ago)
    summary = {"date": today, "teams": {t: {"rating": {k: round(R[t][k], 1) for k in ("off", "def", "net", "healthy_net")},
                                            **seasons.get(t, {}), "title": round(sim[t]["title"], 3),
                                            "elo_title": round(elo_sim[t]["title"], 3), "market": round(mkt.get(t, 0), 3)}
                                        for t in ratings.TEAMS},
               "scorecard": {k: sc.get(k) for k in ("n", "acc", "brier", "logloss", "most_missed")},
               "scorecard_week": {k: sc_week.get(k) for k in ("n", "acc", "brier")},
               "aar_causes": review.cause_totals(conn), "tuning": tuning.history(conn, 5)}
    verdict = None
    if "claude" not in done:
        verdict, _ = analyst.run("weekly.md", agg.compact(summary), ctx.s.model_daily, **({"runner": ctx.runner} if ctx.runner else {}))
        with db.LOCK:
            conn.execute("insert or replace into verdicts(date, kind, json) values (?,?,?)",
                         (today, "weekly", verdict.model_dump_json() if verdict else json.dumps({"fallback": True})))
        step_done(ctx, "weekly", key, "claude")
    else:
        verdict = _load_verdict(conn, today, "weekly")
    msgs = _weekly_msgs(ctx, key, st, sim, elo_sim, mkt, verdict, sc, sc_week)
    if "sent" not in done:
        ctx.tg.send_many(msgs, buttons=[("🎯 Scorecard", "/scorecard"), ("🔄 Tonight", "/game today")])
        step_done(ctx, "weekly", key, "sent")
    vt = {x.team: x for x in (verdict.teams if verdict else [])}
    power = sorted(ratings.TEAMS, key=lambda t: -R[t]["healthy_net"])
    body = [f"# Weekly {key}", "", "## Power rankings (healthy net rating)", "",
            ob.table(["#", "Team", "Net", "Healthy", "Record", "Title (model)", "Elo", "Market", "Verdict"],
                     [[i + 1, ob.team_link(t), f"{R[t]['net']:+.1f}", f"{R[t]['healthy_net']:+.1f}", seasons.get(t, {}).get("record"),
                       f"{sim[t]['title']:.1%}", f"{elo_sim[t]['title']:.1%}", f"{mkt.get(t, 0):.1%}",
                       vt[t].verdict if t in vt else ""] for i, t in enumerate(power)]),
            "", "## Scorecard", "", f"Season {sc.get('wins', 0)}-{sc.get('n', 0) - sc.get('wins', 0)} · Brier {sc.get('brier', 0):.3f}",
            "", "## Digest as sent", "", "\n\n".join(tp.plain(m) for m in msgs)]
    ctx.vault.write(f"Weekly/{key}.md", {"week": key, "date": today, "tags": ["weekly"], "updated": today}, "\n".join(body))
    _write_scorecard_note(ctx, today)
    _write_tuning_note(ctx, today)
    ctx.vault.activity("📅", "weekly report sent", key, key)
    return {"week": key}


def _weekly_msgs(ctx, key, st, sim, elo_sim, mkt, verdict, sc, sc_week):
    R = st["ratings"]
    power = sorted(ratings.TEAMS, key=lambda t: -R[t]["healthy_net"])[:10]
    b1 = [tp.title("weekly", "Power rankings", "healthy net rating")]
    b1 += [f"{i + 1}. <b>{t}</b> {R[t]['healthy_net']:+.1f}" for i, t in enumerate(power)]
    race = sorted(sim, key=lambda t: -sim[t]["title"])[:10]
    b2 = [tp.title("title", "Title odds", "model · Elo · market")]
    b2 += [f"<b>{t}</b> {sim[t]['title']:.0%} · {elo_sim[t]['title']:.0%} · {mkt.get(t, 0):.0%}" for t in race]
    vt = {x.team: x for x in (verdict.teams if verdict else [])}
    b3 = [tp.title("verdict", "All 30 teams")]
    b3 += [f"<b>{t}</b> {tp.e(vt[t].verdict[:110])}" for t in power + [t for t in ratings.TEAMS if t not in power] if t in vt] \
        or ["<i>Claude verdict unavailable this week: stats only.</i>"]
    b4 = [tp.title("scorecard", "Scorecard")]
    if sc.get("n"):
        b4.append(f"Season {sc['wins']}-{sc['n'] - sc['wins']} ({sc['acc']:.0%}) · Brier {sc['brier']:.3f}"
                  + (f" · week {sc_week['wins']}-{sc_week['n'] - sc_week['wins']}" if sc_week.get("n") else ""))
        b4 += [f"{c['bucket']}: {c['n']} games, predicted {c['predicted']:.0%}, actual {c['actual']:.0%}" for c in sc["calibration"]]
        if sc["most_missed"]:
            b4.append("Most missed: " + ", ".join(f"{t} {n}" for t, n in sc["most_missed"]))
    else:
        b4.append("<i>No graded picks yet.</i>")
    causes = review.cause_totals(ctx.conn)
    if causes:
        b4.append("🔍 AAR causes: " + ", ".join(f"{tp.e(k)} {n}" for k, n in causes.items()))
    hist = tuning.history(ctx.conn, 3)
    if hist:
        b4.append("🛠 Last model changes: " + "; ".join(tp.e(h["reason"]) for h in hist))
    blocks = [("power", "\n".join(b1)), ("title", "\n".join(b2))]
    if verdict and verdict.pick:
        blocks.append(("pick", tp.title("verdict", "Pick of the week", verdict.pick.team) + "\n"
                       + "\n".join(f"• {tp.e(r)}" for r in verdict.pick.reasons[:3])))
    blocks += [("teams", "\n".join(b3)), ("scorecard", "\n".join(b4))]
    return tp.fit(blocks, tp.title("weekly", "NBA WEEKLY", key))


# ---------- season end ----------

def season_over(ctx) -> bool:
    if db.meta_get(ctx.conn, "season_over") == "1":
        return True
    if ctx.conn.execute("select 1 from games where season=? and status='scheduled'", (ctx.s.espn_season,)).fetchone():
        return False
    last = ctx.conn.execute("select max(date) from games where season=? and season_type=3 and status='final'",
                            (ctx.s.espn_season,)).fetchone()[0]
    if not last or last[5:7] not in ("06", "07"):
        return False
    wins, _ = standings(ctx)
    _, series = postseason_state(ctx, wins)
    return any(4 in sc for sc in (series or {}).values())


def season_wrap(ctx):
    if db.meta_get(ctx.conn, "season_over") == "1":
        return
    last = ctx.conn.execute("""select * from games where season=? and season_type=3 and status='final' order by date desc limit 1""",
                            (ctx.s.espn_season,)).fetchone()
    champ = last["home"] if last["home_pts"] > last["away_pts"] else last["away"]
    first = ctx.conn.execute("select min(date) from sims where kind='daily'").fetchone()[0]
    path = [(r["date"], r["title"]) for r in ctx.conn.execute(
        "select date, title from sims where kind='daily' and team=? order by date", (champ,))]
    sc = scorecard.grade(ctx.conn)
    pick_on = lambda d: max(((r["team"], r["title"]) for r in ctx.conn.execute(
        "select team, title from sims where kind='daily' and date=?", (d,))), key=lambda x: x[1], default=("–", 0))
    lines = [tp.title("wrap", "SEASON WRAP", ctx.s.season), "", f"🏆 Champion: " + tp.Spoiler(ctx.s.spoiler)(f"<b>{tp.e(champ)}</b>")]
    if path:
        lines.append(f"Model gave them {path[0][1]:.0%} on {path[0][0]} and {path[-1][1]:.0%} on the eve of the title.")
        lines.append(f"Model favourite on day one: {pick_on(first)[0]}")
    if sc.get("n"):
        lines.append(f"🎯 Games: {sc['wins']}-{sc['n'] - sc['wins']} ({sc['acc']:.0%}) · Brier {sc['brier']:.3f}"
                     + (f" · market Brier {sc['mkt_brier']:.3f}" if "mkt_brier" in sc else ""))
    ctx.tg.send("\n".join(lines))
    db.meta_set(ctx.conn, "season_over", "1")
    ctx.vault.activity("🏁", "season over", f"champion {champ}")

# ---------- film room (21:00 SGT) ----------

def film(ctx, key=None):
    key = key or ctx.now_sgt().date().isoformat()
    return guarded(ctx, "film", key, _film)


def _film(ctx, key):
    from . import schemes, social_intel
    res = social_intel.run(ctx.conn, key, ctx.s.model_ask, reach=ctx.cache.get("reach"),
                           claude_runner=ctx.runner)
    added = res["added"]
    by_game = {}
    for it in added:
        by_game.setdefault(it["game_id"], []).append(it)
    for gid, items in by_game.items():
        g = ctx.conn.execute("select date, home, away from games where game_id=?", (gid,)).fetchone()
        rel = ob.game_rel(g["date"], g["away"], g["home"])
        for it in items:
            ctx.vault.append(rel, f"- **{it['team']}** {it['kind']}: **{it['label']}** (reported) · {it['summary']} · [{it['title']}]({it['url']})",
                             header="## Film Room")
    labels = {}
    if added:
        rows = agg.team_season(ctx.conn, ctx.s.espn_season, {})
        labels = {t: v["labels"] for t, v in schemes.team_profiles(ctx.conn, ctx.s.espn_season, rows).items() if not t.startswith("_")}
    clash = social_intel.contradictions(added, labels)
    if clash:
        lines = [tp.title("film", "FILM ROOM", "reported vs inferred"), ""]
        for c in clash[:6]:
            lines.append(f"🎬 <b>{tp.e(c['team'])}</b> played <b>{tp.e(c['label'])}</b> <i>(reported)</i>, "
                         f"but the play-by-play read was <i>{tp.e(c['inferred'])}</i>")
            lines.append(f"   {tp.e(c['summary'])} · <a href=\"{tp.e(c['url'])}\">video</a>")
        ctx.tg.send("\n".join(lines))
    ctx.vault.activity("🎬", "film room", f"{res['games']} games, {len(added)} labels"
                       + (f", {len(clash)} contradictions" if clash else "") + (f", stopped: {res['stop']}" if res["stop"] else ""))
    return {"games": res["games"], "labels": len(added), "contradictions": len(clash), "stop": res["stop"], "used": res["used"]}
