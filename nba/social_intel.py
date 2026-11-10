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
BUDGET = {"search": 30, "read": 12, "subtitle": 6, "web_search": 8, "web_read": 10}
# seconds between calls of the same kind. YouTube's caption endpoint 429'd the home IP after ~1 pull per few minutes
# (2026-11-09: 3 tests), so subtitles are spaced 150 s; a nightly pass takes about 15 minutes.
SPACING = {"search": 11, "read": 4, "subtitle": 150, "web_search": 4, "web_read": 4}
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
    "matchup zone": "coverage", "man-to-man": "coverage", "half-court trap": "coverage",
    "scramble": "coverage", "veer switch": "coverage", "backdoor cuts": "action", "lob": "action", "alley-oop": "action",
}
# what writers call the same thing -> one name (2026-11-10: "horn", "back door", "ball screen" showed up as separate plays)
ALIASES = {"horn": "horns", "horns set": "horns", "back door": "backdoor cuts", "backdoor": "backdoor cuts", "backdoor cut": "backdoor cuts",
           "ball screen": "pick-and-roll", "screen-and-roll": "pick-and-roll", "pick and roll": "pick-and-roll", "pnr": "pick-and-roll",
           "spain pnr": "spain pick-and-roll", "spain pick and roll": "spain pick-and-roll",
           "pick and pop": "pick-and-pop", "handoff": "dho", "dribble hand-off": "dho", "dribble handoff": "dho", "iso": "isolation",
           "post up": "post-up", "switching": "switch", "switch everything": "switch", "drop coverage": "drop", "zone defense": "zone",
           "2-3": "2-3 zone", "3-2": "3-2 zone", "1-3-1": "1-3-1 zone", "scrambles": "scramble", "veer switches": "veer switch",
           "peel switches": "peel switch", "full court press": "full-court press", "press": "full-court press", "trap": "blitz",
           "man to man": "man-to-man", "lobs": "lob", "alley oop": "alley-oop"}


NOT_A_PLAY = {"two-man game", "two man game", "ball movement", "motion", "effort", "transition"}   # too vague to name a set


def normalise(label: str, kind: str | None) -> tuple[str, str | None]:
    """One name per play, and the side the name belongs to (a coverage named by the writer is never an offensive set).
    Vague phrases ("two-man game" can be a pick-and-roll, a handoff or an isolation) get no kind, so they are dropped."""
    label = ALIASES.get(label.strip().lower(), label.strip().lower())
    if label in NOT_A_PLAY:
        return label, None
    return label, SETS.get(label) or (kind if kind in ("set", "action", "coverage") else None)
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
        channel, verb = ("web", action[4:]) if action.startswith("web_") else ("youtube", action)
        rc, out = self.runner(["agent-reach", channel, verb, *args])
        try:
            j = json.loads(out)
        except ValueError:
            raise Stop(f"agent-reach gave no JSON (exit {rc})")
        if not j.get("ok"):
            err = j.get("error") or {}
            if err.get("code") in STOP_CODES:
                raise Stop(f"{'Web' if action.startswith('web_') else 'YouTube'} {err.get('code')}: {str(err.get('message'))[:160]}")
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


