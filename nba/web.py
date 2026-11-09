"""Dashboard web page: animated title race, odds over time, power rankings, tonight's picks, last night (behind a
reveal), model scorecard. Rendered live from the DB on every request (own read connection per request, WAL),
read-only, stdlib only. Env: WEB_PORT (default 8000 inside the container; 0 = off)."""
import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import aggregator as agg
from . import db, espn, injuries as inj_mod, jobs, players, ratings, review, schemes, scorecard, tuning
from . import predictions as P

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).parent / "dashboard.html"
STATIC = Path(__file__).parent / "static"
TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
TYPES = {".woff2": "font/woff2", ".png": "image/png", ".txt": "text/plain"}


def ensure_assets(data_dir: Path) -> dict:
    """Team colours + logos (light and dark variants) from ESPN, fetched once and cached under data/
    (same idea as supermarket-hunter's store logos: keeps working if the CDN moves). Best effort."""
    meta_f = Path(data_dir) / "teams.json"
    if meta_f.exists():
        return json.loads(meta_f.read_text(encoding="utf-8"))
    import httpx
    out = {}
    try:
        j = httpx.get(TEAMS_URL, timeout=30, headers={"User-Agent": "nba-tracker/1.0 (personal, low volume)"}).json()
        logos = Path(data_dir) / "logos"
        logos.mkdir(parents=True, exist_ok=True)
        for x in j["sports"][0]["leagues"][0]["teams"]:
            t = x["team"]
            code = t["abbreviation"]
            out[code] = {"name": t.get("displayName"), "short": t.get("shortDisplayName"),
                         "color": "#" + (t.get("color") or "888888"), "alt": "#" + (t.get("alternateColor") or "cccccc")}
            for variant, suffix in (("500", ""), ("500-dark", "-dark")):
                r = httpx.get("https://a.espncdn.com/combiner/i", timeout=30, params={
                    "img": f"/i/teamlogos/nba/{variant}/{code.lower()}.png", "w": 128, "h": 128, "transparent": "true"})
                if r.status_code == 200 and r.content[1:4] == b"PNG":
                    (logos / f"{code}{suffix}.png").write_bytes(r.content)
        meta_f.write_text(json.dumps(out), encoding="utf-8")
    except Exception as e:
        log.warning("team assets failed (page falls back to codes): %s", e)
    return out


