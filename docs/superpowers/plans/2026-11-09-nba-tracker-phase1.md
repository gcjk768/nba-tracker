# NBA Tracker Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One container on the NAS that collects every NBA game from ESPN, rates teams, simulates the season, predicts and grades games, scores watchability, watches injuries, asks Claude for a verdict and publishes to Telegram and Obsidian.

**Architecture:** Flat Python package in the repo root (one file per module in the spec table). SQLite (WAL) is the only state.
`jobs.py` composes the pipelines; `scheduler.py` + `bot.py` share one asyncio loop; heavy jobs run in `asyncio.to_thread`.

**Tech Stack:** Python 3.12, httpx, pandas + pyarrow (hoopR parquet), numpy/scipy, pydantic v2, python-telegram-bot 22, APScheduler 3.11, pytest. `claude` CLI (Node 22) in the image.

**Execution note (2026-11-09):** executed inline by the session that wrote the spec (it holds the probe results). Each task follows TDD: write the listed tests first against the saved fixtures, watch them fail, implement, pass, commit. The plan pins interfaces and test assertions; the code lives in the files, not duplicated here.

Fixtures (saved 2026-11-09, real ESPN data): `tests/fixtures/scoreboard_20261108.json`, `scoreboard_20261109.json`, `summary_401909945.json` (PHI 127 @ CHI 121), `injuries.json`.

Facts the code relies on (probed):
- ESPN team codes: ATL BOS BKN CHA CHI CLE DAL DEN DET GS HOU IND LAC LAL MEM MIA MIL MIN NO NY OKC ORL PHI PHX POR SAC SA TOR UTAH WSH. hoopR uses the same ids/codes and the same `game_id`.
- Summary `boxscore.teams[i].statistics[]` items have `name` / `displayValue`; made-attempted pairs are `"46-99"`. Player stats: `boxscore.players[i].statistics[0].keys` + `athletes[].stats`, `starter`, `didNotPlay`, `reason`.
- `plays[]`: `id, sequenceNumber, type.text, text, period.number, clock.displayValue, scoringPlay, scoreValue, team.id, participants[].athlete.id, shootingPlay, coordinate, homeScore, awayScore`. `winprobability[]`: `homeWinPercentage, playId` aligned 1:1 with plays.
- `pickcenter[0]`: `spread, overUnder, homeTeamOdds.moneyLine, awayTeamOdds.moneyLine`.
- Team schedule: `/teams/{id}/schedule?season=2027` (no date-range scoreboard). Futures: core API `seasons/2027/futures`, item `name == "NBA - Winner"`.
- 538 archive `nba_elo.csv` columns `date,season,neutral,playoff,team1,team2,elo1_pre,elo2_pre,...,elo1_post,elo2_post,score1,score2,is_home`, last row 2025-03-17; 538 codes differ (BRK, CHO, GSW, NOP, NYK, PHO, SAS, UTA, WAS).

---

### Task 1: Skeleton, config, db
**Files:** `requirements.txt`, `config.py`, `db.py`, `tests/conftest.py`, `tests/test_db.py`
- `config.Settings` (frozen dataclass from env): `data_dir, vault_dir, season="2026-27", espn_season=2027, tg_token, tg_chat_id, tg_thread_id, owner_id, spoiler(bool), ask_limit=20, model_daily="claude-sonnet-5-5", model_ask="claude-haiku-5-5", obsidian_vault_name`.
- `db.connect(path) -> sqlite3.Connection` (WAL, row_factory, `check_same_thread=False`), `db.LOCK` (threading.RLock), `db.migrate(conn)` creates spec §5 tables + triggers `predictions_no_update`/`predictions_no_delete` (RAISE(ABORT)).
- Tests: migrate twice is idempotent; UPDATE/DELETE on predictions raises `sqlite3.IntegrityError`; `job_runs` PK prevents duplicates.

