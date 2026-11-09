# NBA Season Tracker — Phase 1 design (2026-11-09)

Source brief: the owner's "NBA Season Tracker and Champion Predictor: Build Prompt" (updated 2026-11-09).
Approved decisions: **ESPN + hoopR data plan**, **3 phases**, this Phase 1 design.

## 1. Why the data plan differs from the brief

Probed 2026-11-09 from the home network:

| Source | PC (requests/curl) | NAS shared Playwright (real Chromium) |
|---|---|---|
| stats.nba.com (`nba_api`) | timeout | timeout |
| www.nba.com, cdn.nba.com | 403 Access Denied | 403 Access Denied |
| official.nba.com injury report | 403 | – |
| ESPN site/core JSON API | 200 | – |
| `sportsdataverse/hoopR-nba-data` | updated daily (~22:13 SGT) | – |

The home IP is blocked by NBA.com. Fleet rule: a block means stop, no proxy/VPN/stealth/spoofed UA.
So: **ESPN is primary** (live), **hoopR is the fallback and the history source**. NBA.com-only stats
(Synergy play types, `BoxScoreMatchupsV3`, hustle, tracking) are **dropped**. Phase 2 scheme labels
are `inferred` (from ESPN pbp) or `reported` (Phase 3 film room); there is no `measured` play-type tier.
`basketball_reference_web_scraper` is dropped (redundant; Sports Reference rate-limits bots).

ESPN endpoints used (no key):
- `site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD` — slate, status, tip time, spread
- `.../summary?event=<id>` — boxscore (team + player), plays (text, type, coordinate, participants, clock), winprobability per play, pickcenter/odds, injuries
- `.../injuries` — league injury list (status, date, comments)
- `sports.core.api.espn.com/v2/sports/basketball/leagues/nba/seasons/2027/futures` — title futures (DraftKings)

## 2. Phases

- **Phase 1 (this spec):** collect, store, ratings + Elo, simulation, frozen predictions + scorecard,
  excitement + WPA, injuries + alerts, Claude daily/weekly verdict, Telegram digest + core commands,
  Obsidian vault, backtest, Docker deploy.
- **Phase 2:** lineups / on-off from ESPN substitutions, inferred schemes (`schemes.py`), player scouting
  + archetypes (`players.py`), shot charts, similarity, `/plays`, full `/player`.
- **Phase 3:** film room (`social_intel.py`, Agent Reach YouTube first), `/film`.

## 3. Fleet rules that override the brief

- Claude auth: `ANTHROPIC_API_KEY` + `/volume1/docker/claude-fallback` wrapper mounted at `/fb`
  (first on PATH), not `CLAUDE_CODE_OAUTH_TOKEN`. Models: `claude-sonnet-5-5` daily/weekly, `claude-haiku-5-5` `/ask`.
- Vault: host `/volume1/<USER>/Obsidian/NBA 2026-27/` → `/vault`. Fleet layout (`Home.md`,
  `Activity/YYYY/MM/YYYY-MM-DD.md`) plus the brief's folders; date-heavy folders get `YYYY/MM/`.
- Network: `scrape-net` (no free address pools). Container user `1000:10`, umask 002.
- Telegram: fleet card style (HTML, escaped values, header + blocks, `━━━` dividers, status markers,
  plain-text resend on 400). Destination: new topic "NBA" in the owner Channel (ids in NAS `.env`).
- Code: private repo `gcjk768/nba-tracker`, shipped with nas-sync (`sync.py --push nba-tracker`).

## 4. Modules (Phase 1)

