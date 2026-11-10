"""Offense and defense profiles from ESPN play-by-play, plus inferred scheme labels.

Data honesty (spec "Plays and Schemes"): NBA.com play types / matchups are blocked from the home network, so
nothing here is a Synergy play type. What IS measured from play-by-play: shot type (ESPN's own label), distance
and zone, assisted or not, transition (shot within 8 s of gaining the ball), putbacks, blocks, steals.
Scheme labels built on top of those are 'inferred', carry a confidence capped at 0.7 and the evidence numbers.
Switching and zone defense cannot be inferred without matchup data; they are listed as not measurable.
"""
import math
import re
from collections import defaultdict

KINDS = ("drive", "cut", "pull-up", "spot-up jumper", "post-up / turnaround", "putback", "alley-oop", "rim finish")
ZONES = ("rim", "short mid", "long mid", "corner 3", "above-break 3")
NOT_MEASURABLE = ["switching", "zone defense", "drop vs hedge vs blitz coverage", "named sets (horns, Spain PnR, floppy)"]
_FT = re.compile(r"(\d+)-foot")
_AST = re.compile(r"\(([^()]+?) assists\)")
_BLK = re.compile(r"^(.+?) blocks ")
_STL = re.compile(r"\(([^()]+?) steals\)")


def kind_of(ptype: str, assisted: bool) -> str | None:
    t = ptype or ""
    if "Heave" in t:
        return None
    if "Putback" in t or "Tip" in t:
        return "putback"
    if "Alley Oop" in t:
        return "alley-oop"
    if "Cutting" in t:
        return "cut"
    if "Driving" in t or "Floating" in t or ("Running" in t and ("Layup" in t or "Dunk" in t or "Finger" in t)):
        return "drive"
    if "Pullup" in t or "Step Back" in t or ("Running" in t and "Jump" in t):
        return "pull-up"
    if "Turnaround" in t or "Fade Away" in t or "Hook" in t or "Post" in t:
        return "post-up / turnaround"
    if "Jump" in t:     # plain jumper: ESPN labels pull-ups / step-backs explicitly; assists are only known on makes
        return "spot-up jumper"
    return "rim finish"


def zone_of(text: str, x, y, three: bool) -> str:
    m = _FT.search(text or "")
    dist = int(m.group(1)) if m else (math.hypot((x or 25) - 25, y or 0) if x is not None else 0)
    if three:
        return "corner 3" if (y is not None and y <= 9) else "above-break 3"
    if dist <= 4:
        return "rim"
    return "short mid" if dist <= 14 else "long mid"


def shots(conn, season, before=None) -> list[dict]:
    """Every field-goal attempt this season with its classification. One pass over the plays table."""
    q = """select p.game_id, p.seq, p.period, p.clock_s, p.type, p.text, p.team, p.player_id, p.x, p.y, p.shooting,
                  p.scoring, g.home, g.away, g.date
           from plays p join games g on g.game_id = p.game_id
           where g.season = ? %s order by p.game_id, p.seq""" % ("and g.date < ?" if before else "")
    rows = conn.execute(q, [season] + ([before] if before else [])).fetchall()
    out, gain, cur = [], {}, None
    for r in rows:
        if r["game_id"] != cur:
            cur, gain = r["game_id"], {}
        t, team = r["type"] or "", r["team"]
        opp = r["away"] if team == r["home"] else r["home"]
        if t == "Defensive Rebound" and team:
            gain[team] = (r["period"], r["clock_s"])
        elif "Turnover" in t and team:
            gain[opp] = (r["period"], r["clock_s"])
        if not r["shooting"] or "Free Throw" in t or not team:
            continue
        text = r["text"] or ""
        three = "three point" in text
        assisted = bool(_AST.search(text))
        k = kind_of(t, assisted)
        if k is None:
            continue
        g = gain.get(team)
        trans = bool(g and g[0] == r["period"] and 0 <= g[1] - r["clock_s"] <= 8)
        gain.pop(team, None)
        blk = _BLK.search(text)
        made = " makes " in text
        out.append({"game_id": r["game_id"], "date": r["date"], "team": team, "opp": opp, "player_id": r["player_id"], "period": r["period"],
                    "kind": k, "zone": zone_of(text, r["x"], r["y"], three), "three": three, "made": made,
                    "pts": (3 if three else 2) if made else 0, "assisted": assisted, "transition": trans,
                    "assister": _AST.search(text).group(1).strip() if assisted else None,
                    "blocker": blk.group(1).strip() if blk else None})
    return out


