"""Game by game for the focus teams: what they ran on offense and what their defense did, every final this season.

Measured per game from play-by-play (plays.play_of: how each shot was created and where it ended). The per-game
leans reuse schemes.infer, with this game's numbers ranked against every team-game this season, so "packs the paint"
means this opponent's rim share was in the league's bottom quarter of team-games. Named sets and coverages per game
come only from breakdowns tied to that game (film_items.game_id), never guessed.
"""
from bisect import bisect_left
from collections import Counter, defaultdict

from . import playbook_kb as kb, plays, schemes

FOCUS = ("LAL", "SA", "PHI", "OKC")      # the owner 2026-11-10: Lakers, Spurs, 76ers, Thunder


def _pcts(rows: dict) -> tuple[dict, dict]:
    """{key: {metric: v}} -> ({key: {metric: percentile 0-100}}, {metric: league mean})."""
    keys = set().union(*rows.values()) if rows else set()
    srt = {k: sorted(r.get(k, 0) for r in rows.values()) for k in keys}
    n = max(1, len(rows) - 1)
    pct = {g: {k: round(100 * bisect_left(srt[k], r.get(k, 0)) / n) for k in keys} for g, r in rows.items()}
    return pct, {k: sum(v) / max(1, len(v)) for k, v in srt.items()}


def _est(rows, keys, min_att=4):
    used = [r for r in rows if r["play"] in keys]
    att = sum(r["att"] for r in used)
    if att < min_att:
        return None
    return {"success": round(sum(r["success"] * r["att"] for r in used) / att, 3),
            "lg": round(sum(r["lg_success"] * r["att"] for r in used) / att, 3), "att": att}


def _scheme(label, source, def_rows, **kw):
    ctrs, why = kb.COUNTERS.get(label, ([], ""))
    return {"label": label, "source": source, "group": kb.DEF_GROUP.get(label, "other"), "desc": kb.describe(label)["desc"],
            "counters": ctrs[:3], "why": why, "estimate": _est(def_rows, kb.COVERAGE_GIVES.get(label, [])), **kw}


def _named_sets(reported, season, off_rows, top=8):
    """Named offensive sets: reported for this game first, then the team's season playbook (reported in season
    breakdowns), each with this game's estimated success from the plays the set usually ends in."""
    rows = {x["label"]: {"label": x["label"], "source": "this game", "summary": x["summary"], "url": x["url"], "ato": bool(x.get("ato"))}
            for x in reported if x["kind"] != "coverage"}
    for k, x in season.items():
        if x["kind"] != "coverage" and k not in rows:
            rows[k] = {"label": k, "source": "season", "summary": x["summary"], "url": x["url"], "ato": False, "mentions": x["n"]}
    for r in rows.values():
        r["estimate"] = _est(off_rows, kb.SET_TO_PLAYS.get(r["label"], []))
    return list(rows.values())[:top]


GROUPS = ("base", "pick-and-roll", "zone", "junk", "pressure", "help", "other")


def _named_defense(reported, season, labels, def_rows):
    """Named defensive schemes (man-to-man, drop, switch, 2-3 zone...): base man-to-man, then coverages reported for this
    game, then the team's season-reported coverages, then drop inferred from this game's shots. Help leans (pack the
    paint, run shooters off, turnover-forcing, rim protector) are tendencies, not schemes."""
    rows = {"man-to-man": _scheme("man-to-man", "base", def_rows)}
    for x in reported:
        if x["kind"] == "coverage":
            rows[x["label"]] = _scheme(x["label"], "this game", def_rows, summary=x["summary"], url=x["url"])
    for k, x in season.items():
        if x["kind"] == "coverage" and k not in rows:
            rows[k] = _scheme(k, "season", def_rows, summary=x["summary"], url=x["url"], mentions=x["n"])
    tend = []
    for l in labels:
        k = kb.lean_key(l["label"]) if l["side"] == "defense" else None
        if k == "drop" and (k not in rows or rows[k]["source"] == "season"):    # this game's shots back it up
            rows[k] = _scheme(k, "inferred", def_rows, evidence=l["evidence"], confidence=l["confidence"])
        elif k and k != "drop":
            tend.append(_scheme(k, "inferred", def_rows, evidence=l["evidence"], confidence=l["confidence"]))
    return sorted(rows.values(), key=lambda r: GROUPS.index(r["group"]) if r["group"] in GROUPS else 9), tend


