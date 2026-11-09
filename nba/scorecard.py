"""Grade frozen predictions against results."""
import math

from . import predictions as P

BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


def graded_rows(conn, since: str | None = None, until: str | None = None):
    q = "select * from games where status='final' and home_pts is not null"
    args = []
    if since:
        q += " and date >= ?"; args.append(since)
    if until:
        q += " and date <= ?"; args.append(until)
    out = []
    for g in conn.execute(q + " order by date", args).fetchall():
        p = P.graded(conn, g["game_id"])
        if not p:
            continue
        y = 1 if g["home_pts"] > g["away_pts"] else 0
        out.append({"game_id": g["game_id"], "date": g["date"], "home": g["home"], "away": g["away"], "y": y,
                    "p": p["home_wp"], "elo": p["elo_wp"], "mkt": p["market_wp"], "margin": p["margin"],
                    "actual": g["home_pts"] - g["away_pts"], "correct": (p["home_wp"] >= 0.5) == bool(y)})
    return out


def _ll(p, y):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def summarize(rows) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    out = {"n": n, "wins": sum(r["correct"] for r in rows),
           "brier": sum((r["p"] - r["y"]) ** 2 for r in rows) / n,
           "logloss": sum(_ll(r["p"], r["y"]) for r in rows) / n,
           "mae_margin": sum(abs(r["actual"] - r["margin"]) for r in rows) / n}
    out["acc"] = out["wins"] / n
    for k in ("elo", "mkt"):
        sub = [r for r in rows if r[k] is not None]
        if sub:
            out[f"{k}_brier"] = sum((r[k] - r["y"]) ** 2 for r in sub) / len(sub)
            out[f"{k}_acc"] = sum((r[k] >= 0.5) == bool(r["y"]) for r in sub) / len(sub)
            out[f"{k}_n"] = len(sub)
            out[f"model_brier_on_{k}"] = sum((r["p"] - r["y"]) ** 2 for r in sub) / len(sub)
    calib = []
    for lo, hi in BUCKETS:
        b = [r for r in rows if lo <= max(r["p"], 1 - r["p"]) < hi]
        if b:
            calib.append({"bucket": f"{int(lo*100)}-{min(100, int(hi*100))}%", "n": len(b),
                          "predicted": sum(max(r["p"], 1 - r["p"]) for r in b) / len(b),
                          "actual": sum(r["correct"] for r in b) / len(b)})
    out["calibration"] = calib
    miss = {}
    for r in rows:
        if not r["correct"]:
            for t in (r["home"], r["away"]):
                miss[t] = miss.get(t, 0) + 1
    out["most_missed"] = sorted(miss.items(), key=lambda x: -x[1])[:5]
    return out


def grade(conn, since=None, until=None) -> dict:
    return summarize(graded_rows(conn, since, until))
