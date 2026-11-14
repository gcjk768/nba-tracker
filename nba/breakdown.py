"""Game breakdown by a small team of analysts: why this team won or lost THIS game, not general stats.

Code builds the facts for one team in one game from what is already measured (game_log: how the shots came, named
plays and coverages reported for that game, the defense read from the shots; player_box; the points-of-margin split).
Three specialists each read their own slice, in parallel: offence, defence, players. A head coach then reads their
findings plus the margin split and writes the verdict. No agent may use a number that is not in the facts it was
given: clean() drops any finding or reason that does. That check is code, not a model.
Stored in game_breakdowns (one row per game and team), shown on the game card (web.game_log_team).
"""
import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal

from . import analyst, db, game_log, insights as ins, playbook_kb as kb, plays, schemes

log = logging.getLogger(__name__)
ROLES = ("offence", "defence", "players")
MAX_PER_RUN = int(os.environ.get("BREAKDOWN_MAX", 30))      # team-games per run; 4 claude calls each
MAX_NOTES = int(os.environ.get("PLAYBOOK_NOTES_MAX", 10))   # team playbook reads per run; 1 claude call each
VERSION = 2              # bump to redo every stored breakdown after a prompt or facts change


class Finding(analyst._M):
    point: str
    evidence: str
    effect: Literal["helped", "hurt", "neutral"] = "neutral"


class Findings(analyst._M):
    findings: list[Finding]


class Reason(analyst._M):
    reason: str
    evidence: str
    side: Literal["offence", "defence", "players", "other"] = "other"


class Verdict(analyst._M):
    headline: str
    reasons: list[Reason]
    next_time: str = ""


class Playbook(analyst._M):
    headline: str
    notes: list[Finding]


class Down(Exception):
    """claude gave no reply at all: stop the run, store nothing, try again next time."""


# ---------- facts ----------

def _pct(x):
    return round(100 * (x or 0))


def _play(r):
    return {"play": r["play"], "made": round(r["success"] * r["att"]), "attempts": r["att"], "made_pct": _pct(r["success"]),
            "league_made_pct": _pct(r["lg_success"]), "points_per_shot": round(r["pps"], 2), "league_points_per_shot": round(r["lg_pps"], 2)}


def _reported(rows):
    """Named plays / coverages a write-up of THIS game reported (never the season playbook: that is not this game)."""
    return [{"name": x["label"], "what_the_writeup_says": x.get("summary") or "", "worked": x.get("impact") or "not stated",
             "effect": x.get("effect") or ""} for x in rows if x["source"] == "this game"]


def _players(conn, gid, team, top=8):
    rows = [dict(r) for r in conn.execute("select * from player_box where game_id=? and team=? and coalesce(dnp, 0)=0 "
                                          "order by minutes desc limit ?", (gid, team, top))]
    wpa = {r["key"]: r["wpa"] for r in conn.execute("select key, wpa from player_wpa where game_id=? and team=?", (gid, team))}
    return [{"name": r["name"], "starter": bool(r["starter"]), "minutes": round(r["minutes"] or 0), "points": r["pts"],
             "shots": f"{r['fgm']} of {r['fga']}", "threes": f"{r['fg3m']} of {r['fg3a']}", "free_throws": f"{r['ftm']} of {r['fta']}",
             "rebounds": r["reb"], "assists": r["ast"], "steals": r["stl"], "blocks": r["blk"], "turnovers": r["tov"],
             "plus_minus": r["plus_minus"],
             **({"win_probability_added_pct": round(100 * wpa[r["player_id"]], 1)} if r["player_id"] in wpa else {})} for r in rows]


def _box(conn, gid, team):
    """The possession battle the shot analysts do not see: free throws, turnovers, offensive rebounds, easy points."""
    r = conn.execute("select * from team_box where game_id=? and team=?", (gid, team)).fetchone()
    return {} if not r else {"free_throws": f"{r['ftm']} of {r['fta']}", "turnovers": r["tov"], "offensive_rebounds": r["oreb"],
                             "points_off_turnovers": r["pts_off_to"], "fast_break_points": r["fastbreak"],
                             "points_in_the_paint": r["paint"], "largest_lead": r["largest_lead"]}


