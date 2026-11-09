"""News desk: player and team news that can change tonight's games.

Sources (all public, no login): ESPN injury blurbs (already fetched every 30 min), the ESPN news feed, the CBS Sports
NBA RSS feed, and one Agent Reach web search per team playing tonight, about 2-3 hours before its tip. Rotowire is not
used (its robots.txt disallows crawlers). A cheap Claude call tags each new item and writes a short paraphrase; titles
are kept with a link, article text is never stored. News never moves the model: only the official injury feed does.
News that names a player as out / doubtful before the injury report does is flagged "ahead of the report".
"""
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from . import analyst, db, espn

log = logging.getLogger(__name__)
UA = {"User-Agent": "nba-tracker/1.0 (personal, low volume)"}
ESPN_NEWS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news"
CBS_RSS = "https://www.cbssports.com/rss/headlines/nba/"
KINDS = ("injury", "rest", "return", "trade", "suspension", "lineup", "coaching", "personal", "other")
PUSH_KINDS = {"injury", "rest", "return", "suspension", "lineup"}

SCHEMA = """
create table if not exists news_items(
  id integer primary key autoincrement, key text unique, ts text, published text, source text, title text,
  summary text, url text, teams text, players text, kind text, status_implied text, tonight int default 0,
  impact real, ahead int default 0, kept int default 0, pushed int default 0);
"""


def _key(*parts):
    return hashlib.sha1("|".join(str(p or "").strip().lower() for p in parts).encode()).hexdigest()[:20]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SPOILER = re.compile(r"\b\d{2,3}\s*-\s*\d{2,3}\b|\bwins? over\b|\bbeats?\b|\bvictory\b|\bdefeat(?:ed|s)?\b|\blost to\b|\bwon\b|\bscored \d+", re.I)


def spoiler_safe(summary: str, kind: str, players: list[str]) -> str:
    """News sits outside spoiler tags, so a summary that mentions a result is replaced with a neutral line."""
    if summary and not SPOILER.search(summary):
        return summary
    who = players[0] if players else "Team"
    return f"{who}: {kind} update (summary hidden: it mentioned a game result)."


class NoNet:
    """Stand-in HTTP client for tests: every outside request fails (and is logged), nothing leaves the machine."""
    def get(self, *a, **k):
        raise RuntimeError("network disabled")


# ---------- fetch ----------

def from_injuries(injury_json) -> list[dict]:
    out = []
    for t in injury_json.get("injuries", []):
        for i in t.get("injuries", []):
            a = i.get("athlete", {})
            team = (a.get("team") or {}).get("abbreviation") or espn.ID_TEAM.get(str(t.get("id")))
            text = i.get("longComment") or i.get("shortComment")
            if not text:
                continue
            href = next((l.get("href") for l in a.get("links", []) if l.get("href")), "")
            out.append({"key": _key("inj", a.get("displayName"), i.get("date"), text[:80]), "source": "ESPN injury report",
                        "published": i.get("date"), "title": f"{a.get('displayName')} ({team}): {i.get('status')}",
                        "text": text[:900], "url": href, "hint_team": team, "hint_player": a.get("displayName")})
    return out


def from_espn_news(j) -> list[dict]:
    out = []
    for a in j.get("articles", []):
        if (a.get("type") or "") in ("Recap",):          # recaps spoil results and look backwards
            continue
        teams = [espn.team_code(c.get("description", "")) for c in a.get("categories", []) if c.get("type") == "team"]
        out.append({"key": _key("espn", a.get("headline")), "source": "ESPN", "published": a.get("published"),
                    "title": a.get("headline", ""), "text": (a.get("description") or "")[:600],
                    "url": ((a.get("links") or {}).get("web") or {}).get("href", ""), "hint_team": ",".join(t for t in teams if t)})
    return out


def from_rss(xml_text, source="CBS Sports") -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
        out.append({"key": _key("rss", title), "source": source, "published": it.findtext("pubDate"), "title": title,
                    "text": desc[:600], "url": (it.findtext("link") or "").strip(), "hint_team": ""})
    return out


def fetch_all(injury_json=None, http=None) -> list[dict]:
    http = http or httpx.Client(timeout=20, headers=UA, follow_redirects=True)
    items = []
    if injury_json:
        items += from_injuries(injury_json)
    for name, fn in (("espn", lambda: from_espn_news(http.get(ESPN_NEWS, params={"limit": 50}).json())),
                     ("cbs", lambda: from_rss(http.get(CBS_RSS).text))):
        try:
            items += fn()
        except Exception as e:
            log.warning("news source %s failed: %s", name, e)
    return items


def pretip_search(conn, reach, teams_tonight: list[str], today: str) -> list[dict]:
    """One web search per team playing tonight (once a day each), via Agent Reach."""
    out = []
    for t in teams_tonight:
        flag = f"news_search:{today}:{t}"
        if db.meta_get(conn, flag):
            continue
        try:
            res = reach("web_search", f"{espn.NAMES[t]} injury news today", "--limit", "5") or []
        except Exception as e:
            log.info("pre-tip search stopped: %s", e)
            break
        db.meta_set(conn, flag, "1")
        for r in res:
            out.append({"key": _key("web", r.get("url") or r.get("title")), "source": "web search", "published": r.get("published_at"),
                        "title": r.get("title") or "", "text": (r.get("text") or "")[:600], "url": r.get("url") or "", "hint_team": t})
    return out


# ---------- classify ----------