def steals(conn, season, before=None) -> dict:
    """{(team_of_stealer, name): count} parsed from turnover text."""
    q = """select p.text, p.team, g.home, g.away from plays p join games g on g.game_id=p.game_id
           where g.season=? and p.text like '%steals)%' %s""" % ("and g.date < ?" if before else "")
    out = defaultdict(int)
    for r in conn.execute(q, [season] + ([before] if before else [])):
        m = _STL.search(r["text"] or "")
        if m:
            opp = r["away"] if r["team"] == r["home"] else r["home"]
            out[(opp, m.group(1).strip())] += 1
    return out


def _mix(rows):
    n = len(rows) or 1
    made_fg = [s for s in rows if s["made"]]
    d = {"n": len(rows),
         "kind": {k: sum(s["kind"] == k for s in rows) / n for k in KINDS},
         "kind_pps": {k: (sum(s["pts"] for s in rows if s["kind"] == k) / max(1, sum(s["kind"] == k for s in rows))) for k in KINDS},
         "zone": {z: sum(s["zone"] == z for s in rows) / n for z in ZONES},
         "zone_fg": {z: (sum(s["made"] for s in rows if s["zone"] == z) / max(1, sum(s["zone"] == z for s in rows))) for z in ZONES},
         "three_rate": sum(s["three"] for s in rows) / n,
         "rim_rate": sum(s["zone"] == "rim" for s in rows) / n,
         "mid_rate": sum(s["zone"] in ("short mid", "long mid") for s in rows) / n,
         "assisted": sum(s["assisted"] for s in made_fg) / max(1, len(made_fg)),
         "transition": sum(s["transition"] for s in rows) / n,
         "pps": sum(s["pts"] for s in rows) / n,
         "efg": (sum(s["made"] for s in rows) + 0.5 * sum(s["made"] and s["three"] for s in rows)) / n}
    return d


def _flat(m: dict) -> dict:
    f = {k: v for k, v in m.items() if isinstance(v, float)}
    for grp in ("kind", "zone", "zone_fg"):     # zone_fg feeds the rim-protector rule (was missing: it never fired)
        for k, v in m[grp].items():
            f[f"{grp}:{k}"] = v
    return f


def team_profiles(conn, season, team_rows: dict, before=None) -> dict:
    """{team: {offense, defense, pct_off, pct_def, labels}}. team_rows: aggregator.team_season output (pace, factors)."""
    S = shots(conn, season, before)
    by_off, by_def = defaultdict(list), defaultdict(list)
    for s in S:
        by_off[s["team"]].append(s)
        by_def[s["opp"]].append(s)
    teams = sorted(set(by_off) | set(by_def))
    off = {t: _mix(by_off[t]) for t in teams}
    de = {t: _mix(by_def[t]) for t in teams}
    blocks = defaultdict(lambda: defaultdict(int))
    for s in S:
        if s["blocker"]:
            blocks[s["opp"]][s["blocker"]] += 1
    usage = defaultdict(lambda: defaultdict(int))
    for s in S:
        usage[s["team"]][s["player_id"]] += 1

    def pct_table(prof):
        flat = {t: _flat(prof[t]) for t in teams}
        keys = set().union(*[set(v) for v in flat.values()]) if flat else set()
        out = {t: {} for t in teams}
        for k in keys:
            vals = sorted(flat[t].get(k, 0) for t in teams)
            for t in teams:
                v = flat[t].get(k, 0)
                out[t][k] = round(100 * sum(x < v for x in vals) / max(1, len(vals) - 1))
        lg = {k: sum(flat[t].get(k, 0) for t in teams) / max(1, len(teams)) for k in keys}
        return out, lg
    p_off, lg_off = pct_table(off)
    p_def, lg_def = pct_table(de)
    pace_rank = _ranks({t: (team_rows.get(t) or {}).get("pace", 0) for t in teams})
    tov_rank = _ranks({t: _opp_tov(conn, season, t, before) for t in teams})
    result = {}
    for t in teams:
        top_pid, top_n = max(usage[t].items(), key=lambda x: x[1], default=(None, 0))
        share = top_n / max(1, sum(usage[t].values()))
        blk_name, blk_n = max(blocks[t].items(), key=lambda x: x[1], default=(None, 0))
        gp = (team_rows.get(t) or {}).get("games", 0) or 1
        result[t] = {"offense": off[t], "defense": de[t], "pct_off": p_off[t], "pct_def": p_def[t],
                     "pace_pct": pace_rank.get(t), "forced_tov_pct": tov_rank.get(t),
                     "top_shooter": {"player_id": top_pid, "share": share},
                     "top_blocker": {"name": blk_name, "per_game": blk_n / gp},
                     "labels": infer(t, off[t], de[t], p_off[t], p_def[t], lg_off, lg_def, pace_rank.get(t, 50),
                                     tov_rank.get(t, 50), share, blk_name, blk_n / gp)}
    result["_league"] = {"offense": lg_off, "defense": lg_def}
    return result