### Task 2: ESPN client + parsers
**Files:** `espn.py`, `tests/test_espn.py`
- `Client(min_interval=1.5, retries=3)`: `.get(url) -> dict` throttled, exponential backoff, UA `nba-tracker/1.0 (personal, low volume)`.
- Pure parsers: `parse_scoreboard(j) -> list[GameRow]`; `parse_summary(j) -> dict(game, team_box[2], player_box[], plays[])`; `parse_injuries(j) -> list[dict]`; `parse_futures(j_items) -> dict[team, american]`; `parse_team_schedule(j) -> list[GameRow]`.
- `possessions(fga, oreb, tov, fta) = fga - oreb + tov + 0.44*fta`.
- Tests on fixtures: 7 games on 2026-11-08 all final; summary gives PHI 127 CHI 121, both team rows have fga/oreb/tov, player rows have minutes and starter flags, plays count 476 with home_wp attached; injuries list non-empty with status in {Out, Day-To-Day, ...}.

### Task 3: hoopR fallback + collector
**Files:** `fallback.py`, `collector.py`, `tests/test_collector.py`
- `fallback.team_box(season) / player_box(season) -> DataFrame` (raw GitHub parquet, cached per process per day).
- `collector.collect_date(conn, client, date)`: scoreboard → for each final game not stored: summary → upsert rows, `source='espn'`. After 3 consecutive ESPN failures: load hoopR rows for that date, `source='hoopr'`, return `warnings=["ESPN failed ... used hoopR"]`. Returns `CollectResult(new_games, warnings)`.
- `collector.refresh_schedule(conn, client)`: 30 team schedules → upsert scheduled games (status, tip time).
- `collector.backfill(conn, client, start, end)`: dates without stored finals.
- Tests: collecting the fixture date twice stores 7 games once; a client raising always → hoopR path used (fallback stubbed) and warning returned.

### Task 4: Garbage time, excitement, WPA
**Files:** `excitement.py`, `tests/test_excitement.py`
- `garbage_start(plays) -> seq|None` (spec §6 thresholds, Q4/OT only).
- `wpa(plays) -> dict[player_id|"TEAM:<abbr>", float]` from consecutive home_wp deltas, signed for the player's team, credited to `participants[0]`; plays without participant → team bucket.
- `score(plays, ot, top_line_gamescore) -> (score 0-100, stars 1-5, reasons[list of codes])`: total |ΔWP|, comeback (min WP of eventual winner), last-5-min closeness, OT, big individual night (game score ≥ 35).
- Tests: Σ WPA for the winner's side (players + team bucket) == winner_final_wp − winner_start_wp within 1e-6; reasons contain only codes from `REASONS` (no digits, no team names); stars in 1..5.

### Task 5: Elo
**Files:** `elo.py`, `tests/test_elo.py`
- 538 method: K=20, HCA 100, MOV multiplier `((mov+3)**0.8)/(7.5+0.006*elo_diff_winner)`, season carry-over `0.75*elo + 0.25*1505`.
- `seed_from_archive(csv_path_or_url) -> dict[espn_team, elo]` (map 538 codes, last post per team).
- `replay(seed, games_df) -> (ratings_now, history_rows)`; `prob(elo_h, elo_a) = 1/(1+10**(-(elo_h+100-elo_a)/400))`.
- Tests: equal teams home prob ≈ 0.640; winner gains what loser loses; carry-over at season boundary.

### Task 6: Ratings
**Files:** `ratings.py`, `tests/test_ratings.py`
- `fit(games_df, lam) -> dict[team, (off, def, net)]` ridge on per-100 margins with HCA column; `blend_weight(g)` per spec §6; `prior(last_season_net) = 2/3 * net`; `player_values(player_box_df) -> dict[player_id, value]`; `injury_adjust(team_net, out_players)`.
- Tests: blend_weight(10)=0.30, blend_weight(55)=0.80, blend_weight(0)=0; a synthetic league where A beats everyone by 10 rates A highest; injury adjust lowers net.