def classify(items: list[dict], model: str, runner=None) -> list[dict]:
    """Claude tags a batch; returns one dict per input index (missing / invalid -> dropped)."""
    if not items:
        return []
    prompt = (analyst.PROMPTS / "news.md").read_text(encoding="utf-8")
    user = json.dumps({"today_utc": _now()[:10], "items": [{"i": k, "source": x["source"], "title": x["title"], "text": x["text"],
                                                              "team_hint": x.get("hint_team", ""), "player_hint": x.get("hint_player", "")}
                                                             for k, x in enumerate(items)]})
    try:
        raw = analyst.call(prompt, user, model, **({"runner": runner} if runner else {}))
        data = analyst.extract_json(raw)
    except Exception as e:
        log.warning("news classification failed: %s", e)
        return []
    out = []
    for r in data.get("items", []):
        try:
            k = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= k < len(items):
            continue
        out.append({**items[k], "keep": bool(r.get("keep")), "kind": r.get("kind") if r.get("kind") in KINDS else "other",
                    "teams": [t for t in (espn.team_code(x) for x in r.get("teams", [])) if t],
                    "players": [str(p)[:60] for p in r.get("players", [])][:4],
                    "status_implied": r.get("status_implied") if r.get("status_implied") in ("out", "doubtful", "questionable", "available") else None,
                    "summary": " ".join(str(r.get("summary", "")).split())[:220]})
    return out


# ---------- collect + store ----------

def collect(conn, model, injury_json=None, reach=None, teams_tonight=(), today=None, values=None, injuries=None,
            runner=None, http=None) -> list[dict]:
    """Fetch, drop already-seen, classify the new ones, store. Returns the newly stored items that were kept."""
    with db.LOCK:
        conn.executescript(SCHEMA)
    items = fetch_all(injury_json, http)
    if reach and teams_tonight:
        items += pretip_search(conn, reach, list(teams_tonight), today or _now()[:10])
    seen = {r[0] for r in conn.execute("select key from news_items")}
    fresh, uniq = [], set()
    for x in items:
        if x["key"] not in seen and x["key"] not in uniq and x["title"]:
            uniq.add(x["key"])
            fresh.append(x)
    if not fresh:
        return []
    tagged = []
    for i in range(0, len(fresh), 20):
        tagged += classify(fresh[i:i + 20], model, runner)
    tagged_keys = {t["key"] for t in tagged}
    values, injuries = values or {}, injuries or {}
    by_name = {v["name"].lower(): (pid, v) for pid, v in values.items()}
    kept = []
    with db.tx(conn):
        for x in fresh:
            if x["key"] not in tagged_keys:
                conn.execute("insert or ignore into news_items(key, ts, source, title, url, kept) values (?,?,?,?,?,0)",
                             (x["key"], _now(), x["source"], x["title"][:200], x["url"]))
        for t in tagged:
            impact, ahead = None, 0
            hit = by_name.get(t["players"][0].lower()) if t["players"] else None     # the status is about the subject only
            if hit:
                pid, v = hit
                impact = v["value"]
                cur = (injuries.get(pid) or {}).get("status", "available")
                if t["status_implied"] in ("out", "doubtful") and cur not in ("out", "doubtful", "suspended"):
                    ahead = 1
            t["summary"] = spoiler_safe(t["summary"], t["kind"], t["players"])
            tonight = int(bool(set(t["teams"]) & set(teams_tonight)))
            conn.execute("""insert or ignore into news_items(key, ts, published, source, title, summary, url, teams, players, kind,
                            status_implied, tonight, impact, ahead, kept) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (t["key"], _now(), t.get("published"), t["source"], t["title"][:200], t["summary"], t["url"],
                          json.dumps(t["teams"]), json.dumps(t["players"]), t["kind"], t["status_implied"], tonight,
                          impact, ahead, int(t["keep"])))
            if t["keep"]:
                kept.append({**t, "tonight": tonight, "impact": impact, "ahead": ahead})
    return kept


def recent(conn, hours=48, team=None, limit=40) -> list[dict]:
    with db.LOCK:
        conn.executescript(SCHEMA)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = [dict(r) for r in conn.execute("""select * from news_items where kept=1 and ts >= ? order by tonight desc,
                                              coalesce(impact, 0) desc, ts desc limit ?""", (since, limit * 3))]
    for r in rows:
        r["teams"] = json.loads(r["teams"] or "[]")
        r["players"] = json.loads(r["players"] or "[]")
        r["summary"] = spoiler_safe(r["summary"], r["kind"], r["players"])
        if SPOILER.search(r["title"] or ""):
            r["title"] = ""
    if team:
        rows = [r for r in rows if team in r["teams"]]
    return rows[:limit]


def to_push(conn, contenders: set, top_players: dict) -> list[dict]:
    """Unpushed news about tonight's key players or contenders. top_players: {team: set(lower-case names)}."""
    out = []
    for r in recent(conn, hours=12, limit=60):
        if r["pushed"] or r["kind"] not in PUSH_KINDS or not r["tonight"]:
            continue
        key_player = any(p.lower() in top_players.get(t, set()) for t in r["teams"] for p in r["players"])
        if key_player or (set(r["teams"]) & contenders and (r["impact"] or 0) >= 1.0):
            out.append(r)
    return out


def mark_pushed(conn, ids):
    with db.LOCK:
        conn.executemany("update news_items set pushed=1 where id=?", [(i,) for i in ids])