def data(ctx) -> dict:
    """Everything the page shows, as one JSON-able dict. ctx needs .conn, .s, .now_sgt()."""
    c = ctx.conn
    today = ctx.now_sgt().date().isoformat()
    dates = [r[0] for r in c.execute("select distinct date from sims where kind='daily' order by date")]
    series = {}
    for r in c.execute("select date, team, title from sims where kind='daily' order by date"):
        series.setdefault(r["team"], {})[r["date"]] = r["title"]
    latest = dates[-1] if dates else None
    mkt = jobs.market_title(ctx)
    elo_sim = jobs.latest_sim(ctx, "elo")
    sim = jobs.latest_sim(ctx, "daily")
    wins, losses = jobs.standings(ctx)
    rdate = c.execute("select max(date) from ratings").fetchone()[0]
    spark = {}
    for r in c.execute("select team, date, net from ratings order by date"):
        spark.setdefault(r["team"], []).append(round(r["net"], 2))
    rankings = []
    for r in c.execute("select * from ratings where date=? order by healthy_net desc", (rdate,)) if rdate else []:
        t = r["team"]
        rankings.append({"team": t, "name": espn.NAMES.get(t, t), "conf": "East" if t in espn.EAST else "West",
                         "record": f"{wins.get(t, 0)}-{losses.get(t, 0)}", "net": round(r["net"], 1),
                         "healthy": round(r["healthy_net"], 1), "off": round(r["off"], 1), "def": round(r["def"], 1),
                         "title": round(sim.get(t, {}).get("title", 0), 4), "playoffs": round(sim.get(t, {}).get("playoffs", 0), 3),
                         "elo_title": round(elo_sim[t]["title"], 4) if t in elo_sim else None,
                         "market": round(mkt[t], 4) if t in mkt else None, "spark": spark.get(t, [])[-30:]})
    ever_top = sorted({t for d in dates for t in sorted(series, key=lambda x: -series[x].get(d, 0))[:10]})
    last_night = c.execute("select max(date) from games where status='final' and source is not null").fetchone()[0]
    games = []
    for g in c.execute("select * from games where date=? and status='final' and source is not null order by tip_utc", (last_night,)) \
            if last_night else []:
        ex = c.execute("select * from game_excitement where game_id=?", (g["game_id"],)).fetchone()
        p = P.graded(c, g["game_id"])
        top = c.execute("""select name, team, pts, reb, ast from player_box where game_id=? and dnp=0
                           order by (pts + reb + ast) desc limit 1""", (g["game_id"],)).fetchone()
        games.append({"matchup": f"{g['away']} @ {g['home']}", "away": g["away"], "home": g["home"],
                      "stars": ex["stars"] if ex else 1, "excitement": ex["score"] if ex else 0,
                      "reasons": [x for x in (ex["reasons"] or "").split(",") if x] if ex else [],
                      "away_pts": g["away_pts"], "home_pts": g["home_pts"], "ot": g["ot"],
                      "pick": None if not p else (g["home"] if p["home_wp"] >= 0.5 else g["away"]),
                      "pick_wp": None if not p else round(max(p["home_wp"], 1 - p["home_wp"]), 3),
                      "pick_right": None if not p else (p["home_wp"] >= 0.5) == (g["home_pts"] > g["away_pts"]),
                      "top": f"{top['name']} {top['pts']}p {top['reb']}r {top['ast']}a" if top else ""})
    games.sort(key=lambda x: -x["excitement"])
    sc = scorecard.grade(c)
    injuries, values = player_inputs(ctx)
    tonight_teams = {x for g in jobs.slate_view(ctx, today) for x in (g["home"], g["away"])}
    status = players.league_status(c, ctx.s.espn_season, injuries, values, tonight_teams)
    prof_all = profiles(ctx)
    rnow = {r["team"]: {"off": r["off"], "def": r["def"], "net": r["healthy"]} for r in rankings}
    styles = schemes.league_styles(prof_all, rnow, {r["team"]: r["record"] for r in rankings},
                                   {t: v.get("pace") for t, v in agg.team_season(c, ctx.s.espn_season, {}).items()},
                                   {t: schemes._opp_tov(c, ctx.s.espn_season, t, None) for t in rnow})
    aars = [json.loads(r[0]) for r in c.execute("select json from aar order by date desc limit 8")]
    teams_meta = ensure_assets(ctx.s.data_dir) if (Path(ctx.s.data_dir) / "teams.json").exists() else {}
    return {
        "teams": teams_meta,
        "season": ctx.s.season, "today": today, "updated": c.execute(
            "select max(finished) from job_runs where status='done'").fetchone()[0], "spoiler": ctx.s.spoiler,
        "title": {"dates": dates, "teams": {t: [round(series[t].get(d, 0), 4) for d in dates] for t in ever_top},
                  "market": {t: round(v, 4) for t, v in mkt.items()}, "latest": latest},
        "rankings": rankings, "status": status[:40],
        "styles": styles,
        "playbooks": playbooks(ctx, styles),
        "kb": kb_payload(ctx),
        "ato_league": {k: v for k, v in ato_profiles(ctx).get("_league", {}).items()},
        "what_wins": schemes.what_wins(styles), "tonight": jobs.slate_view(ctx, today), "last_night": {"date": last_night, "games": games},
        "scorecard": {k: sc.get(k) for k in ("n", "wins", "acc", "brier", "logloss", "elo_brier", "elo_acc", "mkt_brier",
                                             "mkt_acc", "mkt_n", "model_brier_on_mkt", "calibration", "most_missed")},
        "aar": {"causes": review.cause_totals(c), "recent": [{k: a.get(k) for k in ("date", "pick", "opp", "pick_wp", "error", "main")}
                                                            for a in aars]},
        "tuning": [{"version": h["version"], "when": (h["created_utc"] or "")[:10], "reason": h["reason"],
                    "ll_old": h["ll_old"], "ll_new": h["ll_new"]} for h in tuning.history(c, 8)],
        "params": tuning.current(c).__dict__,
    }


