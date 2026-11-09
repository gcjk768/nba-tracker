---
tags: [active]
updated: 2026-11-09
---
# Changelog

## 2026-11-09
- Dashboard: animated web page on NAS port 8795 (`nba/web.py`, `nba/dashboard.html`, stdlib server, inline SVG + vanilla
  JS, no new deps): title-race replay, odds over time, power rankings, tonight vs market, last night behind a reveal,
  calibration, AAR causes, model changes. Palette validated (dataviz skill) light + dark. `python -m nba history` fills
  past days so the race starts on opening night (also runs at startup). Claude Motion / Claude Dashboards checked:
  neither can be fed live from the NAS (Motion = MP4 export; Dashboards needs a cloud warehouse) — `/data.json` can be
  attached to a Claude Motion chat for a one-off video.
- Tuning now waits for 300 graded games: on 146 games the walk-forward search "adopted" changes the 10-season
  backtest rejects (noise). AAR still runs on every miss from day one.
- Deployed to the NAS (`/volume1/docker/nba-tracker`, container healthy, 146 games backfilled); GitHub `gcjk768/nba-tracker`
  (private) via nas-sync. Bot @gcjk_nbatracker_bot created in BotFather, admin in the owner Channel (delete/pin/manage topics
  only), topic "🏀 NBA" (thread 16211); hello card posted.
- Security fix: httpx logged Telegram request URLs (bot token) at INFO; silenced httpx/httpcore and rotated the token
  (/revoke). Token handled via clipboard → `token.txt` → `set_token.py`, never printed.
- Smoke run on live data found: daily didn't collect last night if the schedule feed failed; tonight's slate missed when the
  schedule was empty; Pythagorean overflow; daily didn't snapshot injuries before predicting. All fixed.
- Probed data sources: NBA.com (stats, cdn, injury report) blocked from the home IP even via the NAS Playwright
  server; switched to ESPN + hoopR (the owner approved). NBA.com-only stats dropped.
- Phase 1 built (54 tests): ESPN/hoopR collector, excitement + WPA, Elo, ratings, simulation, frozen predictions,
  scorecard, injuries + alerts, Claude analyst, Telegram digest, vault writer, pipelines, bot, scheduler, backtest.
- Added AAR + self-tuning on every missed pick (the owner's request).
- Restructured to the fleet layout (`nba/` package, `python -m nba`, compose/deploy like supermarket-hunter / cve-watch).
- Backtest 2016-17..2025-26: 65.3% picks (Elo 64.7%), Brier 0.217; out of sample tuned 66.0%; champion favourite
  hit 4/9 (Dec 1), 4/10 (Feb 15), 3/10 (Apr 1). Tuned params became the defaults.
