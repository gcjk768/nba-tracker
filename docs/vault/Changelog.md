---
tags: [active]
updated: 2026-11-09
---
# Changelog

## 2026-11-09
- Dashboard split by conference (the owner): "Standings by conference" East | West tables with seeds, playoff / play-in /
  lottery bands, GB, net, playoff and title odds (replaces the tier chips); All / East / West filter on "Styles and
  ranks" (table rows hide, map logos of the other conference fade). Fixed a CSS class clash (`.conf` was already the
  confidence meter) that collapsed the new tables.
- Phase 3 film room (the owner: "use YouTube and Agent Reach for sets like horns"): `nba/social_intel.py` wraps Agent Reach
  (YouTube search / read / subtitle, v1.6.1 from the GitHub release in the image). Nightly 21:00 SGT over the last
  3 days of games: postgame pressers + breakdown channels, upload date must be game day to +3, subtitles go to
  `claude-haiku-5-5` with `prompts/film.md` (named sets / actions / coverages only, JSON). Stored: label, <= 2-sentence
  paraphrase, link; never transcripts. Budget 12 searches / 12 reads / 8 subtitles, spaced; the first RATE_LIMITED /
  PLATFORM_BLOCKED / NEED_LOGIN stops the pass (YouTube returned 429 after 8 quick calls in testing). Reported labels
  are compared with inferred ones (agree / contradict shown, never silently merged); Telegram follow-up only on a
  contradiction. Team drawer "Film room" + season playbook counts; `/film BOS`.
- Dashboard redesign (design-taste + dataviz skills): broadcast-style dark-first page, Geist font self-hosted
  (`nba/static/`, OFL), team logos and colours from ESPN cached in `data/` (`/logo/<TEAM>.png`), hero favourite +
  team-colour bar-chart race, matchup cards, spoiler-safe last night, tiered standings, model strip.
- Phase 2 pulled forward (the owner: "show all stats"): `nba/schemes.py` (shot type, zone, assisted, transition from ESPN
  pbp; team offense/defense profiles with league percentiles; inferred scheme labels with confidence + evidence;
  record vs opponent styles; what-wins correlations), `nba/players.py` (status, role changes, per-player shot diet,
  last 5 with WPA). Dashboard: "Who's not at full strength", "Styles and ranks" (offense vs defense map, sortable
  style table, what's winning), team drawer (offense, defense, record vs styles, roster with player detail).
  Telegram `/plays` now answers with the same profile. Switching / zone / coverage type stay "not measurable".
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
