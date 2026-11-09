"""After Action Review for every missed pick (spec §6b).

Breaks the margin error (actual - predicted, from the picked side) into causes, in points:
availability, shooting luck, free throws, turnovers, rebounding; whatever is left is noise.
The causes always sum to the error. The largest negative cause is the verdict.
"""
import json

from . import db
from . import predictions as P

PPP = 1.12           # league points per possession, value of an extra/lost possession
LESSONS = {
    "availability": "Players missed or were limited after the pick was frozen: the injury check now reruns before tip, "
                    "and injury weight is re-tuned on graded games.",
    "shooting luck": "3-point variance: not a model error, ratings already regress 3P% luck.",
    "free throws": "Free-throw variance: noise, no change.",
    "turnovers": "Lost the possession battle on turnovers: real if it repeats, tracked in the team's trend.",
    "rebounding": "Lost the possession battle on the glass: real if it repeats, tracked in the team's trend.",
    "noise": "Inside the normal spread of NBA results: no change.",
}


def _season_rates(conn, team, season, before):
    r = conn.execute("""select sum(fg3m) m, sum(fg3a) a, sum(ftm) ftm, sum(fta) fta, sum(tov) tov, sum(poss) poss,
                               sum(oreb) oreb, count(*) n
                        from team_box t join games g on g.game_id=t.game_id
                        where t.team=? and g.season=? and g.date<?""", (team, season, before)).fetchone()
    opp = conn.execute("""select sum(t.dreb) dreb from team_box t join games g on g.game_id=t.game_id
                          where t.opp=? and g.season=? and g.date<?""", (team, season, before)).fetchone()
    if not r or not r["n"]:
        return None
    return {"p3": r["m"] / max(1, r["a"]), "ft": r["ftm"] / max(1, r["fta"]), "tov": r["tov"] / max(1, r["poss"]),
            "orb": r["oreb"] / max(1, r["oreb"] + (opp["dreb"] or 0))}


def _side_box(conn, game_id, team):
    return conn.execute("select * from team_box where game_id=? and team=?", (game_id, team)).fetchone()


def review_game(conn, game_id, values=None, injuries_at_pick=None) -> dict | None:
    g = conn.execute("select * from games where game_id=?", (game_id,)).fetchone()
    pred = P.graded(conn, game_id)
    if not g or not pred or g["home_pts"] is None:
        return None
    home_pick = pred["home_wp"] >= 0.5
    S, O = (g["home"], g["away"]) if home_pick else (g["away"], g["home"])
    sign = 1 if home_pick else -1
    pred_m = sign * pred["margin"]
    act_m = sign * (g["home_pts"] - g["away_pts"])
    err = act_m - pred_m
    bs, bo = _side_box(conn, game_id, S), _side_box(conn, game_id, O)
    rs, ro = _season_rates(conn, S, g["season"], g["date"]), _season_rates(conn, O, g["season"], g["date"])
    causes = {}
    if bs and bo and rs and ro:
        luck = lambda b, r: 3 * (b["fg3m"] - r["p3"] * b["fg3a"])
        causes["shooting luck"] = luck(bs, rs) - luck(bo, ro)
        ft = lambda b, r: b["ftm"] - r["ft"] * b["fta"]
        causes["free throws"] = ft(bs, rs) - ft(bo, ro)
        tov = lambda b, r: (b["tov"] - r["tov"] * b["poss"]) * PPP
        causes["turnovers"] = tov(bo, ro) - tov(bs, rs)
        orb = lambda b, ob, r: (b["oreb"] - r["orb"] * (b["oreb"] + ob["dreb"])) * PPP
        causes["rebounding"] = orb(bs, bo, rs) - orb(bo, bs, ro)
    causes["availability"] = _availability(conn, game_id, S, values or {}, injuries_at_pick or {}) \
        - _availability(conn, game_id, O, values or {}, injuries_at_pick or {})
    causes["noise"] = err - sum(causes.values())
    causes = {k: round(v, 2) for k, v in causes.items()}
    worst = min(causes, key=causes.get)
    missed = not ((pred["home_wp"] >= 0.5) == (g["home_pts"] > g["away_pts"]))
    out = {"game_id": game_id, "date": g["date"], "pick": S, "opp": O, "pick_wp": max(pred["home_wp"], 1 - pred["home_wp"]),
           "pred_margin": round(pred_m, 1), "actual_margin": act_m, "error": round(err, 1), "missed": missed,
           "causes": causes, "main": worst, "lesson": LESSONS[worst],
           "scratches": _scratches(conn, game_id, values or {}, injuries_at_pick or {})}
    return out


def _availability(conn, game_id, team, values, inj_at_pick):
    """Points lost: rotation players who were expected (not listed out at pick time) but sat or were cut short."""
    lost = 0.0
    rows = {r["player_id"]: r for r in conn.execute("select * from player_box where game_id=? and team=?", (game_id, team))}
    for pid, v in values.items():
        if v["team"] != team or v["mpg"] < 15 or inj_at_pick.get(pid, {}).get("status") in ("out", "suspended"):
            continue
        r = rows.get(pid)
        played = 0.0 if (r is None or r["dnp"]) else r["minutes"]
        share = max(0.0, 1 - played / v["mpg"]) if played < 0.6 * v["mpg"] else 0.0
        lost -= v["value"] * share
    return lost


def _scratches(conn, game_id, values, inj_at_pick):
    names = []
    for pid, v in values.items():
        if v["mpg"] < 15 or inj_at_pick.get(pid, {}).get("status") in ("out", "suspended"):
            continue
        r = conn.execute("select dnp, minutes from player_box where game_id=? and player_id=?", (game_id, pid)).fetchone()
        g = conn.execute("select home, away from games where game_id=?", (game_id,)).fetchone()
        if v["team"] in (g["home"], g["away"]) and (r is None or r["dnp"] or r["minutes"] < 0.6 * v["mpg"]):
            names.append(v["name"])
    return names


def needs_review(conn, game_id) -> bool:
    g = conn.execute("select home_pts, away_pts from games where game_id=?", (game_id,)).fetchone()
    p = P.graded(conn, game_id)
    if not g or not p or g["home_pts"] is None:
        return False
    miss = (p["home_wp"] >= 0.5) != (g["home_pts"] > g["away_pts"])
    return miss or abs((g["home_pts"] - g["away_pts"]) - p["margin"]) >= 15


def review_slate(conn, game_ids, values=None, injuries_at_pick=None) -> list[dict]:
    out = []
    for gid in game_ids:
        if not needs_review(conn, gid):
            continue
        r = review_game(conn, gid, values, injuries_at_pick)
        if r:
            with db.LOCK:
                conn.execute("insert or replace into aar(game_id, date, json) values (?,?,?)", (gid, r["date"], json.dumps(r)))
            out.append(r)
    return out


def cause_totals(conn, since=None) -> dict:
    tot = {}
    for r in conn.execute("select json from aar" + (" where date >= ?" if since else ""), ([since] if since else [])):
        a = json.loads(r[0])
        if a.get("missed"):
            tot[a["main"]] = tot.get(a["main"], 0) + 1
    return dict(sorted(tot.items(), key=lambda x: -x[1]))
