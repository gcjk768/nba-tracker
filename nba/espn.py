"""ESPN public JSON API: throttled client + pure parsers.

NBA.com blocks the home IP (probed 2026-11-09), so ESPN is the live source. Plain client name, no browser UA.
"""
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
ET = ZoneInfo("America/New_York")

TEAM_IDS = {"ATL": 1, "BOS": 2, "BKN": 17, "CHA": 30, "CHI": 4, "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GS": 9,
            "HOU": 10, "IND": 11, "LAC": 12, "LAL": 13, "MEM": 29, "MIA": 14, "MIL": 15, "MIN": 16, "NO": 3, "NY": 18,
            "OKC": 25, "ORL": 19, "PHI": 20, "PHX": 21, "POR": 22, "SAC": 23, "SA": 24, "TOR": 28, "UTAH": 26, "WSH": 27}
ID_TEAM = {str(v): k for k, v in TEAM_IDS.items()}
EAST = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NY", "ORL", "PHI", "TOR", "WSH"}
NAMES = {"ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
         "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
         "DET": "Detroit Pistons", "GS": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
         "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
         "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NO": "New Orleans Pelicans", "NY": "New York Knicks",
         "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
         "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SA": "San Antonio Spurs", "TOR": "Toronto Raptors",
         "UTAH": "Utah Jazz", "WSH": "Washington Wizards"}
ALIASES = {"GSW": "GS", "NYK": "NY", "NOP": "NO", "SAS": "SA", "UTA": "UTAH", "WAS": "WSH", "BRK": "BKN",
           "CHO": "CHA", "PHO": "PHX"}


def team_code(s: str) -> str | None:
    """User input / other sources → ESPN code (BOS, GS, NY...)."""
    s = (s or "").strip().upper()
    s = ALIASES.get(s, s)
    if s in TEAM_IDS:
        return s
    for code, name in NAMES.items():
        if s and s in name.upper():
            return code
    return None


class Client:
    def __init__(self, min_interval=1.5, retries=3, timeout=30):
        self.min_interval, self.retries = min_interval, retries
        self.http = httpx.Client(timeout=timeout, headers={"User-Agent": "nba-tracker/1.0 (personal, low volume)"})
        self._last = 0.0

    def get(self, url, **params):
        err = None
        for attempt in range(self.retries):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.get(url, params=params or None)
                if r.status_code == 200:
                    return r.json()
                err = RuntimeError(f"HTTP {r.status_code} {url}")
                if r.status_code in (403, 404, 429):   # not transient for us: a block or a missing page
                    break
            except (httpx.HTTPError, ValueError) as e:
                err = e
            time.sleep(2 ** attempt)
        raise err

    def scoreboard(self, d):           # d: date or 'YYYYMMDD'
        return self.get(f"{SITE}/scoreboard", dates=_ymd(d))

    def summary(self, game_id):
        return self.get(f"{SITE}/summary", event=game_id)

    def injuries(self):
        return self.get(f"{SITE}/injuries")

    def team_schedule(self, team, season, seasontype=None):
        p = {"season": season}
        if seasontype:
            p["seasontype"] = seasontype
        return self.get(f"{SITE}/teams/{TEAM_IDS[team]}/schedule", **p)

    def futures(self, season):
        j = self.get(f"{CORE}/seasons/{season}/futures")
        for item in j.get("items", []):
            if item.get("name") == "NBA - Winner":
                return item
        for item in j.get("items", []):     # list may hold $refs only
            ref = item.get("$ref")
            if ref:
                full = self.get(ref.replace("http://", "https://"))
                if full.get("name") == "NBA - Winner":
                    return full
        return None


def _ymd(d):
    return d if isinstance(d, str) else d.strftime("%Y%m%d")


