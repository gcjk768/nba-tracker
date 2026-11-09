"""Watchability score, garbage time and win probability added, all from ESPN's per-play win probability.

ESPN already publishes a win probability for every play, so there is no WP model to train (the brief's
nba_wp/nba_comeback approach is what ESPN's curve gives us for free).
"""
import math

REASONS = ("late swing", "comeback", "overtime", "big individual night", "close finish", "back and forth")


def game_score(p) -> float:
    """Hollinger game score from a player_box row."""
    return (p["pts"] + 0.4 * p["fgm"] - 0.7 * p["fga"] - 0.4 * (p["fta"] - p["ftm"]) + 0.7 * p["oreb"]
            + 0.3 * p["dreb"] + p["stl"] + 0.7 * p["ast"] + 0.7 * p["blk"] - 0.4 * p["pf"] - p["tov"])


def _left(p):
    """Seconds left in regulation (0 in OT)."""
    return 0.0 if p["period"] > 4 else (4 - p["period"]) * 720 + p["clock_s"]


def garbage_start(plays) -> int | None:
    """Index of the first garbage-time play: Q4 margin >= 25 under 8:00, >= 20 under 5:00, >= 15 under 2:00.
    Cancelled if the margin later drops under 10."""
    start = None
    for i, p in enumerate(plays):
        if p["home_score"] is None:
            continue
        m = abs(p["home_score"] - p["away_score"])
        if start is not None and m < 10:
            start = None
        if start is None and p["period"] == 4:
            c = p["clock_s"]
            if (c <= 480 and m >= 25) or (c <= 300 and m >= 20) or (c <= 120 and m >= 15):
                start = i
    return start


def clean_split(plays, ot):
    """(home_pts, away_pts, fraction of game time) before garbage time; None if no garbage time."""
    i = garbage_start(plays)
    if i is None:
        return None
    p = plays[i]
    total = 2880 + 300 * ot
    elapsed = 2880 - _left(p)
    return p["home_score"], p["away_score"], elapsed / total


def wpa(plays, home, away) -> dict:
    """Credit each play's change in win probability to the play's first participant, in their team's view.
    Plays with no player go to a 'TEAM:<abbr>' bucket. Convention: sum(winner side) - sum(loser side)
    == winner final WP - winner starting WP (each change is counted exactly once)."""
    out = {}
    prev = None
    for p in plays:
        wp = p["home_wp"]
        if wp is None:
            continue
        if prev is not None:
            d = wp - prev
            if d:
                team = p["team"] or home
                key = p["player_id"] or f"TEAM:{team}"
                if not p["player_id"] and not p["team"]:
                    key = f"TEAM:{home}"
                sign = 1 if team == home else -1
                out[key] = out.get(key, 0.0) + sign * d
        prev = wp
    return out


def score(plays, ot, best_game_score=0.0, lead_changes=0):
    """(score 0-100, stars 1-5, reasons). Reasons are spoiler-free codes from REASONS only."""
    wps = [p["home_wp"] for p in plays if p["home_wp"] is not None]
    if len(wps) < 10:
        return 0.0, 1, []
    home_won = wps[-1] > 0.5
    w = [x if home_won else 1 - x for x in wps]
    swing = sum(abs(b - a) for a, b in zip(wps, wps[1:]))
    comeback = 1 - min(w)                       # 0.9 = winner was down to a 10% chance
    late = [p for p in plays if p["home_wp"] is not None and (p["period"] > 4 or (p["period"] == 4 and p["clock_s"] <= 300))]
    late_wp = [p["home_wp"] for p in late]
    close = sum(1 for x in late_wp if 0.2 <= x <= 0.8) / len(late_wp) if late_wp else 0.0
    crossings = sum(1 for a, b in zip(late_wp, late_wp[1:]) if (a - 0.5) * (b - 0.5) < 0)
    s = (0.25 * min(swing / 5, 1) + 0.30 * max(0.0, (comeback - 0.5) / 0.5) + 0.30 * close
         + 0.10 * min(ot, 2) / 2 + 0.05 * (best_game_score >= 35))
    s = round(100 * min(s * 1.25, 1.0), 1)
    stars = max(1, min(5, 1 + math.floor(s / 20)))
    reasons = []
    if crossings >= 1:
        reasons.append("late swing")
    if comeback >= 0.8:
        reasons.append("comeback")
    if ot:
        reasons.append("overtime")
    if close >= 0.5 and "late swing" not in reasons:
        reasons.append("close finish")
    if swing >= 5 or lead_changes >= 12:
        reasons.append("back and forth")
    if best_game_score >= 35:
        reasons.append("big individual night")
    return s, stars, reasons