def facts(conn, g, og, team, league_pps) -> dict:
    """{role: facts} for `team` in game entry `g` (game_log.build); `og` is the opponent's entry for the same game."""
    o, d, opp = g["offense"], g["defense"], g["opp"]
    head = {"team": team, "opponent": opp, "date": g["date"], "venue": "home" if g["home"] else "away",
            "result": "won" if g["result"] == "W" else "lost", "score": f"{g['pts']}-{g['opp_pts']}", "overtime": bool(g["ot"])}
    opp_d = [x["label"] for x in og["defense"]["schemes"] if x["source"] != "season"] + [x["label"] for x in og["defense"]["tendencies"]]
    parts = (ins.game_recipe(conn, g["game_id"]).get(team) or {})
    return {
        "offence": {**head, "points_per_shot": round(o["pps"], 2), "league_points_per_shot": round(league_pps, 2),
                    "three_point_share_pct": _pct(o["three_rate"]), "rim_share_pct": _pct(o["rim_rate"]),
                    "assisted_makes_pct": _pct(o["assisted"]), "transition_share_pct": _pct(o["transition"]),
                    "how_the_shots_came": [_play(r) for r in o["plays"]],
                    "style_read_from_the_shots": [{"style": x["label"], "evidence": x["evidence"]} for x in o["styles"]],
                    "named_plays_reported_for_this_game": _reported(o["sets"]),
                    "opponent_defence": opp_d},
        "defence": {**head, "opponent_points_per_shot": round(d["opp_pps"], 2), "league_points_per_shot": round(league_pps, 2),
                    "opponent_rim_share_pct": _pct(d["opp_rim"]), "opponent_three_share_pct": _pct(d["opp_three"]),
                    "opponent_midrange_share_pct": _pct(d["opp_mid"]), "blocks": d["blocks"], "rim_protector": d["anchor"],
                    "forced_turnovers": d["forced_tov"],
                    "coverages_reported_for_this_game": _reported(d["schemes"]),
                    "read_from_the_shots": [{"tendency": x["label"], "evidence": x.get("evidence") or ""}
                                            for x in d["tendencies"] + [s for s in d["schemes"] if s["source"] == "inferred"]],
                    "what_the_opponent_scored_on": [_play(r) for r in d["allowed"]],
                    "opponent_named_plays_reported": _reported(og["offense"]["sets"])},
        "players": {**head, "players": _players(conn, g["game_id"], team),
                    "opponent_top_players": sorted(_players(conn, g["game_id"], opp), key=lambda p: -(p["points"] or 0))[:3]},
        "coach": {**head, "margin": g["pts"] - g["opp_pts"],
                  "points_of_margin_by_factor": {k: round(v, 1) for k, v in parts.items() if v},
                  "box_score": {"team": _box(conn, g["game_id"], team), "opponent": _box(conn, g["game_id"], opp)}},
    }


# ---------- the number guard ----------

_NUM = re.compile(r"\d+(?:\.\d+)?")
FREE = {1.0, 2.0, 3.0, 4.0}        # "3-pointer", "2 of", "4th quarter": too common to mean anything


def numbers(obj) -> set:
    """Every number in a facts packet, wherever it sits (values, and inside strings such as "9 of 20" or "123-112")."""
    return {round(float(x), 2) for x in _NUM.findall(json.dumps(obj, ensure_ascii=False))}


def invented(text: str, allowed: set) -> list[str]:
    return [x for x in _NUM.findall(text) if round(float(x), 2) not in allowed | FREE]


def clean(items, allowed):
    """Keep only the findings / reasons whose every number is in the facts. Returns (kept, invented numbers)."""
    kept, bad = [], []
    for it in items:
        inv = invented(" ".join(str(v) for v in it.model_dump().values()), allowed)
        (bad.extend if inv else kept.append)(inv or it)
    return kept, bad


# ---------- the agents ----------

def _ask(prompt, packet, model, runner, schema):
    res, raw = analyst.run(prompt, json.dumps(packet, ensure_ascii=False), model, schema=schema, **({"runner": runner} if runner else {}))
    if res is None and not raw:
        raise Down(prompt)
    return res


def specialist(role, packet, model, runner=None) -> list[dict]:
    """One specialist: 2-4 findings from its own facts. Findings with a number that is not in the facts are dropped;
    if that leaves nothing, it is asked once more and told which numbers were invented."""
    allowed, extra = numbers(packet), {}
    for _ in range(2):
        res = _ask(f"breakdown_{role}.md", {**packet, **extra}, model, runner, Findings)
        kept, bad = clean(res.findings[:4] if res else [], allowed)
        if kept or not bad:
            return [f.model_dump() for f in kept]
        extra = {"your_last_reply_was_rejected": f"it used numbers that are not in the facts: {', '.join(bad[:8])}. Use only numbers you were given."}
    return []