_CACHE = {}


def _key(c):
    return (c.execute("select count(*) from plays").fetchone()[0], c.execute("select max(ts) from injury_state").fetchone()[0],
            c.execute("select max(date) from ratings").fetchone()[0])


def profiles(ctx) -> dict:
    """Team scheme profiles, cached until new games / ratings / injuries arrive."""
    k = ("profiles", _key(ctx.conn))
    if k not in _CACHE:
        rows = agg.team_season(ctx.conn, ctx.s.espn_season, {})
        _CACHE.clear()
        _CACHE[k] = schemes.team_profiles(ctx.conn, ctx.s.espn_season, rows)
    return _CACHE[k]


def ato_profiles(ctx) -> dict:
    from . import ato
    k = ("ato", _key(ctx.conn))
    if k not in _CACHE:
        names = {r[0]: r[1] for r in ctx.conn.execute("select player_id, max(name) from player_box group by player_id")}
        _CACHE[k] = ato.profiles(ato.possessions(ctx.conn, ctx.s.espn_season), names)
    return _CACHE[k]


def weaknesses_all(ctx) -> dict:
    from . import scout
    k = ("weak", _key(ctx.conn))
    if k not in _CACHE:
        P = profiles(ctx)
        rows = agg.team_season(ctx.conn, ctx.s.espn_season, {})
        vs = {t: schemes.record_vs_styles(ctx.conn, ctx.s.espn_season, t, P) for t in P if not t.startswith("_")}
        _CACHE[k] = scout.weaknesses(ctx.conn, ctx.s.espn_season, P, ato_profiles(ctx), vs,
                                     {t: r.get("luck_flags", []) for t, r in rows.items()})
    return _CACHE[k]


def playbooks(ctx, styles: list[dict]) -> list[dict]:
    """One card per team: what they run most on offense, after timeouts, on defense, and named sets reported."""
    from . import social_intel
    P, A = profiles(ctx), ato_profiles(ctx)
    lo, ld = P["_league"]["offense"], P["_league"]["defense"]
    out = []
    for r in styles:
        t = r["team"]
        o, d, a = P[t]["offense"], P[t]["defense"], A.get(t)
        mix = sorted(((k, v, v - lo.get(f"kind:{k}", 0), P[t]["pct_off"].get(f"kind:{k}")) for k, v in o["kind"].items()), key=lambda x: -x[1])
        pb = social_intel.playbook(ctx.conn, t)
        out.append({
            "team": t, "conf": "East" if t in espn.EAST else "West", "record": r["record"],
            "off_rank": r["off_rank"], "def_rank": r["def_rank"], "net_rank": r["net_rank"],
            "offense": {"mix": [{"kind": k, "share": round(v, 3), "vs_lg": round(dv, 3), "pct": pc} for k, v, dv, pc in mix[:5]],
                        "assisted": round(o["assisted"], 3), "transition": round(o["transition"], 3), "three_rate": round(o["three_rate"], 3),
                        "rim_rate": round(o["rim_rate"], 3), "pps": round(o["pps"], 3), "styles": r["off_styles"]},
            "ato": None if not a else {"n": a["n"], "own_n": a["own_n"], "ppp": round(a["ppp"], 3), "rank": a["rank"],
                                        "score_rate": round(a["score_rate"], 3), "tov_rate": round(a["tov_rate"], 3),
                                        "late_n": a["late_n"], "late_ppp": None if a["late_ppp"] is None else round(a["late_ppp"], 3),
                                        "top_kind": max(a["kind"], key=a["kind"].get) if a["n"] else None,
                                        "top_kind_share": round(max(a["kind"].values()), 3) if a["n"] else 0,
                                        "three_rate": round(a["three_rate"], 3), "go_to": [x["name"] for x in a["top_shooters"][:2]],
                                        "small_sample": a["small_sample"], "named": pb["ato"]},
            "defense": {"styles": r["def_styles"], "rim_rate": round(d["rim_rate"], 3), "three_rate": round(d["three_rate"], 3),
                        "pps": round(d["pps"], 3), "rim_pct": P[t]["pct_def"].get("rim_rate"), "three_pct": P[t]["pct_def"].get("three_rate"),
                        "forced_tov_pct": P[t]["forced_tov_pct"],
                        "top_blocker": P[t]["top_blocker"]["name"] if P[t]["top_blocker"]["per_game"] >= 1.0 else None},
            "named": pb["counts"], "named_n": len(pb["recent"]),
            "beat": weaknesses_all(ctx).get(t, [])[:3],
        })
    return out