def _ranks(vals: dict) -> dict:
    v = sorted(vals.values())
    return {t: round(100 * sum(x < x0 for x in v) / max(1, len(v) - 1)) for t, x0 in vals.items()}


def _opp_tov(conn, season, team, before):
    r = conn.execute("""select sum(o.tov) tov, sum(o.poss) poss from team_box t join team_box o on o.game_id=t.game_id and o.team=t.opp
                        join games g on g.game_id=t.game_id where t.team=? and g.season=? %s""" % ("and g.date < ?" if before else ""),
                     [team, season] + ([before] if before else [])).fetchone()
    return (r["tov"] or 0) / max(1, r["poss"] or 1)


def _lab(label, side, conf, evidence):
    return {"label": label, "side": side, "source": "inferred", "confidence": round(min(0.7, max(0.2, conf)), 2), "evidence": evidence}


def _ev(name, val, lg, pct):
    return f"{name} {100 * val:.0f}% (league {100 * lg:.0f}%, {pct}th pct)"


def infer(t, o, d, po, pd_, lo, ld, pace_pct, tov_pct, top_share, blk_name, blk_pg) -> list[dict]:
    L = []
    k = lambda key: po.get(key, 50)
    kd = lambda key: pd_.get(key, 50)
    if pace_pct >= 70 and k("three_rate") >= 70:
        L.append(_lab("Pace and space", "offense", (pace_pct + k("three_rate")) / 200,
                      f"pace {pace_pct}th pct; " + _ev("3PA share", o["three_rate"], lo.get("three_rate", 0), k("three_rate"))))
    if k("transition") >= 80:
        L.append(_lab("Runs in transition", "offense", k("transition") / 130, _ev("transition shots", o["transition"], lo.get("transition", 0), k("transition"))))
    if k("kind:drive") >= 75:
        L.append(_lab("Drive-heavy, rim pressure", "offense", k("kind:drive") / 130, _ev("drives", o["kind"]["drive"], lo.get("kind:drive", 0), k("kind:drive"))))
    if k("kind:pull-up") >= 75 and k("assisted") <= 40:
        L.append(_lab("Self-creation heavy (pick-and-roll or isolation likely)", "offense", (k("kind:pull-up") + 100 - k("assisted")) / 260,
                      _ev("pull-ups", o["kind"]["pull-up"], lo.get("kind:pull-up", 0), k("kind:pull-up")) + "; "
                      + _ev("assisted makes", o["assisted"], lo.get("assisted", 0), k("assisted"))))
    if k("assisted") >= 75 and k("kind:cut") >= 60:
        L.append(_lab("Ball movement / motion", "offense", (k("assisted") + k("kind:cut")) / 260,
                      _ev("assisted makes", o["assisted"], lo.get("assisted", 0), k("assisted")) + "; " + _ev("cuts", o["kind"]["cut"], lo.get("kind:cut", 0), k("kind:cut"))))
    if top_share >= 0.27:
        L.append(_lab("Heliocentric (one main creator)", "offense", top_share * 1.6, f"top shooter takes {100 * top_share:.0f}% of the team's shots"))
    if k("kind:post-up / turnaround") >= 80:
        L.append(_lab("Post hub", "offense", k("kind:post-up / turnaround") / 130,
                      _ev("post / turnaround shots", o["kind"]["post-up / turnaround"], lo.get("kind:post-up / turnaround", 0), k("kind:post-up / turnaround"))))
    if k("kind:putback") >= 80:
        L.append(_lab("Crashes the offensive glass", "offense", k("kind:putback") / 130, _ev("putbacks", o["kind"]["putback"], lo.get("kind:putback", 0), k("kind:putback"))))
    if kd("rim_rate") <= 25 and kd("three_rate") >= 65:
        L.append(_lab("Packs the paint", "defense", (100 - kd("rim_rate") + kd("three_rate")) / 260,
                      _ev("opponent rim shots", d["rim_rate"], ld.get("rim_rate", 0), kd("rim_rate")) + "; " + _ev("opponent 3PA share", d["three_rate"], ld.get("three_rate", 0), kd("three_rate"))))
    if blk_pg >= 1.6 and kd("zone_fg:rim") <= 40:
        L.append(_lab(f"Rim protector anchored ({blk_name})", "defense", (blk_pg / 3.5 + (100 - kd('zone_fg:rim')) / 200) / 2,
                      f"{blk_name} {blk_pg:.1f} blocks/game; " + _ev("opponent FG at rim", d["zone_fg"]["rim"], ld.get("zone_fg:rim", 0), kd("zone_fg:rim"))))
    if kd("three_rate") <= 25:
        L.append(_lab("Runs shooters off the line", "defense", (100 - kd("three_rate")) / 130, _ev("opponent 3PA share", d["three_rate"], ld.get("three_rate", 0), kd("three_rate"))))
    if tov_pct >= 80:
        L.append(_lab("Turnover-forcing (aggressive, traps or blitzes likely)", "defense", tov_pct / 130, f"forces turnovers at the {tov_pct}th pct"))
    if kd("mid_rate") >= 75 and kd("rim_rate") <= 50:
        L.append(_lab("Concedes the mid-range (drop-style)", "defense", kd("mid_rate") / 180,
                      _ev("opponent mid-range share", d["mid_rate"], ld.get("mid_rate", 0), kd("mid_rate")) + "; drop coverage usually looks like this"))
    return L


