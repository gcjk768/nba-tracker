"""Five-man units, on/off, closing lineups, bench units and with/without records, rebuilt from ESPN substitutions.

ESPN logs every change (including between quarters) as "A enters the game for B" with the team on the play.
Starters come from the box score. Stints where a team does not have exactly five players (a missed sub) are dropped
and counted, so coverage is visible. Possessions are counted from possession-ending events.
"""
import re
from collections import defaultdict

_SUB = re.compile(r"^(.+?) enters the game for (.+?)$")


def _gsec(period, clock):
    return (period - 1) * 720 + (720 - clock) if period <= 4 else 2880 + (period - 5) * 300 + (300 - clock)


def _ends_possession(t, text):
    t = t or ""
    if "Turnover" in t:
        return True
    if "Free Throw" in t:
        return bool(re.search(r"(\d) of \1", t)) and " makes " in (text or "")   # made last free throw of the trip
    if "Rebound" in t:
        return t == "Defensive Rebound"
    return " makes " in (text or "") and "Free Throw" not in t                    # made field goal


def stints(conn, season) -> tuple[list[dict], dict]:
    games = [dict(r) for r in conn.execute("""select game_id, home, away from games where season=? and source is not null""", (season,))]
    out, cov = [], {"games": 0, "seconds_kept": 0, "seconds_total": 0}
    for g in games:
        gid = g["game_id"]
        box = conn.execute("select player_id, name, team, starter, dnp from player_box where game_id=?", (gid,)).fetchall()
        if not box:
            continue
        name_id = {(r["team"], r["name"]): r["player_id"] for r in box}
        on = {g["home"]: {r["player_id"] for r in box if r["team"] == g["home"] and r["starter"]},
              g["away"]: {r["player_id"] for r in box if r["team"] == g["away"] and r["starter"]}}
        plays = conn.execute("""select period, clock_s, type, text, team, player_id, home_score, away_score
                                from plays where game_id=? order by seq""", (gid,)).fetchall()
        if not plays:
            continue
        cov["games"] += 1
        cur = {"t0": 0, "h0": 0, "a0": 0, "poss": {g["home"]: 0, g["away"]: 0}, "clutch": 0}
        last_h = last_a = 0

        def close(t1, h1, a1):
            dur = t1 - cur["t0"]
            if dur <= 0:            # out-of-order plays (replays, challenges): nothing to count
                return
            cov["seconds_total"] += dur
            ok = len(on[g["home"]]) == 5 and len(on[g["away"]]) == 5
            if ok:
                cov["seconds_kept"] += dur
                for team, opp, pf, pa in ((g["home"], g["away"], h1 - cur["h0"], a1 - cur["a0"]),
                                          (g["away"], g["home"], a1 - cur["a0"], h1 - cur["h0"])):
                    out.append({"game_id": gid, "team": team, "unit": frozenset(on[team]), "sec": dur, "pf": pf, "pa": pa,
                                "poss": cur["poss"][team], "opp_poss": cur["poss"][opp], "clutch_sec": cur["clutch"]})
        for p in plays:
            t = p["type"] or ""
            gs = _gsec(p["period"], p["clock_s"])
            h, a = p["home_score"] or last_h, p["away_score"] or last_a
            if t == "Substitution" and p["team"] in on:
                m = _SUB.match(p["text"] or "")
                if m:
                    close(gs, last_h, last_a)
                    cur = {"t0": gs, "h0": last_h, "a0": last_a, "poss": {g["home"]: 0, g["away"]: 0}, "clutch": 0}
                    pin = p["player_id"] or name_id.get((p["team"], m.group(1).strip()))
                    pout = name_id.get((p["team"], m.group(2).strip()))
                    on[p["team"]].discard(pout)
                    if pin:
                        on[p["team"]].add(pin)
                continue
            if p["team"] in on and _ends_possession(t, p["text"]):
                owner = p["team"] if t != "Defensive Rebound" else (g["away"] if p["team"] == g["home"] else g["home"])
                if t == "Defensive Rebound":
                    owner = g["away"] if p["team"] == g["home"] else g["home"]       # the shooting team's possession ended
                cur["poss"][owner] += 1
            if (p["period"] == 4 and p["clock_s"] <= 300 or p["period"] > 4) and abs(last_h - last_a) <= 5:
                cur["clutch"] = 1
            last_h, last_a = h, a
        if plays:
            pl = plays[-1]
            close(_gsec(pl["period"], pl["clock_s"]), last_h, last_a)
    return out, cov


def per100(pf, pa, poss):
    return 100 * (pf - pa) / poss if poss else None


