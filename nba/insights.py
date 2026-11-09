"""What decides games: win recipe, shot quality vs shot-making, rest and travel, clutch, quarters, box extras,
referee crews and betting-line movement. All measured from stored box scores, play-by-play, schedule and odds.
"""
import math
from collections import defaultdict
from datetime import date

from . import schemes

# arena: (lat, lon, standard UTC offset in winter, altitude metres). Neutral-site games use the home team's arena.
ARENA = {
    "ATL": (33.757, -84.396, -5, 320), "BOS": (42.366, -71.062, -5, 5), "BKN": (40.683, -73.976, -5, 10),
    "CHA": (35.225, -80.839, -5, 230), "CHI": (41.881, -87.674, -6, 180), "CLE": (41.496, -81.688, -5, 200),
    "DAL": (32.790, -96.810, -6, 140), "DEN": (39.749, -105.008, -7, 1609), "DET": (42.341, -83.055, -5, 190),
    "GS": (37.768, -122.388, -8, 5), "HOU": (29.751, -95.362, -6, 15), "IND": (39.764, -86.156, -5, 220),
    "LAC": (33.945, -118.341, -8, 30), "LAL": (34.043, -118.267, -8, 90), "MEM": (35.138, -90.051, -6, 80),
    "MIA": (25.781, -80.188, -5, 2), "MIL": (43.045, -87.917, -6, 190), "MIN": (44.979, -93.276, -6, 260),
    "NO": (29.949, -90.082, -6, 2), "NY": (40.751, -73.993, -5, 10), "OKC": (35.463, -97.515, -6, 370),
    "ORL": (28.539, -81.384, -5, 30), "PHI": (39.901, -75.172, -5, 10), "PHX": (33.446, -112.071, -7, 330),
    "POR": (45.532, -122.667, -8, 15), "SAC": (38.580, -121.500, -8, 10), "SA": (29.427, -98.437, -6, 200),
    "TOR": (43.643, -79.379, -5, 80), "UTAH": (40.768, -111.901, -7, 1288), "WSH": (38.898, -77.021, -5, 10),
}