COUNTER_TITLES = {"drop": "Drop coverage", "switch": "Switching", "blitz": "Blitz / trap", "hedge": "Hard hedge", "ice": "Ice",
                  "zone": "Zone", "pack the paint": "Packing the paint", "run shooters off": "Running shooters off the line",
                  "turnover-forcing": "Turnover-forcing pressure", "rim protector": "Rim-protector anchor"}


def kb_payload(ctx) -> dict:
    from . import playbook_kb as kb, social_intel
    c = ctx.conn
    reported = {}
    for r in c.execute("select team, label from film_items where kind='coverage'"):
        reported.setdefault(r["team"], set()).add(r["label"])
    ev = schemes.counter_evidence(c, ctx.s.espn_season, profiles(ctx), {t: list(v) for t, v in reported.items()})
    labels = {r[0] for r in c.execute("select distinct label from film_items")}
    for key, (ctrs, _) in kb.COUNTERS.items():
        labels.add(key)
        labels.update(ctrs)
    counters = [{"key": k, "title": COUNTER_TITLES.get(k, k), "counters": v[0], "why": v[1], "source": "coaching principle + measured",
                 "evidence": ev.get(k)} for k, v in kb.COUNTERS.items()]
    counters.sort(key=lambda x: -len((x["evidence"] or {}).get("teams", [])))
    return {"plays": {l: {"kind": kb.PLAYS.get(l, (None, None))[0], "desc": kb.PLAYS.get(l, (None, None))[1]} for l in labels},
            "counters": counters, "videos": social_intel.video_map(c), "league_pps": ev.get("_league_pps")}


def player_inputs(ctx):
    st_values = ratings.player_values(ratings.player_frame(ctx.conn, ctx.s.espn_season),
                                      ratings.games_played(ratings.game_frame(ctx.conn, ctx.s.espn_season)), tuning.current(ctx.conn).injury_scale)
    return inj_mod.state(ctx.conn), st_values


