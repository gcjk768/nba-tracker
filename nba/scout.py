"""Scouting: each team's measured weaknesses and the plays / coverages that exploit them.

Every weakness is a league-relative number (value, league average, rank 1-30) from this season's games; the exploit
is standard basketball counter-play (labels from playbook_kb so the dashboard can show the explainer video).
Nothing here is reputation-based. Small samples are flagged, not hidden.
"""
from collections import defaultdict

RANK_CUT = 7          # worst 7 of 30 counts as a weakness


def _team_factors(conn, season) -> dict:
    """Offense and defense four factors + a few rates per team, from team_box (own row + opponent row)."""
    q = """select t.team,
                  sum(t.fgm) fgm, sum(t.fga) fga, sum(t.fg3m) fg3m, sum(t.fg3a) fg3a, sum(t.fta) fta, sum(t.tov) tov,
                  sum(t.poss) poss, sum(t.oreb) oreb, sum(t.dreb) dreb, sum(t.pf) pf,
                  sum(o.fgm) ofgm, sum(o.fga) ofga, sum(o.fg3m) ofg3m, sum(o.fg3a) ofg3a, sum(o.fta) ofta, sum(o.tov) otov,
                  sum(o.poss) oposs, sum(o.oreb) ooreb, sum(o.dreb) odreb, count(*) g
           from team_box t join team_box o on o.game_id = t.game_id and o.team = t.opp
           join games g on g.game_id = t.game_id where g.season = ? group by t.team"""
    out = {}
    for r in conn.execute(q, (season,)):
        out[r["team"]] = {
            "tov": r["tov"] / max(1, r["poss"]), "three_pct": r["fg3m"] / max(1, r["fg3a"]),
            "orb": r["oreb"] / max(1, r["oreb"] + r["odreb"]), "ftr": r["fta"] / max(1, r["fga"]),
            "d_tov": r["otov"] / max(1, r["oposs"]), "d_orb": r["ooreb"] / max(1, r["ooreb"] + r["dreb"]),
            "d_ftr": r["ofta"] / max(1, r["ofga"]), "d_three_pct": r["ofg3m"] / max(1, r["ofg3a"]),
            "d_efg": (r["ofgm"] + 0.5 * r["ofg3m"]) / max(1, r["ofga"]), "games": r["g"],
        }
    return out


def _rank(vals: dict, t: str, high_is_bad: bool) -> int:
    """1 = worst in the league for this metric."""
    order = sorted(vals, key=lambda x: -vals[x] if high_is_bad else vals[x])
    return order.index(t) + 1


def _w(area, side, value, league, rank, fmt, exploit, why, plays):
    return {"area": area, "side": side, "value": value, "league": league, "rank_worst": rank,
            "evidence": f"{fmt(value)} (league {fmt(league)}, {_nth(rank)} worst)", "exploit": exploit, "why": why,
            "plays": plays, "source": "measured", "severity": RANK_CUT + 1 - rank}