| File | Purpose |
|---|---|
| `config.py` | env → settings (paths, ids, flags, models, limits) |
| `db.py` | SQLite schema + migrations, WAL, one shared connection with a lock |
| `espn.py` | throttled httpx client; parse scoreboard / summary / injuries / futures into rows |
| `fallback.py` | hoopR parquet loader (schedules, team_box, player_box) for a date or a season |
| `collector.py` | `collect(date)`, `backfill(season)`: ESPN first, 3 failures → hoopR + warning; idempotent |
| `injuries.py` | snapshot ESPN injuries, diff vs previous, store status changes |
| `elo.py` | 538 archive (≤2024-25) + replay of later results; `elo_prob(home, away)` |
| `ratings.py` | ridge opponent-adjusted off/def/net per 100, garbage time filtered, prior blend, injury adjustment |
| `simulate.py` | numpy Monte Carlo: rest of season, play-in, playoffs; title/finals/conf odds |
| `predictions.py` | predict a slate; append-only versions; `graded_version(game)` = last before tip |
| `scorecard.py` | accuracy, Brier, log loss, calibration buckets, vs Elo, vs market |
| `excitement.py` | watchability score + reason from ESPN WP; per-player WPA |
| `aggregator.py` | season aggregates, rolling nets, SOS, luck flags; compact JSON for Claude |
| `analyst.py` | `claude -p` call, pydantic validation, one retry, stats-only fallback |
| `telegram_pub.py` | HTML builders, digest fitting (≤4000), split between blocks, send/sendPhoto |
| `obsidian_pub.py` | Daily / Games / Teams / Weekly / Model / Injuries / Activity notes |
| `jobs.py` | daily / weekly / injury_check / predict pipelines; `job_runs` exactly-once |
| `bot.py` | python-telegram-bot handlers, owner-only, daily limits |
| `scheduler.py` | AsyncIOScheduler (Asia/Singapore) + startup catch-up + heartbeat |
| `backtest.py` | 10 seasons from hoopR, point-in-time; saves `Model/Backtest.md` |
| `main.py` | `serve daily weekly backfill backtest predict injury_check healthcheck` |

Prompts: `prompts/daily.md`, `prompts/weekly.md`, `prompts/ask.md`.

## 5. Data model (SQLite)

- `games(game_id PK, date, season, season_type, home, away, tip_utc, status, home_pts, away_pts, ot, source, neutral)`
- `team_box(game_id, team, opp, is_home, pts, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, ast, tov, stl, blk, pf, poss, garbage_poss, …, PK(game_id, team))`
- `player_box(game_id, team, player_id, name, starter, minutes, pts, reb, oreb, dreb, ast, stl, blk, tov, pf, fgm, fga, fg3m, fg3a, ftm, fta, plus_minus, dnp_reason, PK(game_id, player_id))`
- `win_prob(game_id, seq, period, clock_s, home_wp, home_score, away_score, player_id, team, text)` (one row per play)
- `game_excitement(game_id PK, score, stars, reasons_json)`; `player_wpa(game_id, player_id, team, wpa)`
- `injury_snap(ts, player_id, name, team, status, detail)`; `injury_change(id, ts, player_id, name, team, old, new, detail, alerted)`
- `ratings(date, team, off, def, net, prior_net, blend_w, healthy_net, PK(date, team))`; `elo(date, team, elo)`
- `sims(ts, kind, team, make_playoffs, conf_finals, finals, title, PK(ts, kind, team))`
- `predictions(id PK, game_id, version, created_utc, home_wp, margin, total, elo_wp, market_wp, reason)` — triggers forbid UPDATE/DELETE
- `futures(ts, team, american, implied)`; `verdicts(date, kind, json)`; `job_runs(job, key, status, started, finished, PK(job, key))`; `ask_usage(day, kind, n)`

## 6. Model rules (Phase 1)

- **Possessions** = FGA − OREB + TOV + 0.44·FTA (team). **Garbage time**: a play in Q4 with margin
  ≥ 25 under 8:00, ≥ 20 under 5:00, ≥ 15 under 2:00 ends the clean window; efficiency uses clean possessions
  where pbp exists, the full game otherwise.
- **Rating**: ridge regression of per-100 margin on home/away team indicators with HCA term, λ tuned in backtest.
  Prior = last season's final net × 2/3. Blend weight on current season w = clip(0.30 + 0.50·(g−10)/(55−10), 0, 0.8)
  for g ≥ 10 games, w = 0.30·g/10 below 10 (≈30/70 at 10 games → 80/20 by the All-Star break, ~55 games).
- **Luck**: opponent 3P% regressed 50% to league mean before it feeds ratings; close-game record vs Pythagorean flagged.
- **Injury adjustment**: player value = minutes-weighted box value (per-36 game score − league mean, shrunk by minutes)
  × projected minutes / 48; out players subtract their value, questionable ×0.5, doubtful ×0.75.
  `ponytail:` box-score proxy, replaced by on/off in Phase 2.
- **Game prediction**: margin = home_net − away_net + HCA (+ rest: −1.5 for a back-to-back); win prob = Φ(margin/12).
  Total = (pace_h+pace_a)/2 × (off_h+def_a + off_a+def_h)/200. Elo prob and market prob stored beside it.