def team_data(ctx, team: str) -> dict | None:
    team = espn.team_code(team)
    if not team:
        return None
    c, season = ctx.conn, ctx.s.espn_season
    prof_all = profiles(ctx)
    injuries, values = player_inputs(ctx)
    se = agg.team_season(c, season, {}).get(team, {})
    r = c.execute("select * from ratings where team=? order by date desc limit 1", (team,)).fetchone()
    sim = jobs.latest_sim(ctx, "daily").get(team, {})
    mkt = jobs.market_title(ctx).get(team)
    recent = [{"date": g["date"], "opp": g["away"] if g["home"] == team else g["home"], "home": g["home"] == team,
               "score": f"{g['home_pts'] if g['home'] == team else g['away_pts']}-{g['away_pts'] if g['home'] == team else g['home_pts']}",
               "won": (g["home_pts"] > g["away_pts"]) == (g["home"] == team)}
              for g in c.execute("""select * from games where (home=? or away=?) and status='final' and home_pts is not null
                                    order by date desc limit 6""", (team, team))]
    upcoming = [{"date": g["date"], "opp": g["away"] if g["home"] == team else g["home"], "home": g["home"] == team,
                 "tip": jobs.tip_sgt(g["tip_utc"])}
                for g in c.execute("select * from games where (home=? or away=?) and status='scheduled' order by tip_utc limit 5", (team, team))]
    prof = prof_all.get(team, {})
    return {"team": team, "name": espn.NAMES.get(team, team), "conf": "East" if team in espn.EAST else "West",
            "season_row": {k: se.get(k) for k in ("record", "home", "road", "pyth_wins", "roll5", "roll10", "sos_played", "close",
                                                  "luck_flags", "four_factors", "pace", "opp_3p", "lg_3p")},
            "rating": None if not r else {"net": round(r["net"], 1), "off": round(r["off"], 1), "def": round(r["def"], 1),
                                          "healthy": round(r["healthy_net"], 1), "games": r["games"]},
            "title": sim.get("title"), "playoffs": sim.get("playoffs"), "finals": sim.get("finals"), "market": mkt,
            "offense": prof.get("offense"), "defense": prof.get("defense"), "pct_off": prof.get("pct_off"), "pct_def": prof.get("pct_def"),
            "league": prof_all.get("_league"), "labels": prof.get("labels", []), "not_measurable": schemes.NOT_MEASURABLE,
            "pace_pct": prof.get("pace_pct"), "forced_tov_pct": prof.get("forced_tov_pct"), "top_blocker": prof.get("top_blocker"),
            "roster": players.roster(c, season, team, injuries, values),
            "vs_styles": schemes.record_vs_styles(c, season, team, prof_all),
            "film": _film(c, team, prof.get("labels", [])),
            "ato": ato_profiles(ctx).get(team), "ato_league": ato_profiles(ctx).get("_league"),
            "weaknesses": weaknesses_all(ctx).get(team, []),
            "recent": recent, "upcoming": upcoming, "kinds": list(schemes.KINDS), "zones": list(schemes.ZONES)}


def _film(c, team, inferred):
    from . import social_intel
    pb = social_intel.playbook(c, team)
    clash = social_intel.contradictions([dict(r) for r in pb["recent"]], {team: inferred})
    agree = {"drop": "Concedes the mid-range (drop-style)", "blitz": "Turnover-forcing (aggressive, traps or blitzes likely)",
             "zone": "Packs the paint"}
    for r in pb["recent"]:
        r["agrees_with"] = agree.get(r["label"]) if any(l["label"] == agree.get(r["label"]) for l in inferred) else None
        r["contradicts"] = next((x["inferred"] for x in clash if x["url"] == r["url"] and x["label"] == r["label"]), None)
    return pb


def render(payload: dict) -> str:
    blob = json.dumps(payload, separators=(",", ":"), default=str).replace("</", "<\\/")   # safe inside <script>
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)


def handler_for(ctx):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/static/") or path.startswith("/logo/"):
                return self._file(path)
            if path.startswith("/team/") and path.endswith(".json"):
                conn = db.connect(ctx.s.db_path)
                try:
                    td = team_data(replace(ctx, conn=conn, cache={}), path[6:-5])
                finally:
                    conn.close()
                if not td:
                    return self.send_error(404)
                return self._send(200, "application/json", json.dumps(td, default=str).encode())
            if path not in ("/", "/data.json", "/healthz"):
                self.send_error(404)
                return
            if path == "/healthz":
                return self._send(200, "text/plain", b"ok")
            conn = db.connect(ctx.s.db_path)
            try:
                payload = data(replace(ctx, conn=conn, cache={}))
            finally:
                conn.close()
            if path == "/data.json":
                return self._send(200, "application/json", json.dumps(payload, default=str).encode())
            self._send(200, "text/html; charset=utf-8", render(payload).encode("utf-8"))

        def _file(self, path):
            name = path.rsplit("/", 1)[-1]
            if not name or "/" in name or ".." in name or "\\" in name:
                return self.send_error(404)
            base = STATIC if path.startswith("/static/") else Path(ctx.s.data_dir) / "logos"
            f = base / name
            if not f.is_file() or f.suffix not in TYPES:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", TYPES[f.suffix])
            self.send_header("Cache-Control", "public, max-age=604800")
            body = f.read_bytes()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


def start(ctx, port: int):
    threading.Thread(target=ensure_assets, args=(ctx.s.data_dir,), daemon=True, name="assets").start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler_for(ctx))
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on :%d", port)
    return srv
