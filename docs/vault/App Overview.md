---
tags: [active]
updated: 2026-11-09
---
# App Overview

**Status: Phase 1 live on the NAS since 2026-11-09** (container `nba-tracker`, bot @gcjk_nbatracker_bot → the owner Channel › 🏀 NBA). Spec: `docs/superpowers/specs/2026-11-09-nba-tracker-phase1-design.md`.

Python 3.12 package `nba/`, one container `nba-tracker` (`python -m nba serve`): Telegram bot + APScheduler in one
asyncio loop; pipelines run one at a time in a worker thread (`nba/scheduler.py`, `JOB_LOCK`).

## Data (NBA.com blocks the home IP, probed 2026-11-09)
- `nba/espn.py` — ESPN public JSON: scoreboard, summary (box, play-by-play, win probability per play, odds),
  injuries, team schedules, DraftKings title futures. 1.5 s between calls, 3 tries, plain client name.
- `nba/fallback.py` — hoopR-nba-data parquet (same ESPN ids); fallback after ESPN fails, history for Elo/backtest.
- `nba/collector.py` — idempotent collect + backfill; `source` column says espn/hoopr; schedule never overwrites a final.

## Model
- `nba/ratings.py` — ridge off/def per 100 (garbage time removed, half of 3P% luck removed, recency half-life),
  blended with last season ×2/3 on the 30/70 → 80/20 ramp; availability adjustment from a box-score player value
  (`ponytail:` proxy until Phase 2 on/off). `healthy_net` = full rotation.
- `nba/elo.py` — 538 method, seeded from the archive (ends 2025-03-17) and replayed on hoopR.
- `nba/simulate.py` — 10k-run season + play-in + playoffs (random tiebreaks), series odds from series scores.
- `nba/predictions.py` — Φ(margin/σ); append-only versions (DB triggers), refused after tip; graded = last before tip.
- `nba/scorecard.py` — accuracy, Brier, log loss, calibration, vs Elo and market.
- `nba/review.py` — **AAR** for every miss (or |error| ≥ 15): error split into availability, shooting luck,
  free throws, turnovers, rebounding, noise (sums exactly to the error).
- `nba/tuning.py` — walk-forward grid search after every graded slate once ≥ 300 games exist; adopts params only if
  season log loss drops ≥ 0.002; append-only `model_params` history. Claude never sets numbers.
- `nba/backtest.py` — 10 seasons point in time; tuned on 2017-21, validated 2022-26.

## Output
- `nba/analyst.py` — `claude -p --bare` (sonnet-5-5 daily/weekly, haiku-5-5 `/ask`), pydantic schema, 1 retry,
  stats-only fallback. Prompts in `nba/prompts/`.
- `nba/telegram_pub.py` — fleet card style; "Worth watching" built only from stars + reason codes (no scores);
  results/AAR/impact behind spoilers; fits 4,000 chars (drops movers → record → impact → status, then splits).
- `nba/obsidian_pub.py` — runtime vault `/volume1/<USER>/Obsidian/NBA Tracker/`.
- `nba/schemes.py` — shot classification from ESPN pbp, team offense/defense profiles + league percentiles, inferred
  scheme labels (confidence ≤ 0.7, with evidence), record vs styles, what-wins correlations. `nba/players.py` — roster status,
  role changes, per-player shot diet.
- `nba/playbook_kb.py` — named sets / defenses (what each is, `COUNTERS` = what beats it, `SET_TO_PLAYS` / `COVERAGE_GIVES` for the estimated success, `DEF_GROUP` = base / pick-and-roll / help / zone / junk / pressure); `nba/social_intel.py` `season_articles` + `season_videos` find this season's breakdowns per team; `web.named_sets` / `web.defense_schemes` feed the playbook cards, drawer and `/plays`.
- `nba/web.py` + `nba/dashboard.html` — animated dashboard on NAS port 8795 (team drawer at `/team/<CODE>.json`) (title race replay, odds over time, power
  rankings, Emirates NBA Cup groups (`web.cup_table`), tonight vs market, last night behind a reveal, calibration, AAR causes, model changes); `/data.json` too.
- `nba/bot.py` — `/team /compare /whatif /player /status /game /scorecard /ask /plays /film /help`, `ALLOWED_USER_IDS` only.

## Schedule (SGT)
15:15 daily · every 15 min 01:00–14:45 live sweep (`nba/jobs.py` `live`: each game that just went final is stored and gets one
📊 FINAL card; dashboard updates at once) · every 30 min 15:30–07:30 injury check · Mon 15:45 weekly · startup: migrate, backfill, catch-up.
`job_runs` + step marks make every run exactly-once (no double digest after a restart).

## Ship flow
Edit here → `python -m pytest tests` → commit → `sh deploy.sh` → `python -u temp/sync_host.py <ip> --push nba-tracker`
in `~/nas-sync` → dry run clean.