# ---------- league view: styles, ranks, what wins ----------

STYLE_METRICS = [   # (key in profile, side, label for people, higher_is "more of it")
    ("pace", "off", "Pace"), ("three_rate", "off", "3-point share"), ("rim_rate", "off", "Rim share"),
    ("kind:drive", "off", "Drives"), ("kind:pull-up", "off", "Pull-ups"), ("assisted", "off", "Assisted makes"),
    ("transition", "off", "Transition shots"), ("kind:putback", "off", "Putbacks"),
    ("rim_rate", "def", "Opp. rim share"), ("three_rate", "def", "Opp. 3-point share"), ("mid_rate", "def", "Opp. mid-range share"),
    ("forced_tov", "def", "Forced turnovers"),
]


def _rank_desc(vals: dict) -> dict:
    order = sorted(vals, key=lambda t: -vals[t])
    return {t: i + 1 for i, t in enumerate(order)}


def league_styles(P: dict, ratings_now: dict, records: dict, pace: dict, forced_tov: dict) -> list[dict]:
    """One row per team: ranks (1 = best) + inferred styles + the raw style numbers."""
    teams = [t for t in P if not t.startswith("_") and t in ratings_now]
    off = {t: ratings_now[t]["off"] for t in teams}
    de = {t: ratings_now[t]["def"] for t in teams}          # positive = fewer points allowed
    net = {t: ratings_now[t]["net"] for t in teams}
    ro, rd, rn = _rank_desc(off), _rank_desc(de), _rank_desc(net)
    out = []
    for t in teams:
        o, d = P[t]["offense"], P[t]["defense"]
        flat_o, flat_d = _flat(o), _flat(d)
        m = {}
        for key, side, _ in STYLE_METRICS:
            if key == "pace":
                m["off:pace"] = None if pace.get(t) is None else float(pace[t])
            elif key == "forced_tov":
                m["def:forced_tov"] = None if forced_tov.get(t) is None else float(forced_tov[t])
            else:
                m[f"{side}:{key}"] = (flat_o if side == "off" else flat_d).get(key)
        out.append({"team": t, "record": records.get(t, ""), "off": round(off[t], 1), "def": round(de[t], 1), "net": round(net[t], 1),
                    "off_rank": ro[t], "def_rank": rd[t], "net_rank": rn[t],
                    "off_styles": [l for l in P[t]["labels"] if l["side"] == "offense"],
                    "def_styles": [l for l in P[t]["labels"] if l["side"] == "defense"], "metrics": m})
    return sorted(out, key=lambda r: r["net_rank"])