def team_lineups(conn, season, names: dict | None = None, starters: dict | None = None) -> dict:
    """{team: {units, on_off, closing, bench, with_without}} plus '_coverage'."""
    names = names or {r[0]: r[1] for r in conn.execute("select player_id, max(name) from player_box group by player_id")}
    S, cov = stints(conn, season)
    by_team = defaultdict(list)
    for s in S:
        by_team[s["team"]].append(s)
    # who usually starts (for bench units): starters in at least half their games
    st = defaultdict(set)
    for r in conn.execute("""select p.team, p.player_id, sum(p.starter) gs, count(*) gp from player_box p join games g on g.game_id=p.game_id
                             where g.season=? and p.dnp=0 group by p.team, p.player_id""", (season,)):
        if r["gs"] >= r["gp"] / 2 and r["gs"] >= 3:
            st[r["team"]].add(r["player_id"])
    out = {}
    for team, rows in by_team.items():
        units = defaultdict(lambda: [0, 0, 0, 0, 0])      # sec, pf, pa, poss, clutch sec
        onp, offp = defaultdict(lambda: [0, 0, 0]), defaultdict(lambda: [0, 0, 0])
        bench = [0, 0, 0, 0]
        players = {p for r in rows for p in r["unit"]}
        for r in rows:
            u = units[r["unit"]]
            poss = (r["poss"] + r["opp_poss"]) / 2
            u[0] += r["sec"]; u[1] += r["pf"]; u[2] += r["pa"]; u[3] += poss; u[4] += r["sec"] if r["clutch_sec"] else 0
            for p in players:
                x = onp[p] if p in r["unit"] else offp[p]
                x[0] += r["pf"]; x[1] += r["pa"]; x[2] += poss
            if len(r["unit"] & st[team]) <= 2:
                bench[0] += r["sec"]; bench[1] += r["pf"]; bench[2] += r["pa"]; bench[3] += poss
        total_sec = sum(u[0] for u in units.values()) or 1
        top = sorted(units.items(), key=lambda kv: -kv[1][0])[:6]
        closing = max(units.items(), key=lambda kv: kv[1][4]) if any(u[4] for u in units.values()) else None
        fmt = lambda u: [names.get(p, p) for p in sorted(u, key=lambda p: names.get(p, p))]
        out[team] = {
            "units": [{"players": fmt(u), "min": round(v[0] / 60, 1), "net": None if v[3] < 1 else round(per100(v[1], v[2], v[3]), 1),
                       "poss": round(v[3]), "noise": v[3] < 100} for u, v in top],
            "closing": None if not closing else {"players": fmt(closing[0]), "clutch_min": round(closing[1][4] / 60, 1)},
            "on_off": sorted(({"player_id": p, "name": names.get(p, p), "on": round(per100(*onp[p]), 1) if onp[p][2] >= 50 else None,
                               "off": round(per100(*offp[p]), 1) if offp[p][2] >= 50 else None, "on_poss": round(onp[p][2])}
                              for p in players if onp[p][2] >= 50), key=lambda x: -(x["on_poss"])),
            "bench": {"min_share": round(bench[0] / total_sec, 3), "net": round(per100(bench[1], bench[2], bench[3]), 1) if bench[3] >= 50 else None,
                      "poss": round(bench[3])},
            "with_without": _with_without(conn, season, team, names),
        }
        for x in out[team]["on_off"]:
            x["diff"] = None if x["on"] is None or x["off"] is None else round(x["on"] - x["off"], 1)
    out["_coverage"] = {"games": cov["games"], "share_kept": round(cov["seconds_kept"] / max(1, cov["seconds_total"]), 3)}
    return out


def _with_without(conn, season, team, names):
    """Team record and average margin with and without each of its top three players (by minutes)."""
    top = conn.execute("""select p.player_id from player_box p join games g on g.game_id=p.game_id
                          where g.season=? and p.team=? and p.dnp=0 group by p.player_id order by sum(p.minutes) desc limit 3""",
                       (season, team)).fetchall()
    games = [dict(r) for r in conn.execute("""select game_id, home, home_pts, away_pts from games where season=? and home_pts is not null
                                             and (home=? or away=?)""", (season, team, team))]
    out = []
    for (pid,) in top:
        played = {r[0] for r in conn.execute("select game_id from player_box where player_id=? and dnp=0", (pid,))}
        w = defaultdict(lambda: [0, 0, 0.0])
        for g in games:
            m = (g["home_pts"] - g["away_pts"]) * (1 if g["home"] == team else -1)
            k = "with" if g["game_id"] in played else "without"
            w[k][0] += m > 0; w[k][1] += m < 0; w[k][2] += m
        out.append({"player": names.get(pid, pid), **{k: {"record": f"{v[0]}-{v[1]}", "avg_margin": round(v[2] / max(1, v[0] + v[1]), 1)}
                                                       for k, v in w.items()}})
    return out