def extract(text, game, url, title, model, runner=None, prompt="film.md") -> list[dict]:
    """claude -p (cheap model): named sets / coverages only, strict JSON, validated and normalised."""
    words = text.split()
    found = []
    for i in range(0, min(len(words), 18000), 6000):       # <= 3 chunks per video
        chunk = " ".join(words[i:i + 6000])
        user = json.dumps({"game": {"date": game["date"], "home": game["home"], "away": game["away"],
                                    "home_name": espn.NAMES.get(game["home"]), "away_name": espn.NAMES.get(game["away"])},
                           "video_title": title, "transcript_chunk": chunk})
        try:
            raw = analyst.call((analyst.PROMPTS / prompt).read_text(encoding="utf-8"), user, model,
                               **({"runner": runner} if runner else {}))
            data = analyst.extract_json(raw) if "{" in raw else {"items": []}
        except Exception as e:
            log.warning("film extraction failed for %s: %s", url, e)
            continue
        for x in data.get("items", []):
            team = espn.team_code(str(x.get("team", "")))
            label, kind = normalise(str(x.get("label", "")), x.get("kind"))
            if team not in (game["home"], game["away"]) or not label:
                continue
            if not kind:
                continue
            found.append({"team": team, "label": label, "kind": kind, "who": str(x.get("who", ""))[:80], "ato": int(bool(x.get("ato"))),
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
                        _store(conn, g["game_id"], g["date"], it, url, v.get("author") or "")
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
    ato = {}
    for r in rows:
        if r.get("ato"):
            ato[r["label"]] = ato.get(r["label"], 0) + 1
    return {"counts": {k: dict(sorted(v.items(), key=lambda x: -x[1])) for k, v in counts.items()}, "recent": rows[:12],
            "ato": dict(sorted(ato.items(), key=lambda x: -x[1]))}


def contradictions(items: list[dict], labels_by_team: dict) -> list[dict]:
    """Reported coverages that argue against an inferred defense label for the same team."""
    out = []
    for it in items:
        for inferred in CONTRADICTS.get(it["label"], ()):
            if any(l["label"] == inferred for l in labels_by_team.get(it["team"], [])):
                out.append({**it, "inferred": inferred})
    return out

def _store(conn, game_id, d, it, url, channel):
    conn.execute("""insert or ignore into film_items(game_id, date, team, kind, label, who, summary, url, title, channel, created, ato)
                    values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (game_id, d, it["team"], it["kind"], it["label"], it["who"], it["summary"], url, it["title"], channel,
                  datetime.now(timezone.utc).isoformat(timespec="seconds"), it.get("ato", 0)))


def season_articles(conn, today: str, model: str, reach=None, claude_runner=None, per_night=6, teams=None) -> dict:
    """Written breakdowns of a team's offense / defense this season (Exa search + Jina read via Agent Reach).
    Rotates through the league, `per_night` teams a night, so every team is refreshed about every five days."""
    reach = reach or Reach()
    explicit, teams = teams, sorted(espn.TEAM_IDS)
    start = (date.fromisoformat(today).toordinal() * per_night) % len(teams)
    pick = [teams[(start + i) % len(teams)] for i in range(per_night)] if not explicit else list(explicit)
    added, stop = [], None
    try:
        for t in pick:
            name = espn.NAMES[t]
            items = reach("web_search", f"{name} 2026-27 offense sets, defensive scheme, zone or drop coverage, film breakdown", "--limit", "6") or []
            used = 0
            for it in items:
                url = it.get("url") or ""
                if not url or used >= 2 or conn.execute("select 1 from film_videos where url=?", (url,)).fetchone():
                    continue
                pub = _date(it.get("published_at"))
                if pub and pub < date(2026, 9, 1):          # older than this season's camp: skip
                    _video(conn, url, "", "skipped: old article")
                    continue
                if any(x in url for x in ("youtube.com", "youtu.be", "reddit.com", "x.com", "twitter.com")):
                    continue
                got = reach("web_read", url) or []
                text = (got[0].get("text") if got else "") or ""
                used += 1
                if len(text.split()) < 200:
                    _video(conn, url, "", "too short")
                    continue
                game = {"date": today, "home": t, "away": t}
                found = [f for f in extract(text, game, url, it.get("title") or "", model, claude_runner, prompt="film_season.md")
                         if f["team"] == t]
                with db.tx(conn):
                    for f in found:
                        _store(conn, "", (pub or date.fromisoformat(today)).isoformat(), f, url, _host(url))
                _video(conn, url, "", f"done: {len(found)} labels")
                added += found
    except Stop as e:
        stop = str(e)
    return {"teams": pick, "added": added, "stop": stop}


NOT_ARTICLE = ("youtube.com", "youtu.be", "reddit.com", "x.com", "twitter.com", "basketball-reference.com", "48index.com")


def game_articles(conn, today: str, model: str, teams, season: int, reach=None, claude_runner=None, per_night=5) -> dict:
    """Write-ups of each finished game of the focus teams (Exa search + Jina read via Agent Reach): named sets and
    coverages tied to that game. Newest games first, `per_night` a night (<= 2 reads each, inside the read budget);
    each (game, team) is searched once, marked `art:<game_id>:<team>` in film_games."""
    reach = reach or Reach()
    done = {r[0] for r in conn.execute("select game_id from film_games where game_id like 'art:%'")}
    todo = sorted(((dict(g), t) for t in teams for g in conn.execute(
        "select * from games where season=? and status='final' and source is not null and date < ? and ? in (home, away)",
        (season, today, t)) if f"art:{g['game_id']}:{t}" not in done), key=lambda x: x[0]["date"], reverse=True)
    added, stop = [], None
    try:
        for g, t in todo[:per_night]:
            opp, gd = g["away"] if t == g["home"] else g["home"], date.fromisoformat(g["date"])
            items = reach("web_search", f"{espn.NAMES[t]} vs {espn.NAMES[opp]} {gd:%B} {gd.day}, {gd.year} film breakdown sets defense",
                          "--limit", "6") or []
            used = 0
            for it in items:
                url, title = it.get("url") or "", it.get("title") or ""
                pub = _date(it.get("published_at"))
                if (used >= 2 or not url or any(x in url for x in NOT_ARTICLE) or not pub or not gd <= pub <= gd + timedelta(days=4)
                        or not _mentions(title, t) or conn.execute("select 1 from film_videos where url=?", (url,)).fetchone()):
                    continue
                got = reach("web_read", url) or []
                text = (got[0].get("text") if got else "") or ""
                used += 1
                if len(text.split()) < 200:
                    _video(conn, url, g["game_id"], "too short")
                    continue
                found = extract(text, g, url, title, model, claude_runner)
                with db.tx(conn):
                    for f in found:
                        _store(conn, g["game_id"], g["date"], f, url, _host(url))
                _video(conn, url, g["game_id"], f"done: {len(found)} labels (article)")
                added += [{**f, "game_id": g["game_id"]} for f in found]
            with db.LOCK:
                conn.execute("insert or replace into film_games(game_id, status, checked) values (?, 'done', ?)", (f"art:{g['game_id']}:{t}", today))
    except Stop as e:
        stop = str(e)
    return {"games": min(per_night, len(todo)), "left": max(0, len(todo) - per_night), "added": added, "stop": stop}


def _host(url):
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else url



# ---------- explainer videos: what each play / coverage looks like ----------

EXPLAINER_CHANNELS = ("Basketball Immersion", "BBALLBREAKDOWN", "Hooper University", "NBArtv", "The Film Room", "Coach Daniel",
                      "Thinking Basketball", "Basketball Orbit", "Buckets Basketball", "The Princeton Coach", "Half Court Hoops")
_SYN = {"pick-and-roll": "pick roll", "pick-and-pop": "pick pop", "dho": "handoff", "dribble handoff": "handoff", "5-out": "5 out",
        "2-3 zone": "2-3 zone", "box-and-one": "box one", "rip dho": "handoff", "zoom action": "zoom"}


def _tokens(label):
    base = _SYN.get(label, label)
    for word in ("-and-", "-"):
        base = base.replace(word, " ")
    return [w for w in base.lower().split() if len(w) > 1 or w.isdigit()]


NOT_BASKETBALL = re.compile(r"football|madden|soccer|fifa|hockey|nhl|nfl|baseball|volleyball|2k\d|nba 2k|fortnite|valorant|stick and user", re.I)
NEEDS_CONTEXT = {   # short or generic names: the title must also show it is the basketball play
    "switch": ("defens", "switching", "screen", "pick"), "peja": ("action", "set", "play", "screen", "handoff"),
    "lob": ("lob play", "lob set", "ato", "out of bounds", "alley", "lob action"), "chicago": ("action", "handoff", "pin down", "set play"), "wide": ("screen", "action", "set"), "delay": ("offense", "set", "action"),
    "drop": ("coverage", "defens", "pick"), "ice": ("coverage", "defens", "pick", "screen"), "blitz": ("pick", "trap", "defens"),
    "hedge": ("coverage", "defens", "pick", "screen"), "zone": ("defens", "basketball", "offense"), "flare": ("screen", "set", "play"),
}
MORE_SPECIFIC = ("spain", "empty side", "inverted", "drag", "horns", "short roll")


def pick_explainer(items, label, kind):
    toks = _tokens(label)
    best, best_score = None, 0
    for it in items or []:
        title = (it.get("title") or "").lower().replace("&", "and").replace("-", " ")
        raw = it.get("raw") if isinstance(it.get("raw"), dict) else {}
        dur = raw.get("duration") or 0
        if not all(t in title for t in toks) or (dur and not 45 <= dur <= 1000) or NOT_BASKETBALL.search(title):
            continue
        if label in NEEDS_CONTEXT and not any(w in title for w in NEEDS_CONTEXT[label]):
            continue
        if any(w in title and w not in label for w in MORE_SPECIFIC):
            continue
        score = 3 + 2 * ((it.get("author") or "") in EXPLAINER_CHANNELS)
        score += any(w in title for w in ("explained", "breakdown", "basics", "playbook", "how to"))
        score -= kind == "coverage" and "offense" in title and "defense" not in title
        if score > best_score:
            best, best_score = it, score
    return best


def play_videos(conn, labels, reach=None, limit=10) -> dict:
    """Find and cache one explainer per label (YouTube search only, no subtitles). Returns {label: status}."""
    from . import playbook_kb as kb
    reach = reach or Reach()
    done = {r[0] for r in conn.execute("select label from play_videos")}
    out = {}
    try:
        for label in [l for l in labels if l not in done][:limit]:
            kind = kb.PLAYS.get(label, (None,))[0] or "action"
            q = f"{label} {'pick and roll defense coverage' if kind == 'coverage' else 'basketball'} {'action explained' if label in NEEDS_CONTEXT else 'explained'}"
            it = pick_explainer(reach("search", q, "--limit", "8"), label, kind)
            raw = (it or {}).get("raw") if isinstance((it or {}).get("raw"), dict) else {}
            with db.LOCK:
                conn.execute("insert or replace into play_videos(label, url, title, channel, duration, checked) values (?,?,?,?,?,?)",
                             (label, (it or {}).get("url"), (it or {}).get("title"), (it or {}).get("author"), raw.get("duration"),
                              datetime.now(timezone.utc).isoformat(timespec="seconds")))
            out[label] = "found" if it else "none"
    except Stop as e:
        out["_stop"] = str(e)
    return out


def video_map(conn) -> dict:
    return {r["label"]: {"url": r["url"], "title": r["title"], "channel": r["channel"], "duration": r["duration"]}
            for r in conn.execute("select * from play_videos where url is not null")}


# ---------- team film: YouTube breakdowns of a team's sets this season (search + description, no subtitles) ----------

TEAM_FILM = """
create table if not exists team_videos(team text, url text, title text, channel text, published text, labels int,
  primary key(team, url));
"""


def season_videos(conn, today: str, model: str, reach=None, claude_runner=None, teams=None, per_team=2) -> dict:  # 12 reads = 6 teams
    """For each team: find this season's breakdown / playbook videos, keep them as 'film to watch', and extract any
    named sets from the title + description. Never pulls subtitles (those are rate-limited from the home IP)."""
    with db.LOCK:
        conn.executescript(TEAM_FILM)
    reach = reach or Reach()
    teams = list(teams or [])
    added, stop, found = [], None, 0
    try:
        for t in teams:
            nick = _nick(t)
            items = []
            for q in (f"{espn.NAMES[t]} offense sets playbook film breakdown 2026", f"{espn.NAMES[t]} defense zone coverage scheme breakdown 2026"):
                items += reach("search", q, "--limit", "5") or []
            keep = [it for it in items if _mentions(it.get("title") or "", t) and not SKIP_TITLE.search(it.get("title") or "")]
            used = 0
            for it in keep:
                url = it.get("url") or ""
                if used >= per_team or not url or conn.execute("select 1 from team_videos where team=? and url=?", (t, url)).fetchone():
                    continue
                meta = (reach("read", url) or [{}])[0]
                pub = _date(meta.get("published_at"))
                used += 1
                if not pub or pub < date(2026, 9, 1):
                    _video(conn, url, "", "skipped: not this season")
                    continue
                text = f"{it.get('title', '')}. {meta.get('text') or ''}"
                labels = [f for f in extract(text, {"date": today, "home": t, "away": t}, url, it.get("title") or "", model,
                                             claude_runner, prompt="film_season.md") if f["team"] == t]
                with db.tx(conn):
                    conn.execute("insert or replace into team_videos(team, url, title, channel, published, labels) values (?,?,?,?,?,?)",
                                 (t, url, (it.get("title") or "")[:150], it.get("author") or "", pub.isoformat(), len(labels)))
                    for f in labels:
                        _store(conn, "", pub.isoformat(), f, url, it.get("author") or "")
                _video(conn, url, "", f"done: {len(labels)} labels (description)")
                found += 1
                added += labels
    except Stop as e:
        stop = str(e)
    return {"teams": teams, "videos": found, "added": added, "stop": stop}


def team_film(conn, team: str, limit=6) -> list[dict]:
    with db.LOCK:
        conn.executescript(TEAM_FILM)
    return [dict(r) for r in conn.execute("select * from team_videos where team=? order by published desc limit ?", (team, limit))]