def et_date(iso_utc: str) -> str:
    """Tip time (UTC ISO) → US Eastern calendar date, which is how the NBA names a game night."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(ET).date().isoformat()


def clock_seconds(s: str) -> float:
    s = (s or "0").strip()
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s or 0)


def _status(name: str) -> str:
    return {"STATUS_FINAL": "final", "STATUS_SCHEDULED": "scheduled", "STATUS_POSTPONED": "postponed",
            "STATUS_CANCELED": "canceled"}.get(name, "live")


def _game_from_competition(event_id, iso_date, season, season_type, comp):
    home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
    away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
    st = comp.get("status") or {}
    status = _status(st.get("type", {}).get("name", ""))
    period = int(st.get("period") or 0)

    def pts(c):
        sc = c.get("score")
        if isinstance(sc, dict):
            sc = sc.get("value")
        try:
            return int(float(sc))
        except (TypeError, ValueError):
            return None
    notes = comp.get("notes") or []
    return {
        "game_id": str(event_id), "date": et_date(iso_date), "season": season, "season_type": season_type,
        "home": home["team"]["abbreviation"], "away": away["team"]["abbreviation"], "tip_utc": iso_date,
        "status": status, "home_pts": pts(home) if status == "final" else None,
        "away_pts": pts(away) if status == "final" else None, "ot": max(0, period - 4) if status == "final" else 0,
        "neutral": int(bool(comp.get("neutralSite"))), "note": notes[0].get("headline") if notes else None,
    }


def parse_scoreboard(j) -> list[dict]:
    out = []
    for e in j.get("events", []):
        s = e.get("season", {})
        out.append(_game_from_competition(e["id"], e["date"], s.get("year"), s.get("type"), e["competitions"][0]))
    return out


def scoreboard_odds(j) -> dict:
    """{game_id: {home_ml, away_ml, total}} from the scoreboard's first book (closing/current line)."""
    out = {}
    for e in j.get("events", []):
        o = (e["competitions"][0].get("odds") or [None])[0]
        if not o:
            continue
        ml = o.get("moneyline") or {}

        def am(side):
            v = ((ml.get(side) or {}).get("close") or {}).get("odds")
            try:
                return float(str(v).replace("EVEN", "+100"))
            except (TypeError, ValueError):
                return None
        out[str(e["id"])] = {"home_ml": am("home"), "away_ml": am("away"), "total": o.get("overUnder")}
    return out


def parse_team_schedule(j) -> list[dict]:
    out = []
    for e in j.get("events", []):
        st = e.get("seasonType", {}).get("type") or e.get("season", {}).get("type")
        yr = e.get("season", {}).get("year")
        out.append(_game_from_competition(e["id"], e["date"], yr, st, e["competitions"][0]))
    return out


def _pair(v):
    a, b = (v or "0-0").split("-")
    return int(a or 0), int(b or 0)


def _int(v):
    try:
        return int(float(str(v).replace("+", "")))
    except (TypeError, ValueError):
        return 0


def possessions(fga, oreb, tov, fta):
    return fga - oreb + tov + 0.44 * fta


