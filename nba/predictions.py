"""Game projections and the frozen prediction log.

margin (home) = (net_h - net_a) * pace / 100 + hca - b2b(home) + b2b(away) + availability; win prob = Phi(margin / sigma).
Predictions are append-only (DB triggers) and refused at or after tip-off; the graded one is the last before tip.
"""
import json
import math
from datetime import datetime, timedelta, timezone

from . import db
from . import elo as elo_mod


def win_prob(margin, sigma):
    return 0.5 * (1 + math.erf(margin / (sigma * math.sqrt(2))))


def project(rh: dict, ra: dict, mu: float, p, neutral=False, b2b_h=False, b2b_a=False, avail_h=0.0, avail_a=0.0,
            rest_edge=0.0, tz_edge=0.0):
    pace = (rh["pace"] + ra["pace"]) / 2
    base = (rh["net"] - ra["net"]) * pace / 100
    hca = 0.0 if neutral else p.hca
    b2b = -p.b2b * b2b_h + p.b2b * b2b_a
    rt = getattr(p, "rest", 0.0) * rest_edge + getattr(p, "travel", 0.0) * tz_edge
    margin = base + hca + b2b + (avail_h - avail_a) + rt
    total = pace * ((mu + rh["off"] - ra["def"]) + (mu + ra["off"] - rh["def"])) / 100
    parts = {"base": round(base, 3), "hca": hca, "b2b": b2b, "rest_travel": round(rt, 3),
             "avail_raw": round((avail_h - avail_a) / p.injury_scale, 3) if p.injury_scale else 0.0}
    return {"margin": margin, "home_wp": win_prob(margin, p.sigma), "total": total, "parts": parts}


def american_to_prob(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def market_wp(home_ml, away_ml):
    ph, pa = american_to_prob(home_ml), american_to_prob(away_ml)
    if ph is None or pa is None:
        return None
    return ph / (ph + pa)                      # remove the bookmaker margin


def _parse_ts(s):
    s = str(s).replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T", 1)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TipPassed(Exception):
    pass


def store(conn, game_id, pred: dict, now: datetime | None = None, min_change=0.005) -> int | None:
    """Append a new version. Returns the version, or None if it barely differs from the last one."""
    now = now or datetime.now(timezone.utc)
    with db.LOCK:
        g = conn.execute("select tip_utc from games where game_id=?", (game_id,)).fetchone()
        if not g or now >= _parse_ts(g["tip_utc"]):
            raise TipPassed(game_id)
        last = conn.execute("select version, home_wp from predictions where game_id=? order by version desc limit 1",
                            (game_id,)).fetchone()
        if last and abs(last["home_wp"] - pred["home_wp"]) < min_change:
            return None
        v = (last["version"] if last else 0) + 1
        conn.execute("""insert into predictions(game_id, version, created_utc, home_wp, margin, total, elo_wp, market_wp, reason)
                        values (?,?,?,?,?,?,?,?,?)""",
                     (game_id, v, now.isoformat(timespec="seconds"), pred["home_wp"], pred["margin"], pred["total"],
                      pred.get("elo_wp"), pred.get("market_wp"), json.dumps({"parts": pred.get("parts"),
                                                                              "why": pred.get("why", ""), "inj": pred.get("inj", {})})))
        return v


def graded(conn, game_id):
    """The prediction that counts: last version stored before tip-off."""
    g = conn.execute("select tip_utc from games where game_id=?", (game_id,)).fetchone()
    rows = conn.execute("select * from predictions where game_id=? order by version", (game_id,)).fetchall()
    tip = _parse_ts(g["tip_utc"])
    ok = [r for r in rows if _parse_ts(r["created_utc"]) < tip]
    return ok[-1] if ok else None


def latest(conn, game_id):
    return conn.execute("select * from predictions where game_id=? order by version desc limit 1", (game_id,)).fetchone()


def b2b(conn, team, d: str) -> bool:
    prev = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()
    return conn.execute("select 1 from games where date=? and (home=? or away=?) and status!='postponed'",
                        (prev, team, team)).fetchone() is not None


def predict_slate(conn, d: str, ctx, why="", now=None) -> list[dict]:
    """ctx: dict with ratings (team->row, '_mu'), params, values, injuries, elo, odds (game_id->odds).
    Stores a version for every scheduled game on US date d; returns the projections."""
    out = []
    p, R = ctx["params"], ctx["ratings"]
    from . import ratings as rmod
    for g in conn.execute("select * from games where date=? and status='scheduled'", (d,)).fetchall():
        h, a = g["home"], g["away"]
        if h not in R or a not in R:
            continue
        av_h = rmod.availability_delta(h, ctx["values"], ctx["injuries"])
        av_a = rmod.availability_delta(a, ctx["values"], ctx["injuries"])
        sc = ctx.get("sched", {})
        ch, ca = sc.get((g["game_id"], h)) or {}, sc.get((g["game_id"], a)) or {}
        rest_edge = max(-2, min(2, min(3, ch.get("rest") if ch.get("rest") is not None else 2) - min(3, ca.get("rest") if ca.get("rest") is not None else 2)))
        tz_edge = abs(ca.get("tz") or 0) - abs(ch.get("tz") or 0)
        pr = project(R[h], R[a], R["_mu"], p, bool(g["neutral"]), b2b(conn, h, d), b2b(conn, a, d), av_h, av_a, rest_edge, tz_edge)
        el = ctx.get("elo", {})
        pr["elo_wp"] = elo_mod.prob(el.get(h, 1505), el.get(a, 1505), bool(g["neutral"])) if el else None
        o = ctx.get("odds", {}).get(g["game_id"]) or {}
        pr["market_wp"] = market_wp(o.get("home_ml"), o.get("away_ml"))
        pr["why"] = why
        pr["inj"] = {pid: v["status"] for pid, v in ctx["injuries"].items() if v.get("team") in (h, a)}
        try:
            pr["version"] = store(conn, g["game_id"], pr, now)
        except TipPassed:
            continue
        out.append({"game_id": g["game_id"], "home": h, "away": a, "tip_utc": g["tip_utc"], **pr})
    return out
