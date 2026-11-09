"""Season aggregates and the compact JSON Claude sees (never raw box scores)."""
import json

import pandas as pd

from . import espn


def four_factors(t, o) -> dict:
    """t, o: team_box rows (team and opponent) or summed dicts."""
    fga = max(1, t["fga"])
    return {"efg": round((t["fgm"] + 0.5 * t["fg3m"]) / fga, 3), "tov": round(t["tov"] / max(1, t["poss"]), 3),
            "orb": round(t["oreb"] / max(1, t["oreb"] + o["dreb"]), 3), "ftr": round(t["fta"] / fga, 3),
            "p3a": round(t["fg3a"] / fga, 3)}


def team_games(conn, season, before=None) -> pd.DataFrame:
    q = """select g.date, g.game_id, g.home, g.away, t.team, t.opp, t.is_home, t.pts, t.poss, t.clean_pts, t.clean_opp_pts,
                  t.clean_poss, t.fgm, t.fga, t.fg3m, t.fg3a, t.fta, t.ftm, t.tov, t.oreb, t.dreb,
                  o.pts opp_pts, o.fg3m opp_fg3m, o.fg3a opp_fg3a, o.dreb opp_dreb, o.oreb opp_oreb
           from team_box t join games g on g.game_id=t.game_id join team_box o on o.game_id=t.game_id and o.team=t.opp
           where g.season=? and g.season_type in (2,3,5) %s order by g.date""" % ("and g.date < ?" if before else "")
    return pd.DataFrame([dict(r) for r in conn.execute(q, [season] + ([before] if before else []))])


def team_season(conn, season, ratings_now: dict, before=None) -> dict:
    df = team_games(conn, season, before)
    if df.empty:
        return {}
    df["win"] = df.pts > df.opp_pts
    df["net100"] = 100 * (df.clean_pts - df.clean_opp_pts) / df.clean_poss.clip(lower=1)
    lg3 = df.fg3m.sum() / max(1, df.fg3a.sum())
    wpct = df.groupby("team").win.mean().to_dict()
    out = {}
    for t, g in df.groupby("team"):
        n = len(g)
        pf, pa = g.pts.sum(), g.opp_pts.sum()
        close = g[(g.pts - g.opp_pts).abs() <= 5]
        good = g[g.opp.map(lambda x: wpct.get(x, 0) > 0.5)]
        opp_net = [ratings_now.get(o, {}).get("net", 0.0) for o in g.opp]
        out[t] = {
            "record": f"{int(g.win.sum())}-{n - int(g.win.sum())}",
            "home": f"{int(g[g.is_home == 1].win.sum())}-{int((g.is_home == 1).sum() - g[g.is_home == 1].win.sum())}",
            "road": f"{int(g[g.is_home == 0].win.sum())}-{int((g.is_home == 0).sum() - g[g.is_home == 0].win.sum())}",
            "pyth_wins": round(n / (1 + (float(pa) / max(1.0, float(pf))) ** 14), 1),
            "net_clean": round(100 * (g.clean_pts.sum() - g.clean_opp_pts.sum()) / max(1, g.clean_poss.sum()), 1),
            "net_vs_500": round(good.net100.mean(), 1) if len(good) else None, "games_vs_500": len(good),
            "roll5": round(g.net100.tail(5).mean(), 1), "roll10": round(g.net100.tail(10).mean(), 1),
            "sos_played": round(sum(opp_net) / n, 1),
            "opp_3p": round(g.opp_fg3m.sum() / max(1, g.opp_fg3a.sum()), 3), "lg_3p": round(lg3, 3),
            "close": f"{int(close.win.sum())}-{len(close) - int(close.win.sum())}",
            "four_factors": four_factors(g[["fgm", "fga", "fg3m", "fg3a", "fta", "tov", "poss", "oreb"]].sum(),
                                         {"dreb": g.opp_dreb.sum()}),
            "pace": round(g.poss.mean(), 1), "games": n,
        }
        o = out[t]
        o["luck_flags"] = [f for f, on in (
            ("opponents shooting cold from 3", o["opp_3p"] < lg3 - 0.025),
            ("opponents shooting hot from 3", o["opp_3p"] > lg3 + 0.025),
            ("close-game record above expectation", int(close.win.sum()) - len(close) / 2 >= 3),
            ("close-game record below expectation", len(close) / 2 - int(close.win.sum()) >= 3),
            ("record ahead of point margin", g.win.sum() - o["pyth_wins"] >= 3),
            ("record behind point margin", o["pyth_wins"] - g.win.sum() >= 3)) if on]
    return out