def _nth(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


pct = lambda v: f"{100 * v:.1f}%"
pps = lambda v: f"{v:.2f} pts/shot"


def weaknesses(conn, season, P: dict, ato: dict | None = None, vs_styles: dict | None = None,
               luck: dict | None = None) -> dict:
    """{team: [weakness, ...]} sorted most severe first."""
    F = _team_factors(conn, season)
    teams = [t for t in P if not t.startswith("_") and t in F]
    out = defaultdict(list)
    M = {   # metric -> (values by team, high_is_bad, side, area, fmt, exploit, why, plays)
        "d_rim": ({t: P[t]["defense"]["rim_rate"] for t in teams}, True, "defense", "Gives up the rim", pct,
                  "Attack the basket", "Opponents get to the rim against them more than almost anyone.", ["drive", "pick-and-roll", "backdoor cuts"]),
        "d_rim_fg": ({t: P[t]["defense"]["zone_fg"]["rim"] for t in teams}, True, "defense", "Weak rim protection", pct,
                     "Finish inside, throw lobs", "Shots at the rim go in at a high rate against them.", ["lob", "pick-and-roll", "drive"]),
        "d_three": ({t: P[t]["defense"]["three_rate"] for t in teams}, True, "defense", "Concedes three-point attempts", pct,
                    "Spread them out and shoot", "A big share of opponent shots are threes.", ["spot-up jumper", "flare", "5-out"]),
        "d_corner": ({t: P[t]["defense"]["zone"]["corner 3"] for t in teams}, True, "defense", "Leaves the corners open", pct,
                     "Drive and kick to the corner", "Opponents take a lot of corner threes, the best shot in the game.", ["hammer", "drive", "5-out"]),
        "d_mid": ({t: P[t]["defense"]["mid_rate"] for t in teams}, True, "defense", "Concedes the mid-range", pct,
                  "Pull up in the pick-and-roll", "Their bigs sit back and the mid-range is open.", ["pull-up", "pick-and-pop", "drop"]),
        "d_pressure": ({t: F[t]["d_tov"] for t in teams}, False, "defense", "No ball pressure", pct,
                       "Run pick-and-roll freely", "They force very few turnovers, so you can run your offense without risk.", ["pick-and-roll", "spain pick-and-roll"]),
        "d_glass": ({t: F[t]["d_orb"] for t in teams}, True, "defense", "Loses the defensive glass", pct,
                    "Crash the offensive boards", "Opponents grab a lot of their misses back.", ["putback"]),
        "d_fouls": ({t: F[t]["d_ftr"] for t in teams}, True, "defense", "Fouls a lot", lambda v: f"{v:.3f} FTA per FGA",
                    "Attack to draw fouls", "Opponents get to the free-throw line often.", ["drive", "post-up", "isolation"]),
        "d_trans": ({t: P[t]["defense"]["transition"] for t in teams}, True, "defense", "Slow getting back", pct,
                    "Push the pace after misses and turnovers", "Many opponent shots come in transition.", ["pistol", "drive"]),
        "o_tov": ({t: F[t]["tov"] for t in teams}, True, "offense", "Turns it over", pct,
                  "Pressure the ball and trap", "They give the ball away more than most teams.", ["blitz", "full-court press"]),
        "o_three": ({t: F[t]["three_pct"] for t in teams}, False, "offense", "Can't shoot threes", pct,
                    "Pack the paint and go under screens", "Their three-point percentage is near the bottom, so help off shooters.", ["drop", "zone"]),
        "o_rim": ({t: P[t]["offense"]["rim_rate"] for t in teams}, False, "offense", "Rarely gets to the rim", pct,
                  "Stay home on shooters, contest jumpers", "Few of their shots come at the rim, so they live on jumpers.", ["switch", "run shooters off"]),
        "o_assist": ({t: P[t]["offense"]["assisted"] for t in teams}, False, "offense", "Little ball movement", pct,
                     "Load up help on the ball", "Few of their makes are assisted, so early help on drives rarely gets punished.", ["blitz", "zone"]),
        "o_glass": ({t: F[t]["orb"] for t in teams}, False, "offense", "No second chances", pct,
                    "Leak out after defensive rebounds", "They rarely get offensive rebounds, so you can run on misses.", ["pistol"]),
    }
    for key, (vals, high_bad, side, area, fmt, exploit, why, plays) in M.items():
        lg = sum(vals.values()) / max(1, len(vals))
        for t in teams:
            r = _rank(vals, t, high_bad)
            if r <= RANK_CUT:
                out[t].append(_w(area, side, vals[t], lg, r, fmt, exploit, why, plays))
    # one star does everything
    for t in teams:
        share = P[t]["top_shooter"]["share"]
        if share >= 0.27:
            out[t].append({"area": "Relies on one creator", "side": "offense", "value": share, "league": None, "rank_worst": None,
                           "evidence": f"top shooter takes {100 * share:.0f}% of their shots", "exploit": "Load up on the star",
                           "why": "Take the ball out of his hands and make others beat you.", "plays": ["blitz", "box-and-one"],
                           "source": "measured", "severity": 3 + (share - 0.27) * 30})
    # shot types that score unusually well against them
    for t in teams:
        d = P[t]["defense"]
        for k, v in d["kind_pps"].items():
            n = round(d["kind"][k] * d["n"])
            base = _league_kind_pps(P, k)
            if n >= 40 and base and v - base >= 0.12:
                out[t].append({"area": f"Struggles to stop the {k}", "side": "defense", "value": v, "league": base, "rank_worst": None,
                               "evidence": f"{v:.2f} pts/shot allowed on {n} {k} shots (league {base:.2f})", "exploit": f"Go to the {k}",
                               "why": "This shot type scores well above normal against them.", "plays": [k], "source": "measured",
                               "severity": 2 + (v - base) * 20})
    # styles they lose to (3+ games)
    for t, rows in (vs_styles or {}).items():
        for v in rows:
            w, l = (int(x) for x in v["record"].split("-"))
            if v["games"] >= 3 and l > w and v["avg_margin"] <= -5:
                style = v["style"].split(": ", 1)[-1]
                out[t].append({"area": f"Loses to teams that {style.lower().split(' (')[0]}", "side": "matchup", "value": None,
                               "league": None, "rank_worst": None, "evidence": f"{v['record']} in {v['games']} games, {v['avg_margin']:+.1f} avg margin",
                               "exploit": "Play that style against them", "why": "Their results against this style are poor so far.",
                               "plays": [], "source": "measured (small sample)", "severity": 2})
    # ATO
    for t, a in (ato or {}).items():
        if t.startswith("_") or not a or a.get("small_sample") or not a.get("rank"):
            continue
        ranked = (ato or {}).get("_league", {}).get("ranked", 30)
        if a["rank"] > ranked - RANK_CUT:
            out[t].append({"area": "Poor after timeouts", "side": "offense", "value": a["ppp"], "league": (ato.get("_league") or {}).get("ppp"),
                           "rank_worst": ranked + 1 - a["rank"], "evidence": f"{a['ppp']:.2f} pts per ATO possession ({_nth(ranked + 1 - a['rank'])} worst)",
                           "exploit": "Make them execute out of timeouts late", "why": "Their drawn-up plays have not produced; force them into half-court sets late.",
                           "plays": [], "source": "measured", "severity": RANK_CUT + 1 - (ranked + 1 - a["rank"])})
    for t, flags in (luck or {}).items():
        if "opponents shooting cold from 3" in flags:
            out[t].append({"area": "Defense helped by cold opponent shooting", "side": "defense", "value": None, "league": None,
                           "rank_worst": None, "evidence": "opponents are making threes well below the league rate against them",
                           "exploit": "Keep taking open threes", "why": "Opponent three-point luck tends to even out.",
                           "plays": ["spot-up jumper"], "source": "measured (luck)", "severity": 1.5})
    # for good defenses, conceding threes / corners is usually the price of protecting the rim, not a flaw
    d_pps = {t: P[t]["defense"]["pps"] for t in teams}
    good_d = set(sorted(d_pps, key=d_pps.get)[:10])
    for t in good_d:
        for w in out.get(t, []):
            if w["area"] in ("Concedes three-point attempts", "Leaves the corners open"):
                w["tradeoff"] = True
                w["note"] = "Trade-off: their defense is top 10 overall and gives up threes to protect the rim. Only shooting punishes it."
                w["severity"] -= 2
    return {t: sorted(v, key=lambda x: -x["severity"]) for t, v in out.items()}


def _league_kind_pps(P, k):
    pts = n = 0.0
    for t, v in P.items():
        if t.startswith("_"):
            continue
        cnt = v["defense"]["kind"].get(k, 0) * v["defense"]["n"]
        pts += v["defense"]["kind_pps"].get(k, 0) * cnt
        n += cnt
    return pts / n if n else None