def km(a, b):
    (la1, lo1, *_), (la2, lo2, *_) = ARENA[a], ARENA[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


# ---------- 1. win recipe: where the margin came from ----------

def _game_rows(conn, season):
    q = """select g.game_id, g.date, g.home, g.away, g.home_pts, g.away_pts, t.team, t.is_home,
                  t.fgm, t.fga, t.fg3m, t.ftm, t.tov, t.oreb, t.pts_off_to, t.fastbreak, t.paint,
                  o.fgm ofgm, o.fga ofga, o.fg3m ofg3m, o.ftm oftm, o.tov otov, o.oreb ooreb, o.pts_off_to opts_off_to,
                  o.fastbreak ofastbreak, o.paint opaint
           from games g join team_box t on t.game_id=g.game_id join team_box o on o.game_id=g.game_id and o.team=t.opp
           where g.season=? and g.home_pts is not null order by g.date"""
    return [dict(r) for r in conn.execute(q, (season,))]


def margin_parts(r) -> dict:
    """Exact split of one team's margin: shooting + turnovers + offensive rebounds + free throws + other volume.
    FG points difference = (pps_a - pps_b) * avg FGA + (FGA_a - FGA_b) * avg pps (an identity), the volume part is then
    attributed to turnovers and offensive rebounds, the remainder to 'other' (pace, FT trips, team rebounds)."""
    fp_a, fp_b = 2 * r["fgm"] + r["fg3m"], 2 * r["ofgm"] + r["ofg3m"]
    pa, pb = fp_a / max(1, r["fga"]), fp_b / max(1, r["ofga"])
    avg_fga, avg_pps = (r["fga"] + r["ofga"]) / 2, (pa + pb) / 2
    shooting = (pa - pb) * avg_fga
    volume = (r["fga"] - r["ofga"]) * avg_pps
    tov = (r["otov"] - r["tov"]) * avg_pps
    orb = (r["oreb"] - r["ooreb"]) * avg_pps
    ft = r["ftm"] - r["oftm"]
    return {"shooting": shooting, "turnovers": tov, "off. rebounds": orb, "free throws": ft, "other": volume - tov - orb}


def win_recipe(conn, season) -> dict:
    """{team: {avg parts in wins, in losses, overall}, '_league': which factor separates winners from losers}."""
    by = defaultdict(lambda: {"W": [], "L": []})
    allw = []
    for r in _game_rows(conn, season):
        parts = margin_parts(r)
        won = (r["home_pts"] > r["away_pts"]) == bool(r["is_home"])
        by[r["team"]]["W" if won else "L"].append(parts)
        if won:
            allw.append(parts)
    avg = lambda rows: {k: round(sum(p[k] for p in rows) / len(rows), 2) for k in rows[0]} if rows else {}
    out = {t: {"wins": avg(v["W"]), "losses": avg(v["L"]), "all": avg(v["W"] + v["L"]), "n": len(v["W"]) + len(v["L"])}
           for t, v in by.items()}
    out["_league"] = {"winner_edge": avg(allw)}       # average edge of the winning side, per factor
    return out


def game_recipe(conn, game_id) -> dict:
    rows = [r for r in _game_rows_one(conn, game_id)]
    return {r["team"]: {k: round(v, 1) for k, v in margin_parts(r).items()} for r in rows}


def _game_rows_one(conn, game_id):
    q = """select g.home_pts, g.away_pts, t.team, t.is_home, t.fgm, t.fga, t.fg3m, t.ftm, t.tov, t.oreb,
                  o.fgm ofgm, o.fga ofga, o.fg3m ofg3m, o.ftm oftm, o.tov otov, o.oreb ooreb
           from games g join team_box t on t.game_id=g.game_id join team_box o on o.game_id=g.game_id and o.team=t.opp
           where g.game_id=?"""
    return [dict(r) for r in conn.execute(q, (game_id,))]


# ---------- 3. shot quality vs shot-making ----------

def shot_quality(S: list[dict]) -> dict:
    """Expected points per shot from each shot's (type, zone) at league rates, vs actual. Offense and defense.
    shot_making > 0 = making more than the shots deserve (skill or luck); defense luck > 0 = opponents hot against them."""
    cell = defaultdict(lambda: [0, 0])
    for s in S:
        c = cell[(s["kind"], s["zone"])]
        c[0] += s["pts"]
        c[1] += 1
    xp = {k: v[0] / v[1] for k, v in cell.items()}
    agg = defaultdict(lambda: {"o": [0.0, 0.0, 0], "d": [0.0, 0.0, 0]})
    pl = defaultdict(lambda: [0.0, 0.0, 0, None])
    for s in S:
        e = xp[(s["kind"], s["zone"])]
        for side, team in (("o", s["team"]), ("d", s["opp"])):
            a = agg[team][side]
            a[0] += s["pts"]; a[1] += e; a[2] += 1
        if s["player_id"]:
            p = pl[s["player_id"]]
            p[0] += s["pts"]; p[1] += e; p[2] += 1; p[3] = s["team"]
    out = {}
    for t, v in agg.items():
        o, d = v["o"], v["d"]
        out[t] = {"x_pps": o[1] / max(1, o[2]), "pps": o[0] / max(1, o[2]), "making": (o[0] - o[1]) / max(1, o[2]),
                  "d_x_pps": d[1] / max(1, d[2]), "d_pps": d[0] / max(1, d[2]), "d_luck": (d[0] - d[1]) / max(1, d[2]), "shots": o[2]}
    out["_players"] = {pid: {"team": v[3], "shots": v[2], "making": (v[0] - v[1]) / v[2], "x_pps": v[1] / v[2]}
                       for pid, v in pl.items() if v[2] >= 80}
    return out


# ---------- 4. rest and travel ----------

def schedule_context(conn, season) -> dict:
    """{(game_id, team): {rest, b2b, three_in_four, road_trip, km, tz, altitude}} for every game, played or scheduled."""
    games = [dict(r) for r in conn.execute("""select game_id, date, home, away, status from games where season=? and season_type in (2,3,5)
                                             and status!='canceled' order by date, tip_utc""", (season,))]
    last = {}
    hist = defaultdict(list)
    out = {}
    for g in games:
        d = date.fromisoformat(g["date"])
        for team, home in ((g["home"], True), (g["away"], False)):
            prev = last.get(team)
            rest = (d - prev[0]).days - 1 if prev else None
            loc_prev = prev[1] if prev else team
            dates = [x for x in hist[team] if 0 < (d - x).days <= 3]
            trip = 0
            if not home:
                trip = 1
                for h in reversed(hist[team + ":home"]):
                    if h:
                        break
                    trip += 1
            out[(g["game_id"], team)] = {
                "rest": rest, "b2b": rest == 0, "three_in_four": len(dates) >= 2, "road_trip": trip,
                "km": round(km(loc_prev, g["home"])) if prev else 0,
                "tz": ARENA[g["home"]][2] - ARENA[loc_prev][2] if prev else 0,
                "altitude": ARENA[g["home"]][3] >= 1200 and team not in ("DEN", "UTAH")}
            last[team] = (d, g["home"])
            hist[team].append(d)
            hist[team + ":home"].append(home)
    return out


def rest_splits(conn, season, ctxmap) -> dict:
    """Per team: record and margin by rest situation (b2b, 1 day, 2+ days) and on long road trips."""
    out = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    for g in conn.execute("select * from games where season=? and home_pts is not null", (season,)):
        for team, sign in ((g["home"], 1), (g["away"], -1)):
            c = ctxmap.get((g["game_id"], team))
            if not c or c["rest"] is None:
                continue
            m = sign * (g["home_pts"] - g["away_pts"])
            key = "back-to-back" if c["b2b"] else "1 day rest" if c["rest"] == 1 else "2+ days rest"
            for k in (key,) + (("road trip game 3+",) if c["road_trip"] >= 3 else ()):
                x = out[team][k]
                x[0] += m > 0; x[1] += m < 0; x[2] += m
    return {t: {k: {"record": f"{v[0]}-{v[1]}", "avg_margin": round(v[2] / max(1, v[0] + v[1]), 1)} for k, v in d.items()}
            for t, d in out.items()}


# ---------- 5/6. clutch and quarters ----------

def clutch_and_quarters(conn, season) -> dict:
    """Clutch = last 5 min of Q4 / OT with the margin within 5. Quarter margins and halftime shot-mix shifts."""
    q = """select p.game_id, p.period, p.clock_s, p.type, p.text, p.team, p.home_score, p.away_score, g.home, g.away
           from plays p join games g on g.game_id=p.game_id where g.season=? order by p.game_id, p.seq"""
    clutch = defaultdict(lambda: {"pf": 0, "pa": 0, "poss": 0, "ftm": 0, "fta": 0, "games": set(), "w": 0, "l": 0})
    quarters = defaultdict(lambda: defaultdict(list))
    cur, prev_h, prev_a, qstart = None, 0, 0, {}
    rows = conn.execute(q, (season,)).fetchall()
    final = {}
    for r in rows:
        gid = r["game_id"]
        if gid != cur:
            cur, prev_h, prev_a, qstart = gid, 0, 0, {1: (0, 0)}
        h, a = r["home_score"] or 0, r["away_score"] or 0
        per = r["period"]
        if per not in qstart:
            qstart[per] = (prev_h, prev_a)
        if r["type"] == "End Period" and per <= 4:
            sh, sa = qstart[per]
            quarters[r["home"]][per].append((h - sh) - (a - sa))
            quarters[r["away"]][per].append((a - sa) - (h - sh))
        in_clutch = (per == 4 and r["clock_s"] <= 300 or per > 4) and abs(prev_h - prev_a) <= 5
        if in_clutch:
            for team, home in ((r["home"], True), (r["away"], False)):
                c = clutch[team]
                c["games"].add(gid)
                c["pf"] += (h - prev_h) if home else (a - prev_a)
                c["pa"] += (a - prev_a) if home else (h - prev_h)
            t = r["type"] or ""
            if r["team"] and ("Turnover" in t or (schemes.kind_of(t, False) and "Free Throw" not in t and "makes" in (r["text"] or ""))
                              or t == "Defensive Rebound"):
                clutch[r["team"] if t != "Defensive Rebound" else (r["away"] if r["team"] == r["home"] else r["home"])]["poss"] += 1
            if "Free Throw" in t and r["team"]:
                clutch[r["team"]]["fta"] += 1
                clutch[r["team"]]["ftm"] += " makes " in (r["text"] or "")
        prev_h, prev_a = h, a
        final[gid] = (r["home"], r["away"], h, a)
    for gid, (hm, aw, h, a) in final.items():
        for team, won in ((hm, h > a), (aw, a > h)):
            if gid in clutch[team]["games"]:
                clutch[team]["w" if won else "l"] += 1
    out = {}
    for t in set(clutch) | set(quarters):
        c = clutch.get(t)
        qm = quarters.get(t, {})
        out[t] = {"clutch": None if not c or not c["games"] else {
                      "games": len(c["games"]), "record": f"{c['w']}-{c['l']}", "net": c["pf"] - c["pa"],
                      "net_per_game": round((c["pf"] - c["pa"]) / len(c["games"]), 1),
                      "ft_pct": round(c["ftm"] / c["fta"], 3) if c["fta"] else None, "fta": c["fta"]},
                  "quarters": {f"Q{k}": round(sum(v) / len(v), 1) for k, v in sorted(qm.items()) if v}}
    return out


def halftime_shift(S: list[dict]) -> dict:
    """How much a team changes its shot mix from the first half to the second (0 = identical, 1 = completely different),
    averaged over games, plus the shot type it leans into most after halftime."""
    g = defaultdict(lambda: {1: defaultdict(int), 2: defaultdict(int)})
    for s in S:
        if s["period"] <= 4:
            g[(s["game_id"], s["team"])][1 if s["period"] <= 2 else 2][s["kind"]] += 1
    per = defaultdict(list)
    lean = defaultdict(lambda: defaultdict(float))
    for (gid, team), h in g.items():
        n1, n2 = sum(h[1].values()), sum(h[2].values())
        if n1 < 15 or n2 < 15:
            continue
        diff = {k: h[2][k] / n2 - h[1][k] / n1 for k in schemes.KINDS}
        per[team].append(0.5 * sum(abs(v) for v in diff.values()))
        for k, v in diff.items():
            lean[team][k] += v
    return {t: {"shift": round(sum(v) / len(v), 3), "games": len(v),
                "leans_into": max(lean[t], key=lean[t].get), "leans_into_pp": round(100 * max(lean[t].values()) / len(v), 1)}
            for t, v in per.items()}


# ---------- 8. box extras ----------

def box_extras(rows) -> dict:
    agg = defaultdict(lambda: defaultdict(float))
    for r in rows:
        a = agg[r["team"]]
        a["n"] += 1
        for k in ("pts_off_to", "fastbreak", "paint"):
            a[k] += r[k] or 0
            a["opp_" + k] += r["o" + k] or 0
    return {t: {k: round(v / a["n"], 1) for k, v in a.items() if k != "n"} for t, a in agg.items()}


# ---------- 9. referees ----------

def referees(conn, season) -> dict:
    """Per official: games, fouls and free-throw attempts per game (both teams) vs league."""
    q = """select o.name, g.game_id, sum(t.pf) pf, sum(t.fta) fta from game_officials o join games g on g.game_id=o.game_id
           join team_box t on t.game_id=g.game_id where g.season=? group by o.name, g.game_id"""
    per = defaultdict(list)
    for r in conn.execute(q, (season,)):
        per[r["name"]].append((r["pf"], r["fta"]))
    allg = [x for v in per.values() for x in v]
    lg_pf = sum(x[0] for x in allg) / max(1, len(allg))
    lg_fta = sum(x[1] for x in allg) / max(1, len(allg))
    out = {n: {"games": len(v), "pf": round(sum(x[0] for x in v) / len(v), 1), "fta": round(sum(x[1] for x in v) / len(v), 1),
               "fta_vs_lg": round(sum(x[1] for x in v) / len(v) - lg_fta, 1)} for n, v in per.items()}
    out["_league"] = {"pf": round(lg_pf, 1), "fta": round(lg_fta, 1)}
    return out


def crew(conn, game_id, refs: dict) -> dict | None:
    names = [r[0] for r in conn.execute("select name from game_officials where game_id=?", (game_id,))]
    known = [refs[n] for n in names if n in refs and refs[n]["games"] >= 3]
    if not names:
        return None
    return {"names": names, "fta_vs_lg": round(sum(k["fta_vs_lg"] for k in known) / len(known), 1) if known else None}


# ---------- 10. betting line movement ----------

def american_prob(ml):
    if ml is None:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def line_move(conn, game_id) -> dict | None:
    r = conn.execute("select * from odds_snap where game_id=? order by ts desc limit 1", (game_id,)).fetchone()
    if not r or r["home_ml"] is None or r["home_ml_open"] is None:
        return None
    def wp(h, a):
        ph, pa = american_prob(h), american_prob(a)
        return ph / (ph + pa) if ph and pa else None
    now, opn = wp(r["home_ml"], r["away_ml"]), wp(r["home_ml_open"], r["away_ml_open"])
    if now is None or opn is None:
        return None
    return {"home_wp_open": round(opn, 3), "home_wp_now": round(now, 3), "move": round(now - opn, 3),
            "spread_open": r["spread_open"], "spread": r["spread"], "total": r["total"]}
