# NBA Tracker

Self-hosted NBA 2026-27 season tracker and champion predictor for the home NAS (UGREEN, Docker).

Every day it collects last night's games from ESPN, rates all 30 teams, simulates the rest of the season and the
playoffs (10,000 runs), freezes a prediction for every game, runs an After Action Review on every missed pick and
re-tunes itself, asks `claude -p` for a verdict, posts a spoiler-safe Telegram digest and writes the full detail to
an Obsidian vault.

```
python -m nba serve          # bot + scheduler (container default)
python -m nba daily|weekly|injury_check|backfill|predict|tune|backtest|health
python -m pytest tests       # 54 tests, offline (saved ESPN fixtures)
```

Deploy: `sh deploy.sh` (needs `.deploy.env` with `NAS=user@host`). Secrets go in the NAS `.env` (see `env.example`).
Docs: `docs/vault/` (App Overview, Changelog, Roadmap) and `docs/superpowers/specs/`.

Data: ESPN public JSON API (live) and sportsdataverse/hoopR-nba-data (fallback + history); Elo seeded from the
FiveThirtyEight archive (Neil-Paine-1/NBA-elo). NBA.com is not used (blocked from the home network).