### Task 7: Simulation
**Files:** `simulate.py`, `tests/test_simulate.py`
- `simulate(ratings, played_records, remaining_games, conf, n, seed) -> dict[team, {playoffs, conf_finals, finals, title}]` vectorised over n; play-in (7v8, 9v10, loser78 vs winner910); bracket 1v8.., best-of-7 game-level with HCA 2-2-1-1-1.
- `series_prob(net_a, net_b, home_a) -> float`.
- Tests: title probs sum to 1 (±1e-9); a +15 team wins title > 50% in a 30-team league of zeros; deterministic with seed.

### Task 8: Predictions + scorecard
**Files:** `predictions.py`, `scorecard.py`, `tests/test_predictions.py`
- `predict_game(h, a, ratings, extras) -> Pred(home_wp, margin, total, elo_wp, market_wp)`; `store(conn, game_id, pred, reason)` inserts version = max+1 only if `now < tip_utc` (else refuse); `graded_version(conn, game_id)`.
- `scorecard.grade(conn) -> dict(n, acc, brier, logloss, calib[buckets], elo_brier, market_brier, worst_teams)`.
- `american_to_prob(ml)`; market wp normalised from both moneylines.
- Tests: storing after tip raises; two versions before tip → graded = v2; Brier of perfect 1.0 picks = 0; calibration buckets cover 50-100.

### Task 8b: AAR + tuning (spec §6b)
**Files:** `review.py`, `tuning.py`, `tests/test_review.py`
- `review.review_game(conn, game_id) -> dict(pick, pick_wp, pred_margin, actual_margin, causes{name: pts}, main, lesson)`;
  causes sum to the margin error (noise = remainder). `review.review_slate(conn, date) -> list`.
- `tuning.Params(hca, sigma, b2b, half_life, injury_scale)`; `tuning.current(conn)`; `tuning.walk_forward_logloss(games, params)`;
  `tuning.tune(conn) -> (adopted: bool, old, new, ll_old, ll_new)` adopts only if Δ ≤ −0.002, writes `model_params` row.
- Tests: causes sum to margin error; a synthetic season where true HCA is 5 and current is 1 → tune adopts a larger hca;
  random-noise season → no adoption.

### Task 9: Injuries + alerts
**Files:** `injuries.py`, `injury_alert.py`, `tests/test_injuries.py`
- `snapshot(conn, rows, ts) -> list[Change]` diffs against latest snapshot per player (new player with non-available status counts as change; disappearing player → `Available`).
- `alerts.evaluate(conn, changes, ctx) -> list[Alert]`: only contender top-8 × top-3 player; recompute 2k sim + affected game preds; keep if Δtitle ≥ 1pp or Δgame ≥ 5pp; mark `alerted=1` so a change alerts once.
- Tests: same snapshot twice → no changes; status change → 1 change; evaluate twice on same change → one alert total.

### Task 10: Aggregator + analyst
**Files:** `aggregator.py`, `analyst.py`, `prompts/daily.md`, `prompts/weekly.md`, `prompts/ask.md`, `tests/test_analyst.py`
- `aggregator.team_season(conn, date)` (record home/road, pyth wins, SRS/net, net vs >.500, rolling 5/10, SOS played/remaining, luck flags, star workload); `daily_summary(...)`/`weekly_summary(...)` → compact dict (≤ ~15k tokens; asserted by JSON length ≤ 60k chars).
- `analyst.run(prompt_file, summary, model, runner=subprocess) -> (Verdict|None, raw)`: `claude -p <prompt> --output-format json --model <m>`, stdin = summary JSON; parse `.result`, extract JSON block, validate pydantic `Verdict` (spec schema, Phase 2 fields optional); one retry with the validation error; else None.
- Tests: valid JSON → Verdict; garbage twice → None; runner called exactly twice.

