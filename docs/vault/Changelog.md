---
tags: [active]
updated: 2026-11-10
---
# Changelog

## 2026-11-10
- Fix: LAL showed "5-out" as its offensive system (the owner: "is 5-out for Lakers? are you sure?"). The source (Silver
  Screen and Roll, Oct 14) only says Mamukelashvili "brings the ability for LA to operate with five-out spacing": a
  one-lineup look, not the base offense. Prompt now allows a system only when the text says it is the team's base or
  core offense, and `normalise` drops system names the model did not mark as a system. Row removed.
- Play calls, not generic actions (the owner: "what type of horns? triangle etc, those type of offense play"). Knowledge
  base gains the horns family (flare, twist, rip, down, stagger, elbow, high-low, spain, pop, dho, 77), more calls
  (floppy stagger, chicago flare, elevator, pin-down flare, chin) and offensive systems as kind `system` (triangle,
  princeton, motion, 5-out motion, 4-out 1-in, flex, dribble drive, spread pick-and-roll, read and react), each with a
  description and the shots it ends in. Extraction keeps the most specific name ("horns flare", not "horns"); every
  knowledge-base name feeds `SETS`. Card offense = System · Play calls (worked/failed + effect) · "Also" generic
  actions. All 44 games and the four teams' season pieces re-read with it.
- Sets per game and why they won or lost (the owner: "find out what set plays they run and list them, that is important
  why they win or lose; search YouTube etc with Agent Reach"). Extraction (`prompts/film.md`) now also maps clearly
  described actions to standard names (two-way phrases dropped) and records `impact` (worked / failed) and `effect`
  (<= 14 words) per set and coverage: new `film_items` columns, upsert on re-read. Write-ups: two searches per game
  (team angle + "how the opponent beat / lost to"), up to 3 reads, own budget (`GAME_BUDGET`, 6 games a night). YouTube
  per-game pass now does tracked teams' games first (subtitles 429 from the home IP on 2026-11-10: stopped, no other
  backend, per Agent Reach rules). Reddit probed: public search works but returns old, fan-reaction threads; skipped.
  Card: "Won on / Lost on" (the two biggest margin factors from `insights.game_recipe`), "Sets they ran" with
  worked/failed and the effect. All 44 games re-read with the new extraction.
- Game by game grows to top teams (the owner: "slowly add for top teams"): `game_log.tracked` (meta `game_log_teams`) =
  LAL/SAS/PHI/OKC pinned + `grow()` adds the best healthy-net team not yet tracked, one per night and only when the
  write-up backlog is clear, up to `MAX_TEAMS` = 10. Each tab loads `/games/<T>.json` (data.json carries only the list).
- Game by game streamlined (the owner: "too much information, streamline it"). Each card: Offense = sets named for that
  game + "scored most on / struggled with"; Defense = man-to-man + schemes for that game + "held / gave up X pts per
  shot (league Y)" and what hurt them; tables and counters behind "Show the numbers". Season playbook and usual
  coverage listed once in the team summary (season pieces only, `film_items.game_id = ''`), not repeated per card.
- Second source check, all 34 focus-team labels incl. season ones: 23 confirmed in the text, 11 behind sites that block
  the browser (NBC Sports Philadelphia, Lakers Wire, allphly, Exa podcast pages). "two-man game" is no longer mapped
  to pick-and-roll (it can be a handoff or an isolation): `NOT_A_PLAY`, the two PHI rows removed.
- Play names checked against their sources (the owner: "are you sure the play name is correct?"). Every per-game label
  for LAL/SAS/PHI/OKC was re-read with the NAS browser: team and play matched the text for all readable sources except
  one (OKC "drop" vs SAS 10-20: the piece said defenses in general drop on Kornet; removed). NBC Sports Philadelphia,
  Lakers Wire and allphly block the browser, so 8 PHI/LAL labels stay unverified. Names were not normalised ("horn",
  "back door", "ball screen", "two-man game") and "scramble" / "veer switch" were filed as offense: new
  `social_intel.normalise` (ALIASES + side from SETS) runs on every extracted label; stored rows renamed once.
