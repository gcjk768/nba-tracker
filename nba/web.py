"""Dashboard web page: animated title race, odds over time, power rankings, tonight's picks, last night (behind a
reveal), model scorecard. Rendered live from the DB on every request (own read connection per request, WAL),
read-only, stdlib only. Env: WEB_PORT (default 8000 inside the container; 0 = off)."""
import json
import logging
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, espn, jobs, review, scorecard, tuning
from . import predictions as P

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).parent / "dashboard.html"


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
    aars = [json.loads(r[0]) for r in c.execute("select json from aar order by date desc limit 8")]
    return {
        "season": ctx.s.season, "today": today, "updated": c.execute(
            "select max(finished) from job_runs where status='done'").fetchone()[0], "spoiler": ctx.s.spoiler,
        "title": {"dates": dates, "teams": {t: [round(series[t].get(d, 0), 4) for d in dates] for t in ever_top},
                  "market": {t: round(v, 4) for t, v in mkt.items()}, "latest": latest},
        "rankings": rankings, "tonight": jobs.slate_view(ctx, today), "last_night": {"date": last_night, "games": games},
        "scorecard": {k: sc.get(k) for k in ("n", "wins", "acc", "brier", "logloss", "elo_brier", "elo_acc", "mkt_brier",
                                             "mkt_acc", "mkt_n", "model_brier_on_mkt", "calibration", "most_missed")},
        "aar": {"causes": review.cause_totals(c), "recent": [{k: a.get(k) for k in ("date", "pick", "opp", "pick_wp", "error", "main")}
                                                            for a in aars]},
        "tuning": [{"version": h["version"], "when": (h["created_utc"] or "")[:10], "reason": h["reason"],
                    "ll_old": h["ll_old"], "ll_new": h["ll_new"]} for h in tuning.history(c, 8)],
        "params": tuning.current(c).__dict__,
    }


def render(payload: dict) -> str:
    blob = json.dumps(payload, separators=(",", ":"), default=str).replace("</", "<\\/")   # safe inside <script>
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", blob)


def handler_for(ctx):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
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
    srv = ThreadingHTTPServer(("0.0.0.0", port), handler_for(ctx))
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on :%d", port)
    return srv