### Task 11: Telegram publisher
**Files:** `telegram_pub.py`, `tests/test_telegram.py`
- `SECTION_TITLES` constant; `build_digest(ctx, spoiler) -> list[str blocks]`; `fit(blocks, limit=4000) -> list[str messages]` drop order movers → record → shorten tactics → split between blocks; `worth_watching_block(excitement_rows)` takes only `{label: "AWAY @ HOME", stars, reasons}`.
- `send(text, buttons=None)` HTML, plain-text resend on 400; `send_photo`.
- Tests: 15-game synthetic night → every message ≤ 4000; worth-watching text contains no digits other than the star count and no `<tg-spoiler>` needed; spoiler off → no `tg-spoiler` anywhere; all dynamic values escaped (`<script>` name renders escaped).

### Task 12: Obsidian publisher
**Files:** `obsidian_pub.py`, `tests/test_obsidian.py`
- `write_note(path, frontmatter, body)` atomic tmp+rename, chmod 664; `activity(line)` append `Activity/YYYY/MM/YYYY-MM-DD.md`; `game_note`, `daily_note`, `team_page` (rewrite `## Current`, append `## History`), `weekly_note`, `scorecard_note`, `predictions_note`, `injury_log`, `home_note`.
- Tests: frontmatter parses as YAML (`yaml.safe_load` of the block); team page rewrite keeps History lines; wikilinks point to existing note names.

### Task 13: Jobs (pipelines, exactly-once)
**Files:** `jobs.py`, `tests/test_jobs.py`
- `run_once(conn, job, key, fn)` → skip if `done`; steps recorded (`collected`, `sent`); `daily(ctx, date)`, `weekly(ctx, date)`, `injury_check(ctx)`, `predict(ctx, date)`, `season_over(conn)`.
- Tests: daily on an empty slate → one-line note sent once, no Claude call; daily twice → digest sent once; ESPN dead → hoopR + digest still sent.

### Task 14: Bot + scheduler + main
**Files:** `bot.py`, `scheduler.py`, `main.py`, `tests/test_bot.py`
- Handlers: `/team /compare /whatif /game /scorecard /status /ask /player /plays /film /help`; owner filter; `ask_usage` limit; inline buttons 🔄 only for cheap commands.
- Scheduler: cron 15:15 daily, */30 15:30–07:30 injury, Mon 15:45 weekly, heartbeat every 60 s to `data/heartbeat`; `catch_up()` at startup.
- `main.py healthcheck`: heartbeat < 10 min and `SELECT 1` ok.
- Tests: non-owner update → handler returns without reply; healthcheck fails on stale heartbeat; limit enforced at 20.

### Task 15: Backtest
**Files:** `backtest.py`
- Seasons 2017–2026 from hoopR team_box; for each season replay day by day: Elo + ratings fit on games before date; game-level Brier/accuracy; title sims at Dec 1 / All-Star (Feb 15) / Apr 1 with 2,000 runs; champion = actual Finals winner from hoopR playoffs (season_type 3). Writes `data/backtest.json` + vault `Model/Backtest.md`. Tunes ridge λ and HCA on 2017–2021, reports 2022–2026 out-of-sample.

### Task 16: Docker + deploy + docs
**Files:** `Dockerfile`, `docker-compose.yml`, `env.example`, `.gitignore`, `.dockerignore`, `README.md`, `docs/vault/*`
- Image python:3.12-slim + Node 22 + `@anthropic-ai/claude-code`, tini, user 1000:10, HEALTHCHECK. Compose: scrape-net, `/fb:ro`, `./data`, vault bind, PATH with `/fb` first, mem 2g, cpus 2.
- Deploy: create NAS folder + `.env`, `docker compose up -d --build`, smoke `docker exec nba-bot python main.py daily --dry-run`, then live; GitHub private repo + nas-sync APPS entry; dry run clean.
