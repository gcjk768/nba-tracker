"""After-timeout (ATO) possessions, measured from ESPN play-by-play.

ATO possession = the first real possession after a timeout (shot attempt, free throws or turnover), for whichever
team has the ball, followed until the other team gets it (their shot, turnover, defensive rebound) or the period ends.
Points = that team's score change over the possession. Own-timeout ATOs (the team that called it has the ball) are
what coaches draw up; both are kept and labelled.
"""
from collections import defaultdict

from . import schemes

SKIP = ("Substitution", "Challenge", "Timeout", "Jumpball", "End Period", "Instant Replay", "Delay", "Technical")
MIN_N = 12          # fewer ATO possessions than this: rank shown as "sample too small"


def _is_shot(t):
    return t and "Free Throw" not in t and any(k in t for k in ("Shot", "Layup", "Dunk", "Jump", "Hook", "Tip", "Finger"))


def possessions(conn, season, before=None) -> list[dict]:
    q = """select p.game_id, p.seq, p.period, p.clock_s, p.type, p.text, p.team, p.player_id, p.x, p.y, p.shooting,
                  p.home_score, p.away_score, g.home, g.away, g.date
           from plays p join games g on g.game_id=p.game_id
           where g.season=? %s order by p.game_id, p.seq""" % ("and g.date < ?" if before else "")
    rows = conn.execute(q, [season] + ([before] if before else [])).fetchall()
    games = defaultdict(list)
    for r in rows:
        games[r["game_id"]].append(r)
    out = []
    for gid, pl in games.items():
        home, away = pl[0]["home"], pl[0]["away"]
        for i, p in enumerate(pl):
            if "Timeout" not in (p["type"] or "") or not p["team"]:
                continue
            caller = p["team"]
            start_h, start_a = p["home_score"] or 0, p["away_score"] or 0
            j, owner, first = i + 1, None, None
            while j < len(pl) and pl[j]["period"] == p["period"]:
                q2 = pl[j]
                t = q2["type"] or ""
                if any(s in t for s in SKIP) or not q2["team"]:
                    j += 1
                    continue
                if _is_shot(t) or "Free Throw" in t or "Turnover" in t:
                    owner, first = q2["team"], q2
                    break
                j += 1
            if not owner:
                continue
            opp = away if owner == home else home
            k, end = j, j
            while k < len(pl) and pl[k]["period"] == p["period"]:
                t, tm = pl[k]["type"] or "", pl[k]["team"]
                if k > j and tm == opp and (_is_shot(t) or "Turnover" in t or t == "Defensive Rebound" or "Free Throw" in t):
                    break
                if k > j and "Timeout" in t:
                    break
                end = k
                k += 1
            last = pl[end]
            pts = ((last["home_score"] or 0) - start_h) if owner == home else ((last["away_score"] or 0) - start_a)
            ftype = first["type"] or ""
            shot = None
            if _is_shot(ftype):
                assisted = "assists)" in (first["text"] or "")
                shot = {"kind": schemes.kind_of(ftype, assisted), "zone": schemes.zone_of(first["text"], first["x"], first["y"], "three point" in (first["text"] or "")),
                        "made": " makes " in (first["text"] or ""), "assisted": assisted, "player_id": first["player_id"]}
            out.append({"game_id": gid, "date": pl[0]["date"], "team": owner, "own": owner == caller, "points": max(0, pts),
                        "late": p["period"] > 4 or (p["period"] == 4 and p["clock_s"] <= 120),
                        "turnover": "Turnover" in ftype, "fts": "Free Throw" in ftype, "shot": shot})
    return out


def profiles(poss: list[dict], names: dict | None = None) -> dict:
    """{team: {n, ppp, score_rate, tov_rate, own_n, late_n, late_ppp, kind mix, top shooters, rank}} + '_league'."""
    names = names or {}
    by = defaultdict(list)
    for p in poss:
        by[p["team"]].append(p)
    out = {}
    for t, ps in by.items():
        n = len(ps)
        shots = [p["shot"] for p in ps if p["shot"]]
        late = [p for p in ps if p["late"]]
        shooters = defaultdict(int)
        for s in shots:
            if s["player_id"]:
                shooters[s["player_id"]] += 1
        out[t] = {"n": n, "own_n": sum(p["own"] for p in ps), "ppp": sum(p["points"] for p in ps) / n,
                  "score_rate": sum(p["points"] > 0 for p in ps) / n, "tov_rate": sum(p["turnover"] for p in ps) / n,
                  "ft_rate": sum(p["fts"] for p in ps) / n,
                  "late_n": len(late), "late_ppp": (sum(p["points"] for p in late) / len(late)) if late else None,
                  "kind": {k: sum(s["kind"] == k for s in shots) / max(1, len(shots)) for k in schemes.KINDS},
                  "three_rate": sum(s["zone"] in ("corner 3", "above-break 3") for s in shots) / max(1, len(shots)),
                  "assisted": sum(s["assisted"] for s in shots if s["made"]) / max(1, sum(s["made"] for s in shots)),
                  "top_shooters": [{"player_id": pid, "name": names.get(pid, pid), "shots": c}
                                   for pid, c in sorted(shooters.items(), key=lambda x: -x[1])[:3]],
                  "small_sample": n < MIN_N}
    ranked = sorted((t for t in out if not out[t]["small_sample"]), key=lambda t: -out[t]["ppp"])
    for i, t in enumerate(ranked):
        out[t]["rank"] = i + 1
    for t in out:
        out[t].setdefault("rank", None)
    allp = [p for ps in by.values() for p in ps]
    out["_league"] = {"ppp": sum(p["points"] for p in allp) / max(1, len(allp)), "n": len(allp),
                      "score_rate": sum(p["points"] > 0 for p in allp) / max(1, len(allp)), "ranked": len(ranked)}
    return out