def sos_remaining(conn, season, ratings_now) -> dict:
    rows = conn.execute("select home, away from games where season=? and status='scheduled' and season_type=2", (season,))
    acc = {}
    for r in rows:
        for t, o in ((r["home"], r["away"]), (r["away"], r["home"])):
            acc.setdefault(t, []).append(ratings_now.get(o, {}).get("net", 0.0))
    return {t: round(sum(v) / len(v), 1) for t, v in acc.items() if v}


def star_workload(conn, season, team, n=2) -> list:
    q = """select p.name, count(*) gp, avg(p.minutes) mpg from player_box p join games g on g.game_id=p.game_id
           where g.season=? and p.team=? and p.dnp=0 group by p.player_id order by sum(p.minutes) desc limit ?"""
    total = conn.execute("select count(*) from team_box t join games g on g.game_id=t.game_id where g.season=? and t.team=?",
                         (season, team)).fetchone()[0]
    return [{"name": r["name"], "mpg": round(r["mpg"], 1), "missed": total - r["gp"]} for r in conn.execute(q, (season, team, n))]


def game_brief(conn, game_id) -> dict:
    g = conn.execute("select * from games where game_id=?", (game_id,)).fetchone()
    tb = {r["team"]: dict(r) for r in conn.execute("select * from team_box where game_id=?", (game_id,))}
    h, a = tb.get(g["home"]), tb.get(g["away"])
    ex = conn.execute("select * from game_excitement where game_id=?", (game_id,)).fetchone()
    top = conn.execute("""select name, team, pts, reb, ast, fgm, fga, fg3m, fg3a, minutes,
                                 (pts + 0.4*fgm - 0.7*fga - 0.4*(fta-ftm) + 0.7*oreb + 0.3*dreb + stl + 0.7*ast + 0.7*blk - 0.4*pf - tov) gs
                          from player_box where game_id=? and dnp=0 order by gs desc limit 4""", (game_id,)).fetchall()
    names = {r["player_id"]: r["name"] for r in conn.execute("select player_id, name from player_box where game_id=?", (game_id,))}
    wpa = [(names.get(r["key"], r["key"]), r["team"], round(r["wpa"], 3))
           for r in conn.execute("select * from player_wpa where game_id=? and key not like 'TEAM:%' order by wpa desc", (game_id,))]
    out = {"game_id": game_id, "matchup": f"{g['away']} @ {g['home']}", "score": f"{g['away_pts']}-{g['home_pts']}",
           "winner": g["home"] if g["home_pts"] > g["away_pts"] else g["away"], "ot": g["ot"], "source": g["source"],
           "excitement": {"score": ex["score"], "stars": ex["stars"], "reasons": ex["reasons"]} if ex else None,
           "top_players": [{"name": r["name"], "team": r["team"], "line": f"{r['pts']}p {r['reb']}r {r['ast']}a "
                            f"{r['fgm']}-{r['fga']} FG {r['fg3m']}-{r['fg3a']} 3P", "game_score": round(r["gs"], 1)} for r in top],
           "wpa_best": wpa[:2], "wpa_worst": wpa[-2:][::-1] if len(wpa) > 2 else []}
    if h and a:
        out["pace"] = round((h["poss"] + a["poss"]) / 2, 1)
        out["factors"] = {g["home"]: four_factors(h, a), g["away"]: four_factors(a, h)}
        out["reb_margin"] = {g["home"]: (h["oreb"] + h["dreb"]) - (a["oreb"] + a["dreb"])}
        out["tov"] = {g["home"]: h["tov"], g["away"]: a["tov"]}
    return out


def size_ok(summary: dict, limit_chars=60000) -> bool:
    """~15k tokens ≈ 60k chars of JSON."""
    return len(json.dumps(summary, separators=(",", ":"))) <= limit_chars


def compact(summary: dict) -> str:
    return json.dumps(summary, separators=(",", ":"), default=str)


def team_name(t):
    return espn.NAMES.get(t, t)
