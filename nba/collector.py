"""Collect finished games (ESPN first, hoopR when ESPN fails), idempotently, and keep the schedule fresh."""
import json
import logging
from dataclasses import dataclass, field

from . import db
from . import espn
from . import excitement as ex
from . import fallback

log = logging.getLogger(__name__)


@dataclass
class CollectResult:
    new_games: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    pending: list = field(default_factory=list)      # final on the scoreboard, but no box yet


def _stored_final(conn, game_id):
    return conn.execute("select 1 from team_box where game_id=?", (game_id,)).fetchone() is not None


def upsert_schedule_row(conn, g):
    """Insert/refresh a schedule row without clobbering a stored final."""
    with db.LOCK:
        cur = conn.execute("select status, source from games where game_id=?", (g["game_id"],)).fetchone()
        if cur and cur["source"]:            # already has a box score: never overwrite it from a schedule feed
            return
        db.upsert(conn, "games", {**g, "source": None})


def store_game(conn, parsed, source):
    g, plays = parsed["game"], parsed["plays"]
    teams = [dict(t) for t in parsed["team_box"]]
    split = ex.clean_split(plays, g["ot"]) if plays else None
    for t in teams:
        if split:
            hp, ap, frac = split
            t["clean_poss"], t["clean_pts"], t["clean_opp_pts"] = t["poss"] * frac, *((hp, ap) if t["is_home"] else (ap, hp))
        else:
            t["clean_poss"], t["clean_pts"], t["clean_opp_pts"] = t["poss"], t["pts"], g["home_pts"] + g["away_pts"] - t["pts"]
    players = [p for p in parsed["player_box"]]
    with db.tx(conn):
        if not g.get("note"):                # summaries and hoopR carry no note: keep the schedule's (NBA Cup tag)
            old = conn.execute("select note from games where game_id=?", (g["game_id"],)).fetchone()
            g = {**g, "note": old["note"] if old else None}
        db.upsert(conn, "games", {**g, "source": source})
        for t in teams:
            db.upsert(conn, "team_box", t)
        for p in players:
            db.upsert(conn, "player_box", p)
        if plays:
            cols = list(plays[0])
            conn.executemany(f"insert or replace into plays({','.join(cols)}) values ({','.join('?' * len(cols))})",
                             [list(p.values()) for p in plays])
            best = max((ex.game_score(p) for p in players if not p["dnp"]), default=0)
            lc = max((t.get("lead_changes") or 0 for t in teams), default=0)
            score, stars, reasons = ex.score(plays, g["ot"], best, lc)
            db.upsert(conn, "game_excitement", {"game_id": g["game_id"], "score": score, "stars": stars,
                                                "reasons": ",".join(reasons)})
            team_of = {p["player_id"]: p["team"] for p in players}
            for k, v in ex.wpa(plays, g["home"], g["away"]).items():
                db.upsert(conn, "player_wpa", {"game_id": g["game_id"], "key": k,
                                               "team": k[5:] if k.startswith("TEAM:") else team_of.get(k), "wpa": v})
        for name in parsed.get("officials") or []:
            conn.execute("insert or ignore into game_officials(game_id, name) values (?,?)", (g["game_id"], name))
        if parsed.get("odds", {}).get("home_ml") is not None:
            db.meta_set(conn, f"odds:{g['game_id']}", json.dumps(parsed["odds"]))


def collect_date(conn, client, d: str, season: int, hoopr=fallback) -> CollectResult:
    """d = US (ET) date 'YYYY-MM-DD'."""
    res = CollectResult()
    try:
        games = espn.parse_scoreboard(client.scoreboard(d.replace("-", "")))
    except Exception as e:                     # client already retried 3x: switch source for this date
        log.warning("ESPN scoreboard failed for %s: %s", d, e)
        return _from_hoopr(conn, d, season, hoopr, res, f"ESPN scoreboard failed ({type(e).__name__}); used hoopR for {d}")
    espn_failed = []
    for g in games:
        if g["status"] != "final":
            upsert_schedule_row(conn, g)
            continue
        if _stored_final(conn, g["game_id"]):
            continue
        try:
            store_game(conn, espn.parse_summary(client.summary(g["game_id"])), "espn")
            res.new_games.append(g["game_id"])
        except Exception as e:
            log.warning("ESPN summary failed for %s: %s", g["game_id"], e)
            espn_failed.append(g["game_id"])
    if espn_failed:
        _from_hoopr(conn, d, season, hoopr, res, f"ESPN box score failed for {len(espn_failed)} game(s); used hoopR",
                    only=set(espn_failed))
    return res


def _from_hoopr(conn, d, season, hoopr, res, warning, only=None):
    try:
        rows = hoopr.games_for_date(season, d)
    except Exception as e:
        rows = []
        log.warning("hoopR failed for %s: %s", d, e)
    got = set()
    for parsed in rows:
        gid = parsed["game"]["game_id"]
        if (only and gid not in only) or _stored_final(conn, gid):
            continue
        store_game(conn, parsed, "hoopr")
        res.new_games.append(gid)
        got.add(gid)
    missing = (only or set()) - got
    res.pending.extend(sorted(missing))
    res.warnings.append(warning + (f" ({len(missing)} still pending, hoopR updates ~22:15 SGT)" if missing or not rows else ""))
    return res


def refresh_schedule(conn, client, season: int, seasontype=None) -> int:
    n = 0
    for team in espn.TEAM_IDS:
        try:
            for g in espn.parse_team_schedule(client.team_schedule(team, season, seasontype)):
                upsert_schedule_row(conn, g)
                n += 1
        except Exception as e:
            log.warning("schedule %s failed: %s", team, e)
    return n


def missing_dates(conn, today: str) -> list[str]:
    rows = conn.execute("""select distinct g.date from games g left join team_box t on t.game_id = g.game_id
                           where g.date < ? and t.game_id is null and g.status not in ('postponed','canceled')
                           order by g.date""", (today,)).fetchall()
    return [r[0] for r in rows]


def backfill(conn, client, season: int, today: str) -> CollectResult:
    total = CollectResult()
    for d in missing_dates(conn, today):
        r = collect_date(conn, client, d, season)
        total.new_games += r.new_games
        total.warnings += r.warnings
        total.pending += r.pending
    return total


def backfill_officials(conn, season: int) -> int:
    """Officials for stored games that have none, from hoopR (ESPN summaries carry them for new games)."""
    import pandas as pd
    missing = {r[0] for r in conn.execute("""select g.game_id from games g where g.season=? and g.source is not null
                                             and g.game_id not in (select game_id from game_officials)""", (season,))}
    if not missing:
        return 0
    d = pd.read_parquet(f"{fallback.BASE}/officials/parquet/officials_{season}.parquet")
    n = 0
    with db.tx(conn):
        for r in d.itertuples():
            gid = str(r.game_id)
            if gid in missing and r.official_display_name:
                conn.execute("insert or ignore into game_officials(game_id, name) values (?,?)", (gid, r.official_display_name))
                n += 1
    return n