def parse_summary(j) -> dict:
    comp = j["header"]["competitions"][0]
    season = j["header"].get("season", {})
    game = _game_from_competition(j["header"]["id"], comp["date"], season.get("year"), season.get("type"), comp)
    id2team = {c["team"]["id"]: c["team"]["abbreviation"] for c in comp["competitors"]}
    teams = []
    for t in j["boxscore"]["teams"]:
        ab = t["team"]["abbreviation"]
        s = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        fgm, fga = _pair(s.get("fieldGoalsMade-fieldGoalsAttempted"))
        f3m, f3a = _pair(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
        ftm, fta = _pair(s.get("freeThrowsMade-freeThrowsAttempted"))
        oreb, tov = _int(s.get("offensiveRebounds")), _int(s.get("totalTurnovers") or s.get("turnovers"))
        is_home = ab == game["home"]
        teams.append({
            "game_id": game["game_id"], "team": ab, "opp": game["away"] if is_home else game["home"],
            "is_home": int(is_home), "pts": game["home_pts"] if is_home else game["away_pts"],
            "fgm": fgm, "fga": fga, "fg3m": f3m, "fg3a": f3a, "ftm": ftm, "fta": fta, "oreb": oreb,
            "dreb": _int(s.get("defensiveRebounds")), "ast": _int(s.get("assists")), "stl": _int(s.get("steals")),
            "blk": _int(s.get("blocks")), "tov": tov, "pf": _int(s.get("fouls")),
            "paint": _int(s.get("pointsInPaint")), "fastbreak": _int(s.get("fastBreakPoints")),
            "pts_off_to": _int(s.get("turnoverPoints")), "largest_lead": _int(s.get("largestLead")),
            "lead_changes": _int(s.get("leadChanges")), "poss": possessions(fga, oreb, tov, fta),
        })
    players = []
    for t in j["boxscore"].get("players", []):
        ab = t["team"]["abbreviation"]
        for grp in t.get("statistics", []):
            keys = grp.get("keys", [])
            for a in grp.get("athletes", []):
                st = dict(zip(keys, a.get("stats") or []))
                fgm, fga = _pair(st.get("fieldGoalsMade-fieldGoalsAttempted"))
                f3m, f3a = _pair(st.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                ftm, fta = _pair(st.get("freeThrowsMade-freeThrowsAttempted"))
                ath = a["athlete"]
                players.append({
                    "game_id": game["game_id"], "team": ab, "player_id": str(ath["id"]), "name": ath.get("displayName"),
                    "position": (ath.get("position") or {}).get("abbreviation"), "starter": int(bool(a.get("starter"))),
                    "minutes": float(_int(st.get("minutes"))), "pts": _int(st.get("points")), "reb": _int(st.get("rebounds")),
                    "oreb": _int(st.get("offensiveRebounds")), "dreb": _int(st.get("defensiveRebounds")),
                    "ast": _int(st.get("assists")), "stl": _int(st.get("steals")), "blk": _int(st.get("blocks")),
                    "tov": _int(st.get("turnovers")), "pf": _int(st.get("fouls")), "fgm": fgm, "fga": fga,
                    "fg3m": f3m, "fg3a": f3a, "ftm": ftm, "fta": fta, "plus_minus": _int(st.get("plusMinus")),
                    "dnp": int(bool(a.get("didNotPlay"))), "reason": a.get("reason"),
                })
    wp = {w["playId"]: w["homeWinPercentage"] for w in j.get("winprobability", [])}
    plays = []
    for p in j.get("plays", []):
        parts = p.get("participants") or []
        co = p.get("coordinate") or {}
        x, y = co.get("x"), co.get("y")
        if x is not None and abs(x) > 1000:     # ESPN sentinel for "no location"
            x = y = None
        plays.append({
            "game_id": game["game_id"], "seq": int(p["sequenceNumber"]), "period": p["period"]["number"],
            "clock_s": clock_seconds(p.get("clock", {}).get("displayValue")), "type": p["type"]["text"],
            "text": p.get("text"), "team": id2team.get((p.get("team") or {}).get("id")),
            "player_id": parts[0]["athlete"]["id"] if parts else None,
            "home_score": p.get("homeScore"), "away_score": p.get("awayScore"), "home_wp": wp.get(p["id"]),
            "scoring": int(bool(p.get("scoringPlay"))), "shooting": int(bool(p.get("shootingPlay"))), "x": x, "y": y,
        })
    pc = (j.get("pickcenter") or [None])[0] or {}
    odds = {"spread": pc.get("spread"), "total": pc.get("overUnder"),
            "home_ml": (pc.get("homeTeamOdds") or {}).get("moneyLine"),
            "away_ml": (pc.get("awayTeamOdds") or {}).get("moneyLine")}
    return {"game": game, "team_box": teams, "player_box": players, "plays": plays, "odds": odds}


_GAME_STATUS = re.compile(r"\b(doubtful|questionable|probable)\b", re.I)


def injury_status(espn_status: str, comment: str) -> str:
    s = (espn_status or "").lower()
    if s == "out":
        return "out"
    if "suspen" in s:
        return "suspended"
    m = _GAME_STATUS.search(comment or "")
    if m:
        return m.group(1).lower()
    return "questionable" if "day" in s else s or "questionable"


def parse_injuries(j) -> list[dict]:
    out = []
    for t in j.get("injuries", []):
        for i in t.get("injuries", []):
            a = i.get("athlete", {})
            href = next((l.get("href", "") for l in a.get("links", [])), "")
            m = re.search(r"/id/(\d+)", href)
            team = (a.get("team") or {}).get("abbreviation") or ID_TEAM.get(str(t.get("id")))
            det = i.get("details") or {}
            detail = " ".join(x for x in (det.get("side"), det.get("type")) if x and x != "Not Specified")
            ret = det.get("returnDate")
            out.append({"player_id": m.group(1) if m else a.get("displayName"), "name": a.get("displayName"),
                        "team": team, "status": injury_status(i.get("status"), i.get("shortComment")),
                        "detail": (detail + (f" (expected back {ret})" if ret else "")).strip()})
    return out


def parse_futures(item) -> dict[str, float]:
    """'NBA - Winner' item → {team: american odds} from the first book listed."""
    if not item:
        return {}
    books = (item.get("futures") or [{}])[0].get("books", [])
    out = {}
    for b in books:
        ref = (b.get("team") or {}).get("$ref", "")
        m = re.search(r"/teams/(\d+)", ref)
        team = ID_TEAM.get(m.group(1)) if m else None
        try:
            out[team] = float(b.get("value"))
        except (TypeError, ValueError):
            pass
    out.pop(None, None)
    return out
