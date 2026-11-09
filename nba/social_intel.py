"""Film room: named sets and coverages reported in YouTube breakdowns and press conferences (spec "Film Room").

Every Agent Reach call goes through `reach()` so the CLI syntax lives here only. Read-only, low volume:
budgets per run, spacing between calls, and the first RATE_LIMITED / PLATFORM_BLOCKED / NEED_LOGIN stops the whole
pass for the day (no retries, no other backend). Stored: labels, a <= 2 sentence paraphrase and the link. Never the
transcript. The daily digest never waits on this module.
"""
import json
import logging
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

from . import analyst, db, espn

log = logging.getLogger(__name__)
BUDGET = {"search": 12, "read": 12, "subtitle": 6}
# seconds between calls of the same kind. YouTube's caption endpoint 429'd the home IP after ~1 pull per few minutes
# (2026-11-09: 3 tests), so subtitles are spaced 150 s; a nightly pass takes about 15 minutes.
SPACING = {"search": 11, "read": 4, "subtitle": 150}
STOP_CODES = {"RATE_LIMITED", "PLATFORM_BLOCKED", "NEED_LOGIN", "BUSY"}
PREFERRED = ("BBALLBREAKDOWN", "Thinking Basketball", "Blind Pig Basketball", "Coach Mike", "Swish Cultures",
             "Half Court Hoops", "Basketball Immersion", "Coach Pat", "The Athletic NBA", "Hoop Vision")
SKIP_TITLE = re.compile(r"highlights|full game|reaction|reacts|mixtape|top \d+ plays|fight|ejected|trade|rumou?r", re.I)
PRESSER = re.compile(r"post\s*-?\s*game|press conference|postgame|availability", re.I)
SETS = {   # normalised label -> kind; anything else Claude returns is kept as "other" only if it names a set/coverage
    "horns": "set", "spain pick-and-roll": "set", "floppy": "set", "chicago": "set", "zipper": "set", "iverson": "set",
    "elbow": "set", "double drag": "set", "77": "set", "ram": "set", "delay": "set", "pistol": "set", "flare": "set",
    "stagger": "set", "hammer": "set", "ghost screen": "action", "empty corner": "set", "5-out": "set", "dho": "action",
    "pick-and-roll": "action", "isolation": "action", "post-up": "action", "split action": "action", "pin-down": "action",
    "back screen": "action", "drop": "coverage", "ice": "coverage", "blitz": "coverage", "hedge": "coverage",
    "show": "coverage", "switch": "coverage", "zone": "coverage", "2-3 zone": "coverage", "3-2 zone": "coverage",
    "1-3-1 zone": "coverage", "box-and-one": "coverage", "triangle-and-two": "coverage", "at the level": "coverage",
    "scram switch": "coverage", "tag": "coverage", "peel switch": "coverage", "full-court press": "coverage",
}
CONTRADICTS = {   # reported coverage vs our inferred defense label that it argues against
    "switch": ("Concedes the mid-range (drop-style)",),
    "blitz": ("Concedes the mid-range (drop-style)",),
    "drop": ("Turnover-forcing (aggressive, traps or blitzes likely)",),
}


class Stop(Exception):
    pass


class Reach:
    """Budgeted, spaced Agent Reach CLI wrapper. runner(cmd) -> (returncode, stdout) for tests."""
    def __init__(self, runner=None, sleep=time.sleep):
        self.runner = runner or _run
        self.sleep = sleep
        self.used = {k: 0 for k in BUDGET}
        self.last = {k: 0.0 for k in BUDGET}

    def __call__(self, action, *args):
        if self.used[action] >= BUDGET[action]:
            raise Stop(f"{action} budget used")
        wait = self.last[action] + SPACING[action] - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self.used[action] += 1
        self.last[action] = time.monotonic()
        rc, out = self.runner(["agent-reach", "youtube", action, *args])
        try:
            j = json.loads(out)
        except ValueError:
            raise Stop(f"agent-reach gave no JSON (exit {rc})")
        if not j.get("ok"):
            err = j.get("error") or {}
            if err.get("code") in STOP_CODES:
                raise Stop(f"YouTube {err.get('code')}: {str(err.get('message'))[:160]}")
            return None
        return j.get("items") or []


def _run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, encoding="utf-8")
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, json.dumps({"ok": False, "error": {"code": "TIMEOUT", "message": str(e)}})


def _nick(team):
    return espn.NAMES.get(team, team).split()[-1]       # "Trail Blazers" -> "Blazers" is fine for search


def _mentions(title, team):
    t = title.lower()
    full = espn.NAMES.get(team, team).lower()
    return _nick(team).lower() in t or full.split()[0] in t or team.lower() in t.split()


def candidates(items, game, limit=3):
    """Keep videos about this matchup: mentions a team, not a highlight reel; pressers and preferred channels first."""
    out = []
    for it in items or []:
        title, author = it.get("title") or "", it.get("author") or ""
        if SKIP_TITLE.search(title) or not (_mentions(title, game["home"]) or _mentions(title, game["away"])):
            continue
        score = 2 * bool(PRESSER.search(title)) + 3 * (author in PREFERRED) + (_mentions(title, game["home"]) and _mentions(title, game["away"]))
        out.append((score, it))
    return [it for _, it in sorted(out, key=lambda x: -x[0])][:limit]


def _date(s):
    if not s:
        return None
    s = str(s)
    try:
        return date.fromisoformat(s[:10]) if "-" in s[:10] else datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None


