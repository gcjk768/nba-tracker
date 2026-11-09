"""Player status, role and shot profile for the dashboard and /status. Box-score + injury + play-by-play only."""
from collections import defaultdict

from . import schemes

ROTATION_MPG = 15.0


def _ts(pts, fga, fta):
    return pts / max(1e-9, 2 * (fga + 0.44 * fta)) if fga or fta else None


def roster(conn, season, team, injuries: dict, values: dict, shots=None, steals=None) -> list[dict]:
    """Everyone who has played for `team` this season (latest team), most minutes first."""
    rows = conn.execute("""
        select p.player_id, max(p.name) name, max(p.position) pos, count(*) gp, sum(p.starter) gs, sum(p.minutes) mins,
               sum(p.pts) pts, sum(p.reb) reb, sum(p.ast) ast, sum(p.stl) stl, sum(p.blk) blk, sum(p.tov) tov,
               sum(p.fgm) fgm, sum(p.fga) fga, sum(p.fg3m) fg3m, sum(p.fg3a) fg3a, sum(p.ftm) ftm, sum(p.fta) fta,
               sum(p.plus_minus) pm
        from player_box p join games g on g.game_id=p.game_id
        where g.season=? and p.team=? and p.dnp=0 group by p.player_id order by mins desc""", (season, team)).fetchall()
    team_games = conn.execute("""select count(*) from team_box t join games g on g.game_id=t.game_id
                                 where g.season=? and t.team=?""", (season, team)).fetchone()[0] or 1
    shots = shots if shots is not None else schemes.shots(conn, season)
    by_player = defaultdict(list)
    for s in shots:
        if s["team"] == team:
            by_player[s["player_id"]].append(s)
    out = []
    for r in rows:
        pid = r["player_id"]
        last5 = conn.execute("""select p.minutes, p.pts, p.reb, p.ast, p.starter, g.date, g.home, g.away, g.game_id
                                from player_box p join games g on g.game_id=p.game_id
                                where p.player_id=? and g.season=? order by g.date desc limit 5""", (pid, season)).fetchall()
        mpg = r["mins"] / r["gp"]
        l5 = [x["minutes"] for x in last5]
        l5mpg = sum(l5) / len(l5) if l5 else 0
        inj = injuries.get(pid)
        S = by_player.get(pid, [])
        prof = schemes._mix(S) if S else None
        role_flags = []
        if last5 and sum(x["starter"] for x in last5) >= 4 and r["gs"] < r["gp"] / 2:
            role_flags.append("moved into the starting five")
        if last5 and sum(x["starter"] for x in last5) <= 1 and r["gs"] >= r["gp"] / 2:
            role_flags.append("moved to the bench")
        if mpg >= 10 and l5mpg >= 1.2 * mpg:
            role_flags.append("minutes up")
        if mpg >= 10 and l5mpg <= 0.8 * mpg:
            role_flags.append("minutes down")
        out.append({
            "player_id": pid, "name": r["name"], "pos": r["pos"], "gp": r["gp"], "gs": r["gs"], "missed": team_games - r["gp"],
            "role": "starter" if r["gs"] >= r["gp"] / 2 else "bench", "rotation": mpg >= ROTATION_MPG,
            "mpg": round(mpg, 1), "last5_mpg": round(l5mpg, 1),
            "ppg": round(r["pts"] / r["gp"], 1), "rpg": round(r["reb"] / r["gp"], 1), "apg": round(r["ast"] / r["gp"], 1),
            "spg": round(r["stl"] / r["gp"], 1), "bpg": round(r["blk"] / r["gp"], 1), "tpg": round(r["tov"] / r["gp"], 1),
            "fg": f"{r['fgm']}-{r['fga']}", "fg3": f"{r['fg3m']}-{r['fg3a']}", "ft": f"{r['ftm']}-{r['fta']}",
            "fg3_pct": round(r["fg3m"] / r["fg3a"], 3) if r["fg3a"] else None,
            "ts": round(_ts(r["pts"], r["fga"], r["fta"]) or 0, 3), "pm_pg": round(r["pm"] / r["gp"], 1),
            "value": round(values.get(pid, {}).get("value", 0), 2),
            "status": inj["status"] if inj else "available", "detail": inj.get("detail", "") if inj else "",
            "flags": role_flags,
            "sample_small": r["gp"] < 5 or r["mins"] < 100,
            "shots": None if not prof else {"n": prof["n"], "kind": {k: round(v, 3) for k, v in prof["kind"].items()},
                                            "zone": {k: round(v, 3) for k, v in prof["zone"].items()},
                                            "zone_fg": {k: round(v, 3) for k, v in prof["zone_fg"].items()},
                                            "assisted": round(prof["assisted"], 3), "transition": round(prof["transition"], 3)},
            "last5": [{"date": x["date"], "opp": x["away"] if x["home"] == team else x["home"], "min": int(x["minutes"]),
                       "line": f"{x['pts']}p {x['reb']}r {x['ast']}a", "game_id": x["game_id"],
                       "wpa": _wpa(conn, x["game_id"], pid)} for x in last5],
        })
    return out


def _wpa(conn, game_id, pid):
    r = conn.execute("select wpa from player_wpa where game_id=? and key=?", (game_id, pid)).fetchone()
    return round(r["wpa"], 3) if r else None


def league_status(conn, season, injuries: dict, values: dict, teams_tonight=()) -> list[dict]:
    """Rotation players who are not fully available, most important first; tonight's teams first."""
    out = []
    for pid, inj in injuries.items():
        v = values.get(pid)
        if not v or v.get("mpg", 0) < 12 or inj["status"] in ("available", "probable"):
            continue
        out.append({"player_id": pid, "name": inj["name"], "team": inj["team"], "status": inj["status"],
                    "detail": inj.get("detail", ""), "mpg": round(v["mpg"], 1), "value": round(v["value"], 2),
                    "tonight": inj["team"] in teams_tonight})
    return sorted(out, key=lambda x: (not x["tonight"], -x["value"]))
