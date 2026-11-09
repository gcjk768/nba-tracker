"""Opponent-adjusted team ratings (points per 100 possessions), blended with a preseason prior.

Game frame columns (one row per game): game_id, date, home, away, home_pts, away_pts, home_poss, away_poss,
optional neutral, h3pm, h3pa, a3pm, a3pa. Same frame comes from the DB (live) or hoopR (prior, backtest).
"""
import math
from datetime import date as _date

import numpy as np
import pandas as pd

from . import espn

TEAMS = sorted(espn.TEAM_IDS)
IDX = {t: i for i, t in enumerate(TEAMS)}
LG3 = 0.36          # league 3P% used for luck regression when the frame has no better estimate
LUCK = 0.5          # share of 3P% luck removed (opponent 3P% is mostly noise)


def blend_weight(g: int) -> float:
    """Weight on this season: ~30/70 at 10 games, rising to 80/20 by the All-Star break (~55 games)."""
    if g <= 0:
        return 0.0
    if g < 10:
        return 0.30 * g / 10
    return min(0.80, 0.30 + 0.50 * (g - 10) / 45)


def game_frame(conn, season: int, before: str | None = None, season_types=(2, 3, 5)) -> pd.DataFrame:
    q = """select g.game_id, g.date, g.home, g.away, g.neutral, g.season_type,
                  h.clean_pts home_pts, a.clean_pts away_pts, h.clean_poss home_poss, a.clean_poss away_poss,
                  h.pts home_pts_raw, a.pts away_pts_raw, h.poss home_poss_raw, a.poss away_poss_raw,
                  h.fg3m h3pm, h.fg3a h3pa, a.fg3m a3pm, a.fg3a a3pa
           from games g join team_box h on h.game_id=g.game_id and h.is_home=1
                        join team_box a on a.game_id=g.game_id and a.is_home=0
           where g.season=? and g.season_type in (%s) %s order by g.date""" % (
        ",".join("?" * len(season_types)), "and g.date < ?" if before else "")
    args = [season, *season_types] + ([before] if before else [])
    return pd.DataFrame([dict(r) for r in conn.execute(q, args)])


def fit(df: pd.DataFrame, lam=10.0, half_life=None, asof=None):
    """Ridge: ortg(team row) = mu + off[team] - def[opp] ± hca/2. Returns (off, def, hca, mu) in pts/100."""
    if df.empty:
        z = {t: 0.0 for t in TEAMS}
        return z, dict(z), 0.0, 110.0
    luck = _luck(df)
    poss_h = df.home_poss.to_numpy(float)
    poss_a = df.away_poss.to_numpy(float)
    poss = (poss_h + poss_a) / 2
    o_h = 100 * (df.home_pts.to_numpy(float) + luck) / poss
    o_a = 100 * (df.away_pts.to_numpy(float) - luck) / poss
    n, T = len(df), len(TEAMS)
    X = np.zeros((2 * n, 2 * T + 1))
    y = np.concatenate([o_h, o_a])
    hi = df.home.map(IDX).to_numpy()
    ai = df.away.map(IDX).to_numpy()
    r = np.arange(n)
    side = 1 - df.get("neutral", pd.Series(0, index=df.index)).fillna(0).to_numpy(float)
    X[r, hi] = 1; X[r, T + ai] = -1; X[r, 2 * T] = 0.5 * side
    X[n + r, ai] = 1; X[n + r, T + hi] = -1; X[n + r, 2 * T] = -0.5 * side
    w = np.ones(2 * n)
    if half_life and asof:
        age = (pd.Timestamp(asof) - pd.to_datetime(df.date)).dt.days.to_numpy(float)
        w = np.tile(0.5 ** (age / half_life), 2)
    mu = float(np.average(y, weights=w))
    pen = np.full(2 * T + 1, lam); pen[-1] = 1e-6
    A = X.T @ (X * w[:, None]) + np.diag(pen)
    beta = np.linalg.solve(A, X.T @ (w * (y - mu)))
    off = {t: float(beta[IDX[t]]) for t in TEAMS}
    de = {t: float(beta[T + IDX[t]]) for t in TEAMS}
    return off, de, float(beta[-1]), mu


def _luck(df):
    """Points to add to the home side to undo half of both teams' 3P% luck (0 if no 3P columns)."""
    if "h3pa" not in df or df.h3pa.isna().all():
        return np.zeros(len(df))
    lg = (df.h3pm.sum() + df.a3pm.sum()) / max(1, df.h3pa.sum() + df.a3pa.sum()) or LG3
    h = (df.h3pm - lg * df.h3pa).fillna(0).to_numpy(float) * 3
    a = (df.a3pm - lg * df.a3pa).fillna(0).to_numpy(float) * 3
    return LUCK * (a - h)


