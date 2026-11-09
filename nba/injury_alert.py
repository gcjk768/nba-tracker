"""Which injury changes deserve a push alert: a top-3 player on a top-8 contender, and a real odds move."""

TITLE_PP = 0.01      # alert if title odds move >= 1 percentage point
GAME_PP = 0.05       # or tonight's game win probability moves >= 5 points


def contenders(title_odds: dict, n=8) -> set:
    return set(sorted(title_odds, key=lambda t: -title_odds[t])[:n])


def top_players(values: dict, team: str, n=3) -> set:
    mine = [(pid, v["value"]) for pid, v in values.items() if v["team"] == team]
    return {pid for pid, _ in sorted(mine, key=lambda x: -x[1])[:n]}


def relevant(changes: list[dict], title_odds: dict, values: dict) -> list[dict]:
    top = contenders(title_odds)
    return [c for c in changes if c["team"] in top and c["player_id"] in top_players(values, c["team"])]


def should_alert(d_title: float, d_game: float | None) -> bool:
    return abs(d_title) >= TITLE_PP or (d_game is not None and abs(d_game) >= GAME_PP)
