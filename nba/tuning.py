"""Model parameters + the self-improvement loop (spec §6b).

After each graded slate: re-fit params walk-forward on every game this season. A candidate replaces the
current params only if it lowers the season log loss by at least MIN_GAIN. Every adoption is an
append-only `model_params` row with before/after log loss. Claude never sets numbers.
"""
import itertools
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

MIN_GAIN = 0.002
MIN_GAMES = 300      # below this a season's games can't outvote a 10-season backtest (2026-11-09: 146 games 'adopted' noise)


@dataclass(frozen=True)
class Params:
    # starting values = backtest tuned on 2016-17..2020-21, validated on 2021-22..2025-26 (2026-11-09)
    lam: float = 10.0            # ridge penalty
    half_life: float | None = 45.0   # days; recent games weigh more (None = all games equal)
    hca: float = 2.0             # home edge, points
    sigma: float = 11.0          # spread of the actual margin around the projection
    b2b: float = 2.0             # points lost on the 2nd night of a back-to-back
    injury_scale: float = 0.5    # box value -> points of margin
    rest: float = 0.0            # points per day of rest advantage (capped at 2 days); 0 until a backtest shows it helps
    travel: float = 0.0          # points per time zone crossed since the previous game (away minus home)


def current(conn) -> Params:
    r = conn.execute("select json from model_params order by version desc limit 1").fetchone()
    return Params(**json.loads(r[0])) if r else Params()


def save(conn, p: Params, ll_old, ll_new, reason):
    from . import db
    with db.LOCK:
        conn.execute("insert into model_params(created_utc, json, ll_old, ll_new, reason) values (?,?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(asdict(p)), ll_old, ll_new, reason))


def history(conn, n=10):
    return [dict(r) for r in conn.execute("select * from model_params order by version desc limit ?", (n,))]


GRID = {"half_life": [None, 45.0, 25.0], "hca": [1.5, 2.0, 2.5, 3.0, 3.5], "sigma": [11.0, 12.0, 13.0, 14.0],
        "b2b": [0.0, 1.0, 2.0, 3.0], "rest": [0.0, 0.5, 1.0], "travel": [0.0, 0.5, 1.0]}


def candidates(p: Params):
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        yield replace(p, **dict(zip(keys, combo)))


# ---------- walk-forward evaluation ----------

def _rest_travel(df):
    """Rest days (capped 0..3) and time zones crossed since the previous game, for home and away, from the frame itself."""
    import pandas as pd
    from .insights import ARENA
    d = pd.to_datetime(df.date)
    last = {}
    rh, ra, th, ta = [], [], [], []
    for home, away, x in zip(df.home, df.away, d):
        for team, R, T in ((home, rh, th), (away, ra, ta)):
            prev = last.get(team)
            R.append(min(3, max(0, (x - prev[0]).days - 1)) if prev else 2)
            T.append(abs(ARENA.get(home, (0, 0, 0))[2] - ARENA.get(prev[1], (0, 0, 0))[2]) if prev else 0)
        last[home] = (x, home)
        last[away] = (x, home)
    return rh, ra, th, ta


def _b2b_flags(df):
    import pandas as pd
    d = pd.to_datetime(df.date)
    played = set(zip(df.home, d)) | set(zip(df.away, d))
    one = pd.Timedelta(days=1)
    bh = [(t, x - one) in played for t, x in zip(df.home, d)]
    ba = [(t, x - one) in played for t, x in zip(df.away, d)]
    return bh, ba


def walk_forward_arrays(df, prior, p: "Params", warmup_days=7):
    """Score each game with ratings fit only on earlier days. Returns numpy arrays for fast grid search."""
    import numpy as np
    from . import ratings
    df = df.sort_values("date").reset_index(drop=True)
    bh, ba = _b2b_flags(df)
    rest_h, rest_a, tz_h, tz_a = _rest_travel(df)
    days = sorted(df.date.unique())
    first = days[0]
    import pandas as pd
    out = {k: [] for k in ("base", "neutral", "b2b_h", "b2b_a", "y", "margin", "rest_edge", "tz_edge")}
    for day in days:
        if (pd.Timestamp(day) - pd.Timestamp(first)).days < warmup_days:
            continue
        past = df[df.date < day]
        R = ratings.current(past, prior, p, day)
        for i in df.index[df.date == day]:
            g = df.loc[i]
            rh, ra = R[g.home], R[g.away]
            out["base"].append((rh["net"] - ra["net"]) * (rh["pace"] + ra["pace"]) / 200)
            out["neutral"].append(float(g.get("neutral", 0) or 0))
            out["b2b_h"].append(float(bh[i])); out["b2b_a"].append(float(ba[i]))
            out["rest_edge"].append(float(max(-2, min(2, rest_h[i] - rest_a[i])))); out["tz_edge"].append(float(tz_a[i] - tz_h[i]))
            m = (g.home_pts_raw - g.away_pts_raw) if "home_pts_raw" in g else (g.home_pts - g.away_pts)
            out["margin"].append(m); out["y"].append(1.0 if m > 0 else 0.0)
    return {k: np.array(v) for k, v in out.items()}


def logloss(a, hca, sigma, b2b, rest=0.0, travel=0.0):
    import numpy as np
    from scipy.special import ndtr
    m = a["base"] + hca * (1 - a["neutral"]) - b2b * a["b2b_h"] + b2b * a["b2b_a"]
    if "rest_edge" in a:
        m = m + rest * a["rest_edge"] + travel * a["tz_edge"]
    pr = np.clip(ndtr(m / sigma), 1e-6, 1 - 1e-6)
    return float(-np.mean(a["y"] * np.log(pr) + (1 - a["y"]) * np.log(1 - pr)))


def _injury_scale(conn, cur):
    """Re-weight availability on graded predictions that stored it. Returns (best_scale, ll_cur, ll_best, n)."""
    import numpy as np
    from scipy.special import ndtr
    from . import scorecard
    rows = []
    for r in scorecard.graded_rows(conn):
        p = conn.execute("""select reason from predictions where game_id=? order by version desc""", (r["game_id"],)).fetchone()
        parts = (json.loads(p[0]) or {}).get("parts") or {} if p else {}
        if parts.get("avail_raw"):
            rows.append((r["margin"] - cur.injury_scale * parts["avail_raw"], parts["avail_raw"], r["y"]))
    if len(rows) < 30:
        return cur.injury_scale, None, None, len(rows)
    base, av, y = (np.array(x) for x in zip(*rows))

    def ll(s):
        pr = np.clip(ndtr((base + s * av) / cur.sigma), 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(pr) + (1 - y) * np.log(1 - pr)))
    best = min([0.25, 0.4, 0.5, 0.65, 0.8, 1.0], key=ll)
    return best, ll(cur.injury_scale), ll(best), len(rows)