def pace(df: pd.DataFrame) -> dict:
    """Possessions per game by team (raw, whole game)."""
    if df.empty:
        return {t: 99.0 for t in TEAMS}
    hp = df.get("home_poss_raw", df.home_poss); ap = df.get("away_poss_raw", df.away_poss)
    g = pd.concat([pd.DataFrame({"t": df.home, "p": (hp + ap) / 2}), pd.DataFrame({"t": df.away, "p": (hp + ap) / 2})])
    m = g.groupby("t").p.mean().to_dict()
    lg = float(g.p.mean())
    return {t: float(m.get(t, lg)) for t in TEAMS}


def games_played(df: pd.DataFrame) -> dict:
    c = pd.concat([df.home, df.away]).value_counts().to_dict() if not df.empty else {}
    return {t: int(c.get(t, 0)) for t in TEAMS}


def prior_from_season(df_last: pd.DataFrame, lam=10.0) -> dict:
    """Preseason prior: last season's opponent-adjusted (off, def), pulled 1/3 back to the league average."""
    off, de, _, _ = fit(df_last, lam)
    return {t: (2 / 3 * off[t], 2 / 3 * de[t]) for t in TEAMS}


def current(df: pd.DataFrame, prior: dict, params, asof: str) -> dict:
    """Blended rating per team: {off, def, net, prior_net, blend_w, games, pace} (pts/100)."""
    off, de, hca, mu = fit(df, params.lam, params.half_life, asof)
    gp, pc = games_played(df), pace(df)
    out = {}
    for t in TEAMS:
        w = blend_weight(gp[t])
        po, pd_ = prior.get(t, (0.0, 0.0))
        o, d = w * off[t] + (1 - w) * po, w * de[t] + (1 - w) * pd_
        out[t] = {"off": o, "def": d, "net": o + d, "prior_net": po + pd_, "blend_w": w, "games": gp[t], "pace": pc[t]}
    out["_mu"], out["_hca100"] = mu, hca
    return out


# ---------- players: availability adjustment (ponytail: box-score proxy until Phase 2 on/off) ----------

REPL_GMSC36 = 8.0


def player_values(pb: pd.DataFrame, team_games: dict, scale: float) -> dict:
    """pb: player_box rows this season (player_id, name, team, minutes, game score 'gmsc', dnp).
    value = points of margin per game the team loses when he sits (vs a replacement player)."""
    if pb.empty:
        return {}
    pb = pb[pb.dnp == 0]
    g = pb.groupby(["player_id"]).agg(name=("name", "last"), team=("team", "last"), mins=("minutes", "sum"),
                                       gmsc=("gmsc", "sum"), gp=("minutes", "size"))
    out = {}
    for pid, r in g.iterrows():
        if r.mins <= 0:
            continue
        g36 = 36 * r.gmsc / r.mins
        k = r.mins / (r.mins + 300)                        # shrink small samples to replacement level
        g36 = k * g36 + (1 - k) * REPL_GMSC36
        mpg = r.mins / r.gp
        val = max(0.0, scale * (g36 - REPL_GMSC36) * mpg / 48)
        frac = min(1.0, r.gp / max(1, team_games.get(r.team, r.gp)))
        out[str(pid)] = {"name": r["name"], "team": r.team, "value": val, "mpg": mpg, "frac": frac, "gp": int(r.gp)}
    return out


STATUS_W = {"out": 1.0, "suspended": 1.0, "doubtful": 0.75, "questionable": 0.5, "probable": 0.1}


def availability_delta(team: str, values: dict, injuries: dict, healthy=False) -> float:
    """Points/game to add to the team's margin. Current: minus out players (in proportion to how much his
    presence is already baked into the rating). Healthy: plus the share of games he missed, if not out for season."""
    d = 0.0
    for pid, v in values.items():
        if v["team"] != team:
            continue
        inj = injuries.get(pid)
        if healthy:
            if inj and "season" in (inj.get("detail") or "").lower():
                continue
            d += v["value"] * (1 - v["frac"])
        elif inj:
            d -= v["value"] * v["frac"] * STATUS_W.get(inj["status"], 0.0)
    return d


def player_frame(conn, season: int, before: str | None = None) -> pd.DataFrame:
    q = """select p.*, (p.pts + 0.4*p.fgm - 0.7*p.fga - 0.4*(p.fta-p.ftm) + 0.7*p.oreb + 0.3*p.dreb + p.stl
                       + 0.7*p.ast + 0.7*p.blk - 0.4*p.pf - p.tov) gmsc
           from player_box p join games g on g.game_id=p.game_id
           where g.season=? and g.season_type in (2,3,5) %s""" % ("and g.date < ?" if before else "")
    return pd.DataFrame([dict(r) for r in conn.execute(q, [season] + ([before] if before else []))])