- Dashboard split into pages (the owner: "side bar or navbar, split into different pages"). Navbar in the sticky top bar:
  Today (last night, tonight, injuries, news) · Game by game · Playbooks (+ what beats what) · Styles (+ what decides
  games) · Title race (hero, standings, Cup, odds over time) · Model. Hash routing (`#games`), so links and the back
  button work; every `<section>` carries `data-page` (test enforces it, and that the page script parses).
- Game by game now leads with named plays (the owner: "offense play is horns, triangle etc; defense is man to man, 2-3,
  3-2"). Offense: named sets run this game (orange, from that game's write-up) then the season playbook (grey), each
  with an est. success from this game's shots. Defense: man-to-man base, then pick-and-roll coverage (drop, switch,
  hedge...) played this game / season / inferred drop, zone line ("none reported" unless named), with help leans
  (pack the paint, turnover-forcing...) moved to "tendencies". Shot types stay below as "how possessions ended".
- Game by game: plays and defense for LAL, SAS, PHI, OKC (the owner: "what plays and defense scheme they run for every
  match, past match till current"). New `nba/game_log.py` (`FOCUS`): every final this season, newest first, with the
  top plays run (shots, success vs league), offense leans, the defensive scheme played (inferred per game by running
  `schemes.infer` on that game's numbers ranked against every team-game, mapped to drop / pack the paint / run
  shooters off / turnover-forcing / rim protector, else man-to-man), what beats it, opponents' make rate on the shots
  that scheme gives up, and plays allowed. Named sets and coverages per game only when a write-up of that game names
  them: new `social_intel.game_articles` (Exa search + Jina read, <= 5 games a night, each game once, marked
  `art:<game_id>:<team>` in `film_games`) runs in the 21:00 film job; `prompts/film.md` now covers articles too.
  Dashboard section after Team playbooks with team tabs (`web.game_log`).
- Fix: `schemes._flat` never flattened `zone_fg`, so the "Rim protector anchored" label could never fire (SAS with
  Wembanyama at 4.3 blocks a game had none). Now included.
- Off/def/net ratings now update game by game (the owner). The dashboard reads the `ratings` table, which only the 15:15
  daily wrote, so off/def lagged a night (HOU showed 10 games at 8-3). The live sweep now refits and writes today's
  ratings + Elo whenever a new final lands (`jobs.store_ratings`, shared with the daily, which still rewrites the row
  at 15:15 with the full night and any tuning). Title odds still update at 15:15 (10k sim).
- Fix: the Cup table missed the 19 group games already played (Oct 30, Nov 6) (the owner: "Emirates NBA Cup already
  started"). Their tag was wiped when the box score was stored (the bug below), and nothing restored a stored final's
  note. `collect_date` now carries the scoreboard note into `store_game` and fills a missing note on stored finals;
  past dates were re-read once on the NAS to repair them.
- Emirates NBA Cup table on the dashboard (after Standings). ESPN tags Cup games ("NBA Cup - Group Play") but not the
  group, so `web.cup_table` finds the 6 groups as the connected sets of teams that meet in Cup games. W-L, point diff,
  points, games left; ranked by win % (teams have played different numbers of games), ties by head-to-head among
  tied teams, then diff, then points; flags "leads group" and the
  best runner-up per conference as "wild card". Row opens the team drawer.
- Fix: storing a box score wiped the game's note (ESPN summaries and hoopR have none), so a finished Cup game lost its
  Cup tag, and the Cup final would have counted in the standings. `collector.store_game` keeps the schedule's note.
- Fix: stale news (the owner: "Luka is playing tonight but dashboard shows he is out"). `tonight` was stored once at ingest
  and served for 48 h, so Nov 8 "Doncic out vs DET" items still showed "tonight · out" after he played Nov 9 and LAL
  was off. `news.recent` (dashboard, digest, Telegram pushes all read through it) now works out `tonight` from today's
  slate at read time and drops availability news about a player who has logged minutes since it was published.
- Dashboard by priority (the owner): Last night (results) first, then Tonight, then the title race hero, injuries, news,
  model record, standings, title odds over time, then the analysis sections. Scores shown, no spoiler buttons
  (`web.data` sends `spoiler: False`); Telegram still follows `SPOILER_MODE`.
- Fix: a restart during game hours backfilled last night's finished games, so they never got a FINAL card. Startup
  backfill now stops before last night's slate (`nba/scheduler.py` `startup`); the live sweep or catch-up daily owns it.
- Results game by game (the owner: "update game by game instead of waiting for all at 15:15"). New `live` job every 15 min
  01:00–14:45 SGT collects last night's US slate; each newly final game is stored, the model state bumped (dashboard and
  bot show it at once), and one spoiler-wrapped 📊 FINAL card is posted (score, pick right/wrong, factors, top player).
  ESPN only: a missing box retries next sweep (hoopR lags to ~22:15). The 15:15 daily is unchanged and still digests
  every game, with AAR, tuning, sim and Claude. Shared row builder `_result_row` in `nba/jobs.py`.

## 2026-11-09
- Fix: the Team playbooks section stayed invisible. Its scroll-reveal used `threshold: .08` (8% of the section on screen);
  with 30 taller cards the section is >12x the viewport, so 8% can never be visible. Reveals now fire on any pixel
  (`threshold: 0`, bottom margin 10%). Regression test in `tests/test_web.py`.
- Playbooks by named sets and named defenses (the owner: "playbook is not horns, triangle etc?" and "for defence shouldn't it
  be zone, 2-3, 3-2 etc?"). Team cards and the drawer now lead with **Playbook: named sets** (horns, Spain PnR, floppy,
  zipper...: times reported, sources, last seen, link) and **Defense: named schemes** grouped base (man-to-man) /
  pick-and-roll coverage (drop, switch, hedge, blitz, ice) / help (pack the paint, tag) / zone (2-3, 3-2, 1-3-1, matchup) /
  junk (box-and-one, triangle-and-two) / pressure (press, trap), each with what beats it. Est. success = the make rate
  on the shots a set usually ends in (`playbook_kb.SET_TO_PLAYS`) or a defense usually gives up (`COVERAGE_GIVES`);
  labelled with *, because public data never logs the set or defense of a possession. New `social_intel.season_videos`:
  YouTube search + description read (no subtitles) for this season's breakdowns, one offense and one defense query
  per team, in the nightly film rotation; "Film to watch" links. Article search now asks for zone / drop coverage too.
  `/plays` shows named sets and defensive schemes.
- Top 5 plays with success % (the owner: "top 5 plays they normally run and the percentage that the play succeed"):
  `nba/plays.py` turns every shot into a play (how it was created + where it ended: pull-up 3, drive to the rim, spot-up
  corner 3, cut, lob, post-up, putback...) with usage, success % (makes / attempts), points per shot, league comparison,
  assisted share and a 1-30 rank; ATO top plays and how ATO possessions end; top plays allowed on defense. Team cards
  show the top 5, the drawer shows every play (offense, ATO, defense), `/plays` lists the top 5. Named sets keep "reported"
  only: public data does not tag possessions with sets, so no success rate for them. News spoiler regex fixed (a heredoc
  had turned `` into backspaces); news subject-only status flag.
- News desk (the owner: "news that might affect the game"): `nba/news.py` + `prompts/news.md`. Sources: ESPN injury blurbs,
  ESPN news feed (recaps skipped), CBS Sports NBA RSS, one Agent Reach web search per team ~3 h before its tip.
  Rotowire not used (robots.txt). Claude haiku tags kind / teams / players / implied status and paraphrases (<= 25 words);
  priced with player values; flagged "ahead of the injury report" when news says out / doubtful before the official
  feed does. Runs with every injury check and before the digest; pushes one batched Telegram card only for tonight's
  top-3 players or contenders (first run seeds silently). Dashboard "News that matters" (All / Tonight / Ahead filters),
  team drawer news, digest block. News never moves the model.
- "What decides games" (the owner picked all ten ideas): `nba/insights.py` (exact win-recipe split of every margin into
  shooting / turnovers / offensive rebounds / free throws; shot quality = expected pts per shot from type + zone vs
  actual, offense and defense luck; rest and travel per game from the schedule and arena coordinates; clutch; quarter
  margins; halftime shot-mix shift; points off turnovers / fast break / paint; referee crews (officials from ESPN +
  hoopR backfill); betting-line movement from opening vs current lines stored in `odds_snap`), `nba/lineups.py`
  (5-man units from substitutions, 97% of game time covered, on/off, closing five, bench units, with/without stars).
  Dashboard: new section + tonight card context + "won on" and refs for last night + drawer sections. Telegram:
  "won on" per result, rest / line-move flags on tonight's picks. Rest and travel were added to the model grid and
  backtested on 2017-2021: the tuner kept both at 0 (no out-of-sample gain beyond back-to-backs), so they stay display-only.
- Telegram digest: "Matchup keys" for tonight's games (each side's best way to win = the opponent's top measured
  weakness + the plays that attack it; never dropped, splits into a 2nd message on busy nights), dashboard link
  (`DASHBOARD_URL`), `/help` links the dashboard, `/team` has "How to beat them". Cleared the job_runs row left by the
  manual 14:35 film test so the 21:00 film room runs tonight.