def coach(packet, model, runner=None) -> dict | None:
    """The verdict from the specialists' findings and the margin split. Same number rule; one retry; then the reasons
    that pass are kept and a headline with an invented number is replaced by the plain result."""
    allowed, extra, res = numbers(packet), {}, None
    for _ in range(2):
        res = _ask("breakdown_coach.md", {**packet, **extra}, model, runner, Verdict)
        if res is None:
            return None
        bad = invented(" ".join([res.headline, res.next_time] + [r.reason + " " + r.evidence for r in res.reasons]), allowed)
        if not bad:
            break
        extra = {"your_last_reply_was_rejected": f"it used numbers that are not in the facts: {', '.join(bad[:8])}. Use only numbers you were given."}
    reasons, _ = clean(res.reasons[:3], allowed)
    plain = f"{packet['team']} {packet['result']} {packet['score']}"
    return {"headline": plain if invented(res.headline, allowed) else res.headline,
            "reasons": [r.model_dump() for r in reasons], "next_time": "" if invented(res.next_time, allowed) else res.next_time}


def analyse(F, fast, smart, runner=None) -> dict | None:
    """Three specialists in parallel, then the coach. None when the coach gave nothing usable."""
    with ThreadPoolExecutor(max_workers=len(ROLES)) as pool:
        got = dict(zip(ROLES, pool.map(lambda r: specialist(r, F[r], fast, runner), ROLES)))
    verdict = coach({**F["coach"], **{f"{r}_findings": got[r] for r in ROLES}}, smart, runner)
    return {**verdict, **got} if verdict else None


# ---------- run + store ----------

def stored(conn, team) -> dict:
    return {r["game_id"]: json.loads(r["body"]) for r in conn.execute("select game_id, body from game_breakdowns where team=?", (team,))}


def run(conn, season, fast, smart, runner=None, limit=MAX_PER_RUN, teams=None) -> dict:
    """Break down the newest finished games of the tracked teams that have no breakdown yet, or whose facts changed
    (a write-up of that game arrived). Returns counts; stops quietly when claude is down."""
    every = game_log.all_teams(conn, season)
    first = game_log.tracked(conn)
    teams = list(teams or first + [t for t in every if t not in first])      # every team; the tracked ones first
    G = game_log.build(conn, season, sorted(set(every) | set(teams)))
    have = {(r["game_id"], r["team"]): r["hash"] for r in conn.execute("select game_id, team, hash from game_breakdowns")}
    todo = []
    for t in teams:
        for g in G.get(t, {}).get("games", []):
            og = next((x for x in G.get(g["opp"], {}).get("games", []) if x["game_id"] == g["game_id"]), None)
            if og:
                F = facts(conn, g, og, t, G["_league"]["pps"])
                # Redo a game only when its own inputs change (a write-up of it arrived), not when league averages move.
                named = sorted(x["name"] for k in ("named_plays_reported_for_this_game",) for x in F["offence"][k]) +                     sorted(x["name"] for k in ("coverages_reported_for_this_game", "opponent_named_plays_reported") for x in F["defence"][k])
                h = hashlib.sha1(json.dumps([VERSION, named]).encode()).hexdigest()[:16]
                if have.get((g["game_id"], t)) != h:
                    todo.append((g["date"], t, g["game_id"], h, F))
    todo.sort(key=lambda x: (x[0], x[1] in first), reverse=True)             # newest first; tracked teams first on a day
    out = {"done": 0, "left": max(0, len(todo) - limit), "failed": 0, "stop": None}
    for d, t, gid, h, F in todo[:limit]:
        try:
            body = analyse(F, fast, smart, runner)
        except Down as e:
            out["stop"] = f"claude unavailable ({e})"
            break
        if not body:
            out["failed"] += 1
            continue
        with db.tx(conn):
            conn.execute("insert or replace into game_breakdowns(game_id, team, hash, created, body) values (?,?,?,?,?)",
                         (gid, t, h, datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(body, ensure_ascii=False)))
        out["done"] += 1
    if out["done"] or out["stop"] or out["failed"]:
        log.info("game breakdowns: %s", out)
    return out


# ---------- the playbook analyst: a team's named sets and coverages across its games ----------