def extract(text, game, url, title, model, runner=None) -> list[dict]:
    """claude -p (cheap model): named sets / coverages only, strict JSON, validated and normalised."""
    words = text.split()
    found = []
    for i in range(0, min(len(words), 18000), 6000):       # <= 3 chunks per video
        chunk = " ".join(words[i:i + 6000])
        user = json.dumps({"game": {"date": game["date"], "home": game["home"], "away": game["away"],
                                    "home_name": espn.NAMES.get(game["home"]), "away_name": espn.NAMES.get(game["away"])},
                           "video_title": title, "transcript_chunk": chunk})
        try:
            raw = analyst.call((analyst.PROMPTS / "film.md").read_text(encoding="utf-8"), user, model,
                               **({"runner": runner} if runner else {}))
            data = analyst.extract_json(raw) if "{" in raw else {"items": []}
        except Exception as e:
            log.warning("film extraction failed for %s: %s", url, e)
            continue
        for x in data.get("items", []):
            team = espn.team_code(str(x.get("team", "")))
            label = str(x.get("label", "")).strip().lower()
            if team not in (game["home"], game["away"]) or not label:
                continue
            kind = SETS.get(label) or (x.get("kind") if x.get("kind") in ("set", "action", "coverage") else None)
            if not kind:
                continue
            found.append({"team": team, "label": label, "kind": kind, "who": str(x.get("who", ""))[:80],
                          "summary": " ".join(str(x.get("summary", "")).split())[:300], "url": url, "title": title[:150]})
    seen, out = set(), []
    for f in found:
        k = (f["team"], f["label"])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def run(conn, today: str, model: str, reach=None, claude_runner=None, days=3) -> dict:
    """One film-room pass over games from the last `days` US dates. Returns counts and the stop reason, if any."""
    reach = reach or Reach()
    since = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    games = [dict(g) for g in conn.execute("""
        select g.*, coalesce(e.score, 0) ex from games g left join game_excitement e on e.game_id = g.game_id
        where g.status='final' and g.source is not null and g.date >= ? and g.date < ?
          and g.game_id not in (select game_id from film_games where status='done')
        order by ex desc""", (since, today))]
    added, stop = [], None
    try:
        for g in games:
            vids = []
            for q in (f"{_nick(g['away'])} {_nick(g['home'])} postgame press conference", f"{_nick(g['away'])} vs {_nick(g['home'])} breakdown"):
                vids += candidates(reach("search", q, "--limit", "8"), g)
            seen = set()
            for v in vids:
                url = v.get("url")
                if not url or url in seen or conn.execute("select 1 from film_videos where url=?", (url,)).fetchone():
                    continue
                seen.add(url)
                meta = reach("read", url)
                pub = _date((meta or [{}])[0].get("published_at")) if meta else None
                gd = date.fromisoformat(g["date"])
                if not pub or not (gd <= pub <= gd + timedelta(days=3)):
                    _video(conn, url, g["game_id"], "skipped: wrong date")
                    continue
                subs = reach("subtitle", url, "--lang", "en,en-orig")
                text = (subs or [{}])[0].get("text") if subs else None
                if not text:
                    _video(conn, url, g["game_id"], "no subtitles")
                    continue
                items = extract(text, g, url, v.get("title") or "", model, claude_runner)
                with db.tx(conn):
                    for it in items:
                        conn.execute("""insert or ignore into film_items(game_id, date, team, kind, label, who, summary, url, title, channel, created)
                                        values (?,?,?,?,?,?,?,?,?,?,?)""",
                                     (g["game_id"], g["date"], it["team"], it["kind"], it["label"], it["who"], it["summary"],
                                      url, it["title"], v.get("author") or "", datetime.now(timezone.utc).isoformat(timespec="seconds")))
                _video(conn, url, g["game_id"], f"done: {len(items)} labels")
                added += [{**it, "game_id": g["game_id"]} for it in items]
            with db.LOCK:
                conn.execute("insert or replace into film_games(game_id, status, checked) values (?, 'done', ?)", (g["game_id"], today))
    except Stop as e:
        stop = str(e)
        log.info("film room stopped: %s", stop)
    return {"games": len(games), "added": added, "stop": stop, "used": dict(reach.used)}


def _video(conn, url, game_id, status):
    with db.LOCK:
        conn.execute("insert or replace into film_videos(url, game_id, status, checked) values (?,?,?,?)",
                     (url, game_id, status, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def playbook(conn, team: str, season_since: str | None = None) -> dict:
    """Season counts of reported sets / coverages for a team + latest items with links."""
    q = "select * from film_items where team=?" + (" and date >= ?" if season_since else "")
    rows = [dict(r) for r in conn.execute(q + " order by date desc, id desc", (team,) + ((season_since,) if season_since else ()))]
    counts = {}
    for r in rows:
        counts.setdefault(r["kind"], {}).setdefault(r["label"], 0)
        counts[r["kind"]][r["label"]] += 1
    return {"counts": {k: dict(sorted(v.items(), key=lambda x: -x[1])) for k, v in counts.items()}, "recent": rows[:12]}


def contradictions(items: list[dict], labels_by_team: dict) -> list[dict]:
    """Reported coverages that argue against an inferred defense label for the same team."""
    out = []
    for it in items:
        for inferred in CONTRADICTS.get(it["label"], ()):
            if any(l["label"] == inferred for l in labels_by_team.get(it["team"], [])):
                out.append({**it, "inferred": inferred})
    return out