- "How to beat them" (the owner: "state teams weakness and how to exploit them"): `nba/scout.py` ranks each team on 14
  measured weak spots (defense: rim, rim FG, threes, corners, mid-range, ball pressure, glass, fouls, transition;
  offense: turnovers, 3P%, rim rate, ball movement, second chances), one-creator reliance, shot types that score well
  against them, styles they lose to, poor ATO, and 3P luck. Worst seven in the league = weakness; each has evidence and
  exploit plays (chips open the explainer). Top-10 defenses get a "trade-off" note on conceded threes. Top 3 on every
  playbook card, full report in the drawer and `/team`.
- Fix (the owner: "team playbooks East and West is incorrect"): the Styles filter wired `.seg button` page-wide, so it
  overwrote the playbook buttons' handlers; East/West on the playbooks filtered the Styles table instead. Scoped each
  filter to its own section; test guards against page-wide `.seg` selectors. Explainer picker: stricter context for
  "chicago" and "lob" (had matched a Bulls highlight and a point-guard lesson).
- Play explainers + "What beats what" (the owner: "add youtube to understand the play", "what play break what type of
  defences"): `nba/playbook_kb.py` (coaching definitions for ~50 sets / actions / coverages, standard counters per
  defense), `schemes.counter_evidence()` (shot types that score best vs teams with each lean or reported coverage, pts/shot
  vs league), `social_intel.play_videos()` (one YouTube explainer per play via Agent Reach search only, cached in
  `play_videos`). Every play chip opens a panel: definition, embedded video (youtube-nocookie), what beats it / what it
  beats, teams running it. Team cards get "How to attack it".
- Team playbooks (the owner: "what they normally run at ATO or in every offence and defence"): new "Team playbooks"
  section, one card per team (East / West filter, sort by net / off / def / ATO rank): offense play-type mix vs league,
  ATO block, defense lean, named sets. `nba/ato.py` measures after-timeout possessions from pbp (first real possession
  after a timeout, points until the other team gets the ball; own-timeout share, late-game split, shot look, go-to
  players; 1,579 possessions league-wide, 1.03 pts each). Team drawer gets an ATO section. Film room gets a second
  reported source: written scheme articles via Agent Reach web (Exa search + Jina read), 6 teams a night in rotation,
  `prompts/film_season.md`; extraction now flags ATO sets.
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
