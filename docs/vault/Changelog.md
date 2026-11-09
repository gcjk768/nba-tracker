---
tags: [active]
updated: 2026-11-09
---
# Changelog

## 2026-11-09
- Probed data sources: NBA.com (stats, cdn, injury report) blocked from the home IP even via the NAS Playwright
  server; switched to ESPN + hoopR (the owner approved). NBA.com-only stats dropped.
- Phase 1 built (54 tests): ESPN/hoopR collector, excitement + WPA, Elo, ratings, simulation, frozen predictions,
  scorecard, injuries + alerts, Claude analyst, Telegram digest, vault writer, pipelines, bot, scheduler, backtest.
- Added AAR + self-tuning on every missed pick (the owner's request).
- Restructured to the fleet layout (`nba/` package, `python -m nba`, compose/deploy like supermarket-hunter / cve-watch).
- Backtest 2016-17..2025-26: 65.3% picks (Elo 64.7%), Brier 0.217; out of sample tuned 66.0%; champion favourite
  hit 4/9 (Dec 1), 4/10 (Feb 15), 3/10 (Apr 1). Tuned params became the defaults.