- **Simulation**: each remaining game draws from the same margin model; play-in 7–10; playoffs best-of-7 with HCA;
  tiebreak by random draw (`ponytail:` simplified tiebreaks). 10,000 runs weekly and daily, 2,000 for alerts.
- **Market**: ESPN futures American odds → implied, normalised to sum 1.

## 6b. After Action Review on every missed pick (the owner, 2026-11-09)

"Every failed prediction: review what failed and improve the model for the next match; AAR in the summary."

- `review.py` — for each graded game where the pick lost (or |margin error| ≥ 15), break the error
  (actual − predicted margin, from the picked side) into causes, each in points:
  `availability` (players out/limited who were expected to play: box value × lost minutes),
  `shooting luck` (3P% and opp 3P% vs season means × attempts × 3), `turnovers` and `rebounding`
  (vs season rates × possessions × league pts/poss), `rest` (b2b not priced), `rating` (the team's last-10 residual
  trend), `noise` (what is left). Largest cause = verdict. Stored in `aar(game_id PK, json)`.
- `tuning.py` — after each graded slate, re-fit model params on all graded games this season:
  `hca`, `sigma`, `b2b`, `half_life` (recency weight in the ridge), `injury_scale`. Grid search, walk-forward
  (each game scored with params fit only on earlier games). A candidate is **adopted only if it lowers the
  season log loss by ≥ 0.002** vs the current params; else "no change — noise". Every adoption is a new
  `model_params` version (append-only) with before/after log loss and the reason. Claude never sets numbers;
  it writes the lesson text only.
- Digest: section "🔍 AAR" after Results: one line per miss (`pick 64% → lost · main cause · model change`).
  Weekly: tuning history + which causes dominate. Vault: `Model/AAR/YYYY/MM/YYYY-MM-DD.md` + an "AAR" block in the game note.
- Honest limit: ~68–70% pick accuracy is the market's ceiling; the goal is measurable improvement, not certainty.

## 7. Jobs and schedule (SGT)

- 15:15 daily: collect yesterday's US slate → excitement/WPA → ratings → grade → sim (10k) → predict today's slate
  → aggregate → Claude daily → Telegram digest → Obsidian. No games: one-line note, no Claude call.
- every 30 min 15:30–07:30: injury check → changes → re-predict affected games (new version) → alert if contender
  (top 8 by title odds) top-3 player (by value) and Δtitle ≥ 1pp or Δgame ≥ 5pp; batched; each change alerted once.
- Monday 15:45: full sim, Claude weekly (30 teams), weekly report + Weekly note + Scorecard note.
- Startup: migrate → backfill missing days → catch-up of any job whose slot passed today without a `done` row → serve.
- Exactly-once: `job_runs(job, key)` row inserted `running` before work; digest send step records `sent`; reruns skip sent steps.
- Season end: when the Finals series is complete, post the wrap once and stop scheduling.

## 8. Telegram

Digest sections in the brief's order; Phase 2 sections (tactics lines, player-impact verdicts beyond WPA) degrade to
what exists. Fitting: build all blocks → if > 4000 drop "movers", then "record", then shorten lines, then split
between blocks. `SPOILER_MODE=off` removes `<tg-spoiler>`. "Worth watching" is built only from excitement score +
reason codes (no team score/margin/winner fields reach that builder).
Commands: `/team /compare /whatif /game /scorecard /status /ask /player` (basic) `/plays /film` (stub).
Owner-only (`TELEGRAM_OWNER_ID`), `/ask` + `/whatif` share `ASK_DAILY_LIMIT` (20/day).

## 9. Errors

Any job exception → Telegram failure card + Activity line; the next scheduled run retries. Vault I/O best-effort.
Claude failure → stats-only digest. ESPN failure ×3 → hoopR + warning. Telegram 400 → plain-text resend.

## 10. Acceptance (Phase 1 subset)

Backfill no duplicates; no-game day clean; digest ≤ 4000; valid frontmatter/links; invalid Claude → stats-only;
backtest champion hit rate + Brier for 10 seasons; predictions immutable, graded version = last before tip;
ESPN blocked → hoopR fallback, digest still sent; spoiler mode never leaks; one alert per change; other users ignored;
restart mid-run → no double digest, missed job runs once; bot answers during daily job; healthcheck fails if
scheduler stops; WPA per game sums to winner's final − start WP.