def playbook_facts(conn, season, team, G, S, lg) -> dict | None:
    """What has been reported about this team's named plays and coverages, game by game, next to how its shots have
    gone all season. None when no breakdown has named anything for the team yet (nothing to follow)."""
    games = {g["game_id"]: g for g in G.get(team, {}).get("games", [])}
    named = {}
    for r in conn.execute("select * from film_items where team=? order by date", (team,)):
        x = named.setdefault((r["label"], r["kind"]), {"name": r["label"], "type": "coverage (defence)" if r["kind"] == "coverage" else
                                                       "offensive system" if r["kind"] == "system" else "offensive play",
                                                       "what_it_is": kb.describe(r["label"])["desc"], "times_reported": 0, "games": []})
        x["times_reported"] += 1
        g = games.get(r["game_id"])
        if g:
            x["games"].append({"date": g["date"], "opponent": g["opp"], "result": "won" if g["result"] == "W" else "lost",
                               "score": f"{g['pts']}-{g['opp_pts']}", "worked": r["impact"] or "not stated", "effect": r["effect"] or ""})
        elif len(x.setdefault("from_season_pieces", [])) < 2:
            x["from_season_pieces"].append(" ".join((r["summary"] or "").split())[:200])
    if not named:
        return None
    off = {r["play"]: r for r in plays.team_plays(S, team, lg, "offense")}
    for x in named.values():                     # how the shot types this play usually ends in have gone, all season
        used = [off[k] for k in kb.SET_TO_PLAYS.get(x["name"], []) if k in off] if x["type"] != "coverage (defence)" else []
        att = sum(u["att"] for u in used)
        if att >= 20:
            x["season_estimate_from_its_usual_shots"] = {
                "made_pct": _pct(sum(u["success"] * u["att"] for u in used) / att),
                "league_made_pct": _pct(sum(u["lg_success"] * u["att"] for u in used) / att), "attempts": att}
    rec = [g["result"] for g in games.values()]
    return {"team": team, "record": f"{rec.count('W')}-{rec.count('L')}", "games_played": len(rec),
            "named_plays_and_coverages": sorted(named.values(), key=lambda x: -x["times_reported"])[:14],
            "how_its_shots_come_this_season": [_play(r) for r in plays.team_plays(S, team, lg, "offense")[:5]],
            "what_opponents_score_on_this_season": [_play(r) for r in plays.team_plays(S, team, lg, "defense")[:4]]}


def playbook_notes(conn, team) -> dict | None:
    r = conn.execute("select body from playbook_notes where team=?", (team,)).fetchone()
    return json.loads(r["body"]) if r else None


def playbooks(conn, season, model, runner=None, limit=MAX_NOTES) -> dict:
    """The playbook analyst's read of each team that has named plays, redone when a new label or a new game arrives.
    Teams whose facts changed longest ago go first. Same number rule as the game breakdowns."""
    every = game_log.all_teams(conn, season)
    G = game_log.build(conn, season, every)
    S = schemes.shots(conn, season)
    lg = plays.league(S)
    have = {r["team"]: (r["hash"], r["created"]) for r in conn.execute("select team, hash, created from playbook_notes")}
    todo = []
    for t in every:
        F = playbook_facts(conn, season, t, G, S, lg)
        if F:
            h = hashlib.sha1(json.dumps([VERSION, F["games_played"], [(x["name"], x["times_reported"], x["games"])
                                                                       for x in F["named_plays_and_coverages"]]]).encode()).hexdigest()[:16]
            if have.get(t, ("", ""))[0] != h:
                todo.append((have.get(t, ("", ""))[1], t, h, F))
    todo.sort(key=lambda x: x[0])                                   # never written first, then the oldest
    out = {"done": 0, "left": max(0, len(todo) - limit), "stop": None}
    for _, t, h, F in todo[:limit]:
        allowed, extra, res = numbers(F), {}, None
        try:
            for _ in range(2):
                res = _ask("breakdown_playbook.md", {**F, **extra}, model, runner, Playbook)
                kept, bad = clean(res.notes[:5] if res else [], allowed)
                if kept or not bad:
                    break
                extra = {"your_last_reply_was_rejected": f"it used numbers that are not in the facts: {', '.join(bad[:8])}. Use only numbers you were given."}
        except Down as e:
            out["stop"] = f"claude unavailable ({e})"
            break
        if not res or not kept:
            continue
        body = {"headline": f"{t} playbook" if invented(res.headline, allowed) else res.headline, "notes": [f.model_dump() for f in kept]}
        with db.tx(conn):
            conn.execute("insert or replace into playbook_notes(team, hash, created, body) values (?,?,?,?)",
                         (t, h, datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(body, ensure_ascii=False)))
        out["done"] += 1
    if out["done"] or out["stop"]:
        log.info("playbook notes: %s", out)
    return out
