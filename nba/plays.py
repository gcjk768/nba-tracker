"""Each team's most-used plays and how often they succeed, from ESPN play-by-play.

A "play" here is how the shot was created plus where it ended up (pull-up 3, drive to the rim, spot-up corner 3...).
Success % = the share of those attempts that went in. Points per shot and the league comparison come with it.
Named sets (horns, Spain pick-and-roll) are not logged per possession in public data, so they get no success rate.
"""
from collections import defaultdict

PLAY_ORDER = ("drive to the rim", "drive: floater / short jumper", "pull-up 3", "pull-up mid-range", "spot-up corner 3",
              "spot-up above-break 3", "spot-up mid-range", "cut to the rim", "lob / alley-oop", "post-up / turnaround",
              "putback", "dump-off at the rim")
EXPLAIN = {   # which play-explainer label to open for each play
    "drive to the rim": "drive", "drive: floater / short jumper": "drive", "pull-up 3": "pull-up", "pull-up mid-range": "pull-up",
    "spot-up corner 3": "spot-up jumper", "spot-up above-break 3": "spot-up jumper", "spot-up mid-range": "spot-up jumper",
    "cut to the rim": "cut", "lob / alley-oop": "alley-oop", "post-up / turnaround": "post-up / turnaround", "putback": "putback",
    "dump-off at the rim": "rim finish",
}
MIN_ATT = 15        # fewer attempts than this: shown, flagged as small sample, not ranked


def play_of(s) -> str:
    k, z = s["kind"], s["zone"]
    three = z in ("corner 3", "above-break 3")
    if k == "drive":
        return "drive to the rim" if z == "rim" else "drive: floater / short jumper"
    if k == "pull-up":
        return "pull-up 3" if three else "pull-up mid-range"
    if k == "spot-up jumper":
        return "spot-up corner 3" if z == "corner 3" else "spot-up above-break 3" if three else "spot-up mid-range"
    if k == "cut":
        return "cut to the rim"
    if k == "alley-oop":
        return "lob / alley-oop"
    if k == "post-up / turnaround":
        return "post-up / turnaround"
    if k == "putback":
        return "putback"
    return "dump-off at the rim"


def _agg(shots):
    a = defaultdict(lambda: {"att": 0, "made": 0, "pts": 0, "ast": 0})
    for s in shots:
        x = a[play_of(s)]
        x["att"] += 1
        x["made"] += s["made"]
        x["pts"] += s["pts"]
        x["ast"] += s["made"] and s["assisted"]
    return a


def league(S) -> dict:
    a = _agg(S)
    return {p: {"success": v["made"] / v["att"], "pps": v["pts"] / v["att"]} for p, v in a.items() if v["att"]}


def team_plays(S, team: str, lg: dict, side="offense", top=None) -> list[dict]:
    """Rows sorted by how often the play is used. side='defense' = plays opponents ran against this team."""
    mine = [s for s in S if (s["team"] if side == "offense" else s["opp"]) == team]
    n = len(mine) or 1
    rows = []
    for p, v in _agg(mine).items():
        base = lg.get(p, {})
        rows.append({"play": p, "explain": EXPLAIN[p], "share": round(v["att"] / n, 3), "att": v["att"],
                     "success": round(v["made"] / v["att"], 3), "pps": round(v["pts"] / v["att"], 3),
                     "lg_success": round(base.get("success", 0), 3), "lg_pps": round(base.get("pps", 0), 3),
                     "vs_lg": round(v["pts"] / v["att"] - base.get("pps", 0), 3),
                     "assisted": round(v["ast"] / v["made"], 3) if v["made"] else None,
                     "small_sample": v["att"] < MIN_ATT})
    rows.sort(key=lambda r: -r["share"])
    return rows[:top] if top else rows


def ranks(S, lg) -> dict:
    """{play: {team: rank by pts/shot, 1 = best}} among teams with enough attempts (offense) and (defense, 1 = stingiest)."""
    off, de = defaultdict(dict), defaultdict(dict)
    teams = {s["team"] for s in S}
    for t in teams:
        for r in team_plays(S, t, lg, "offense"):
            if not r["small_sample"]:
                off[r["play"]][t] = r["pps"]
        for r in team_plays(S, t, lg, "defense"):
            if not r["small_sample"]:
                de[r["play"]][t] = r["pps"]
    out = {"offense": {}, "defense": {}}
    for p, d in off.items():
        out["offense"][p] = {t: i + 1 for i, t in enumerate(sorted(d, key=lambda x: -d[x]))}
    for p, d in de.items():
        out["defense"][p] = {t: i + 1 for i, t in enumerate(sorted(d, key=lambda x: d[x]))}
    return out


def ato_plays(possessions: list[dict], team: str, lg: dict, top=5) -> dict:
    """Most-used after-timeout plays for a team with success %, plus how the ATO possessions end overall."""
    mine = [p for p in possessions if p["team"] == team]
    shots = [dict(p["shot"], pts=(3 if p["shot"]["zone"] in ("corner 3", "above-break 3") else 2) * p["shot"]["made"])
             for p in mine if p["shot"]]
    n = len(mine) or 1
    a = _agg(shots)
    rows = sorted(({"play": k, "explain": EXPLAIN[k], "share": round(v["att"] / n, 3), "att": v["att"],
                    "success": round(v["made"] / v["att"], 3), "lg_success": round(lg.get(k, {}).get("success", 0), 3),
                    "small_sample": v["att"] < 6} for k, v in a.items()), key=lambda r: -r["share"])[:top]
    return {"n": len(mine), "plays": rows,
            "ends": {"score": round(sum(p["points"] > 0 for p in mine) / n, 3), "turnover": round(sum(p["turnover"] for p in mine) / n, 3),
                     "free throws": round(sum(p["fts"] for p in mine) / n, 3)}}