def tune(conn, df, prior, cur: "Params" = None, min_games=MIN_GAMES) -> dict:
    """Search the grid walk-forward on this season's games; adopt only on a real gain. Returns a report dict."""
    cur = cur or current(conn)
    if len(df) < min_games:
        return {"adopted": False, "reason": f"only {len(df)} games, need {min_games}", "params": asdict(cur)}
    arrays = {}

    def arr(hl):
        if hl not in arrays:
            arrays[hl] = walk_forward_arrays(df, prior, replace(cur, half_life=hl))
        return arrays[hl]
    ll_cur = logloss(arr(cur.half_life), cur.hca, cur.sigma, cur.b2b, cur.rest, cur.travel)
    best, ll_best = cur, ll_cur
    for c in candidates(cur):
        ll = logloss(arr(c.half_life), c.hca, c.sigma, c.b2b, c.rest, c.travel)
        if ll < ll_best - 1e-12:
            best, ll_best = c, ll
    report = {"n": int(len(arr(cur.half_life)["y"])), "ll_old": ll_cur, "ll_new": ll_best, "params": asdict(cur)}
    changes = []
    if ll_cur - ll_best >= MIN_GAIN:
        changes = [f"{k} {getattr(cur, k)}→{getattr(best, k)}" for k in GRID if getattr(cur, k) != getattr(best, k)]
    else:
        best = cur
    s, ll_s_cur, ll_s_best, n_inj = _injury_scale(conn, best)
    if ll_s_cur is not None and ll_s_cur - ll_s_best >= MIN_GAIN:
        changes.append(f"injury_scale {best.injury_scale}→{s}")
        best = replace(best, injury_scale=s)
    if changes:
        save(conn, best, ll_cur, ll_best, "; ".join(changes))
        report.update(adopted=True, changes=changes, params=asdict(best))
    else:
        report.update(adopted=False, reason=f"no candidate beat the current model by {MIN_GAIN} log loss")
    return report