def build(conn, season, teams=FOCUS) -> dict:
    S = schemes.shots(conn, season)
    lg = plays.league(S)
    by = defaultdict(list)                       # (game_id, team) -> that team's shots in that game
    for s in S:
        by[(s["game_id"], s["team"])].append(s)
    mix = {k: schemes._mix(v) for k, v in by.items()}
    p_off, l_off = _pcts({k: schemes._flat(m) for k, m in mix.items()})
    box = {(r["game_id"], r["team"]): dict(r) for r in conn.execute(
        "select b.* from team_box b join games g on g.game_id=b.game_id where g.season=?", (season,))}
    pace_pct, _ = _pcts({k: {"poss": b["poss"] or 0} for k, b in box.items()})
    tov_pct, _ = _pcts({k: {"tov": (b["tov"] or 0) / max(1, b["poss"] or 1)} for k, b in box.items()})   # key = team that turned it over
    films, season_named = defaultdict(list), defaultdict(dict)
    for r in conn.execute("""select team, kind, label, count(*) n, max(url) url, max(summary) summary from film_items
                             group by team, kind, label order by n desc"""):
        season_named[r["team"]][r["label"]] = dict(r)
    for r in conn.execute("select * from film_items where game_id != '' order by id"):
        films[(r["game_id"], r["team"])].append(dict(r))
    out = {}
    for t in teams:
        games = []
        for g in conn.execute("""select * from games where season=? and status='final' and source is not null and ? in (home, away)
                                 order by date desc""", (season, t)):
            gid, home = g["game_id"], g["home"] == t
            o = g["away"] if home else g["home"]
            if (gid, t) not in mix or (gid, o) not in mix:
                continue
            gs = by[(gid, t)] + by[(gid, o)]
            off_rows = plays.team_plays(gs, t, lg, "offense")
            def_rows = plays.team_plays(gs, t, lg, "defense")
            usage = Counter(s["player_id"] for s in by[(gid, t)])
            top_share = usage.most_common(1)[0][1] / max(1, len(by[(gid, t)]))
            blk = Counter(s["blocker"] for s in by[(gid, o)] if s["blocker"])
            blk_name, blk_n = blk.most_common(1)[0] if blk else (None, 0)
            labels = schemes.infer(t, mix[(gid, t)], mix[(gid, o)], p_off[(gid, t)], p_off[(gid, o)], l_off, l_off,
                                   pace_pct.get((gid, t), {}).get("poss", 50), tov_pct.get((gid, o), {}).get("tov", 50),
                                   top_share, blk_name, blk_n if blk_n >= 4 else 0)   # one game: 4+ blocks, not 1.6 a night
            reported = films.get((gid, t), [])
            sets = _named_sets(reported, season_named[t], off_rows)
            defense, tendencies = _named_defense(reported, season_named[t], labels, def_rows)
            mo, md = mix[(gid, t)], mix[(gid, o)]
            pts, opts = (g["home_pts"], g["away_pts"]) if home else (g["away_pts"], g["home_pts"])
            games.append({
                "game_id": gid, "date": g["date"], "opp": o, "home": home, "pts": pts, "opp_pts": opts, "ot": g["ot"],
                "result": "W" if pts > opts else "L", "cup": "Cup" in (g["note"] or ""),
                "offense": {"plays": off_rows[:4], "styles": [l for l in labels if l["side"] == "offense"], "sets": sets,
                            "pps": round(mo["pps"], 3), "three_rate": round(mo["three_rate"], 3), "rim_rate": round(mo["rim_rate"], 3),
                            "assisted": round(mo["assisted"], 3), "transition": round(mo["transition"], 3)},
                "defense": {"schemes": defense, "tendencies": tendencies, "allowed": def_rows[:3], "opp_pps": round(md["pps"], 3),
                            "opp_rim": round(md["rim_rate"], 3), "opp_three": round(md["three_rate"], 3),
                            "opp_mid": round(md["mid_rate"], 3), "blocks": sum(blk.values()), "anchor": blk_name if blk_n >= 3 else None,
                            "forced_tov": (box.get((gid, o)) or {}).get("tov")},
            })
        out[t] = {"games": games, "record": f"{sum(x['result'] == 'W' for x in games)}-{sum(x['result'] == 'L' for x in games)}",
                  "def_counts": Counter(s["label"] for x in games for s in x["defense"]["schemes"] if s["source"] != "season").most_common(),
                  "set_counts": Counter(s["label"] for x in games for s in x["offense"]["sets"] if s["source"] == "this game").most_common(),
                  "tendency_counts": Counter(s["label"] for x in games for s in x["defense"]["tendencies"]).most_common(),
                  "off_counts": Counter(s["label"].split(" (")[0] for x in games for s in x["offense"]["styles"]).most_common(),
                  "play_counts": Counter(x["offense"]["plays"][0]["play"] for x in games if x["offense"]["plays"]).most_common(3)}
    return out
