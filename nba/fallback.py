"""hoopR-nba-data parquet (sportsdataverse, rebuilt from ESPN nightly ~22:13 SGT).
Fallback when the ESPN API fails, and the history source for Elo replay and the backtest."""
from functools import lru_cache
from datetime import date as _date

import pandas as pd

from .espn import TEAM_IDS as TEAMS

BASE = "https://raw.githubusercontent.com/sportsdataverse/hoopR-nba-data/main/nba"


@lru_cache(maxsize=32)
def _read(kind, season, day):         # day: cache key so a long-running process refetches daily
    name = {"team_box": "team_box", "player_box": "player_box"}[kind]
    return pd.read_parquet(f"{BASE}/{kind}/parquet/{name}_{season}.parquet")


def team_box(season: int) -> pd.DataFrame:
    return _read("team_box", season, _date.today().isoformat())


def player_box(season: int) -> pd.DataFrame:
    return _read("player_box", season, _date.today().isoformat())


def _n(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def games_for_date(season: int, d: str) -> list[dict]:
    """Rows shaped like espn.parse_summary() output (no plays) for every final game on US date d."""
    tb = team_box(season)
    tb = tb[tb["game_date"].astype(str) == d]
    pb = player_box(season)
    pb = pb[pb["game_date"].astype(str) == d]
    out = []
    for gid, g in tb.groupby("game_id"):
        if len(g) != 2 or not set(g.team_abbreviation) <= set(TEAMS):
            continue
        h = g[g.team_home_away == "home"].iloc[0]
        a = g[g.team_home_away == "away"].iloc[0]
        game = {"game_id": str(gid), "date": d, "season": int(h.season), "season_type": int(h.season_type),
                "home": h.team_abbreviation, "away": a.team_abbreviation, "tip_utc": str(h.game_date_time),
                "status": "final", "home_pts": _n(h.team_score), "away_pts": _n(a.team_score), "ot": 0,
                "neutral": 0, "note": None}
        teams = []
        for r in (h, a):
            fga, oreb = _n(r.field_goals_attempted), _n(r.offensive_rebounds)
            tov, fta = _n(r.total_turnovers) or _n(r.turnovers), _n(r.free_throws_attempted)
            teams.append({
                "game_id": str(gid), "team": r.team_abbreviation, "opp": r.opponent_team_abbreviation,
                "is_home": int(r.team_home_away == "home"), "pts": _n(r.team_score), "fgm": _n(r.field_goals_made),
                "fga": fga, "fg3m": _n(r.three_point_field_goals_made), "fg3a": _n(r.three_point_field_goals_attempted),
                "ftm": _n(r.free_throws_made), "fta": fta, "oreb": oreb, "dreb": _n(r.defensive_rebounds),
                "ast": _n(r.assists), "stl": _n(r.steals), "blk": _n(r.blocks), "tov": tov, "pf": _n(r.fouls),
                "paint": _n(r.points_in_paint), "fastbreak": _n(r.fast_break_points), "pts_off_to": _n(r.turnover_points),
                "largest_lead": _n(r.largest_lead), "lead_changes": _n(r.lead_changes),
                "poss": fga - oreb + tov + 0.44 * fta})
        players = []
        for r in pb[pb.game_id == gid].itertuples():
            players.append({
                "game_id": str(gid), "team": r.team_abbreviation, "player_id": str(r.athlete_id),
                "name": r.athlete_display_name, "position": r.athlete_position_abbreviation,
                "starter": int(bool(r.starter)), "minutes": float(_n(r.minutes)), "pts": _n(r.points),
                "reb": _n(r.rebounds), "oreb": _n(r.offensive_rebounds), "dreb": _n(r.defensive_rebounds),
                "ast": _n(r.assists), "stl": _n(r.steals), "blk": _n(r.blocks), "tov": _n(r.turnovers),
                "pf": _n(r.fouls), "fgm": _n(r.field_goals_made), "fga": _n(r.field_goals_attempted),
                "fg3m": _n(r.three_point_field_goals_made), "fg3a": _n(r.three_point_field_goals_attempted),
                "ftm": _n(r.free_throws_made), "fta": _n(r.free_throws_attempted), "plus_minus": _n(r.plus_minus),
                "dnp": int(bool(r.did_not_play)), "reason": r.reason})
        out.append({"game": game, "team_box": teams, "player_box": players, "plays": [], "odds": {}})
    return out


def season_results(season: int) -> pd.DataFrame:
    """One row per game: game_id, date, season_type, home, away, home_pts, away_pts, home_poss, away_poss."""
    tb = team_box(season)
    tb = tb.assign(poss=tb.field_goals_attempted - tb.offensive_rebounds + tb.total_turnovers.fillna(tb.turnovers)
                   + 0.44 * tb.free_throws_attempted)
    h = tb[tb.team_home_away == "home"]
    a = tb[tb.team_home_away == "away"]
    m = h.merge(a, on="game_id", suffixes=("_h", "_a"))
    df = pd.DataFrame({
        "game_id": m.game_id.astype(str), "date": m.game_date_h.astype(str), "season_type": m.season_type_h,
        "home": m.team_abbreviation_h, "away": m.team_abbreviation_a,
        "home_pts": m.team_score_h.astype(int), "away_pts": m.team_score_a.astype(int),
        "home_poss": m.poss_h, "away_poss": m.poss_a})
    real = df.home.isin(TEAMS) & df.away.isin(TEAMS)          # drops All-Star / exhibition games
    return df[real].sort_values("date").reset_index(drop=True)
