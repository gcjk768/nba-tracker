"""Elo second opinion, FiveThirtyEight method (as in nicidob/nba_elo), seeded from the 538 archive.

The archive (Neil-Paine-1/NBA-elo) stops on 2025-03-17, so we seed from the end of an archived season and
replay later seasons from hoopR results. Same code path for the live rating and the backtest.
"""
import pandas as pd

ARCHIVE_URL = "https://raw.githubusercontent.com/Neil-Paine-1/NBA-elo/main/nba_elo.csv"
K, HCA, MEAN, CARRY = 20.0, 100.0, 1505.0, 0.75
CODES_538 = {"BRK": "BKN", "CHO": "CHA", "GSW": "GS", "NOP": "NO", "NYK": "NY", "PHO": "PHX", "SAS": "SA",
             "UTA": "UTAH", "WAS": "WSH"}


def prob(elo_h, elo_a, neutral=False) -> float:
    return 1 / (1 + 10 ** (-(elo_h + (0 if neutral else HCA) - elo_a) / 400))


def update(elo_h, elo_a, home_pts, away_pts, neutral=False):
    p = prob(elo_h, elo_a, neutral)
    home_won = home_pts > away_pts
    mov = abs(home_pts - away_pts)
    diff = (elo_h + (0 if neutral else HCA) - elo_a) * (1 if home_won else -1)   # winner minus loser
    mult = ((mov + 3) ** 0.8) / (7.5 + 0.006 * diff)
    shift = K * mult * ((1.0 if home_won else 0.0) - p)
    return elo_h + shift, elo_a - shift


def seed_from_archive(archive: pd.DataFrame, season: int) -> dict:
    """Final Elo of each team after archived season `season` (538 labels 2015-16 as 2016)."""
    d = archive[archive.season == season].sort_values("date")
    last = d.groupby("team1").tail(1)
    return {CODES_538.get(t, t): float(e) for t, e in zip(last.team1, last.elo1_post)}


def carry(ratings: dict) -> dict:
    return {t: CARRY * e + (1 - CARRY) * MEAN for t, e in ratings.items()}


def replay(seed: dict, results: pd.DataFrame, carry_first=True):
    """results: date-sorted rows (date, home, away, home_pts, away_pts[, neutral]) of ONE season.
    Returns (ratings after the season, list of (game_id, elo_home_pre, elo_away_pre))."""
    r = carry(seed) if carry_first else dict(seed)
    pre = []
    for g in results.itertuples():
        eh, ea = r.get(g.home, MEAN), r.get(g.away, MEAN)
        pre.append((getattr(g, "game_id", None), eh, ea))
        r[g.home], r[g.away] = update(eh, ea, g.home_pts, g.away_pts, bool(getattr(g, "neutral", 0)))
    return r, pre


def load_archive(path_or_url=ARCHIVE_URL) -> pd.DataFrame:
    return pd.read_csv(path_or_url, usecols=["date", "season", "team1", "elo1_post"])