def _pearson(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return None if not sx or not sy else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def what_wins(styles: list[dict]) -> list[dict]:
    """Correlation of each style number with net rating across the league (30 points: suggestive, not proof)."""
    out = []
    for key, side, label in STYLE_METRICS:
        k = f"{side}:{key}"
        pts = [(r["metrics"].get(k), r["net"]) for r in styles if r["metrics"].get(k) is not None]
        r = _pearson([p[0] for p in pts], [p[1] for p in pts])
        if r is not None:
            out.append({"metric": label, "side": "offense" if side == "off" else "defense", "r": round(float(r), 2), "n": len(pts)})
    return sorted(out, key=lambda x: -abs(x["r"]))


def record_vs_styles(conn, season, team, P: dict) -> list[dict]:
    """This team's record and average margin against opponents grouped by the opponent's inferred styles."""
    groups = defaultdict(lambda: {"w": 0, "l": 0, "margin": 0.0, "n": 0})
    for g in conn.execute("""select * from games where season=? and status='final' and home_pts is not null
                             and (home=? or away=?)""", (season, team, team)):
        opp = g["away"] if g["home"] == team else g["home"]
        m = (g["home_pts"] - g["away_pts"]) * (1 if g["home"] == team else -1)
        for l in (P.get(opp) or {}).get("labels", []):
            key = (("vs defense: " if l["side"] == "defense" else "vs offense: ") + l["label"])
            x = groups[key]
            x["w"] += m > 0
            x["l"] += m < 0
            x["margin"] += m
            x["n"] += 1
    return sorted(({"style": k, "record": f"{v['w']}-{v['l']}", "games": v["n"], "avg_margin": round(v["margin"] / v["n"], 1)}
                   for k, v in groups.items()), key=lambda x: -x["games"])


def counter_evidence(conn, season, P: dict, reported: dict | None = None, min_shots=150) -> dict:
    """For each defense type, which shot types score best against the teams that play it (pts/shot vs league).
    Defense types come from our inferred leans plus coverages reported in the film room ({team: [coverage, ...]})."""
    from . import playbook_kb as kb
    teams_by = defaultdict(set)
    for t, v in P.items():
        if t.startswith("_"):
            continue
        for l in v["labels"]:
            if l["side"] == "defense" and kb.lean_key(l["label"]):
                teams_by[kb.lean_key(l["label"])].add(t)
    for t, covs in (reported or {}).items():
        for c in covs:
            if c in kb.COUNTERS:
                teams_by[c].add(t)
    S = shots(conn, season)
    lg = {k: [0, 0] for k in KINDS}
    for s in S:
        lg[s["kind"]][0] += s["pts"]
        lg[s["kind"]][1] += 1
    lg_pps = {k: v[0] / max(1, v[1]) for k, v in lg.items()}
    out = {}
    for key, teams in teams_by.items():
        vs = [s for s in S if s["opp"] in teams]
        if len(vs) < min_shots:
            out[key] = {"teams": sorted(teams), "n": len(vs), "kinds": [], "small_sample": True}
            continue
        rows = []
        for k in KINDS:
            ks = [s for s in vs if s["kind"] == k]
            if len(ks) >= 25:
                pps = sum(s["pts"] for s in ks) / len(ks)
                rows.append({"kind": k, "pps": round(pps, 3), "vs_lg": round(pps - lg_pps[k], 3), "n": len(ks),
                             "share": round(len(ks) / len(vs), 3)})
        out[key] = {"teams": sorted(teams), "n": len(vs), "small_sample": False,
                    "kinds": sorted(rows, key=lambda r: -r["vs_lg"])}
    out["_league_pps"] = {k: round(v, 3) for k, v in lg_pps.items()}
    return out
