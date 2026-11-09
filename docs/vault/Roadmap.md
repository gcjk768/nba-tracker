---
tags: [active]
updated: 2026-11-09
---
# Roadmap

- [x] Phase 1 spec, plan, build, backtest
- [ ] Deploy to the NAS + GitHub `gcjk768/nba-tracker` (private) via nas-sync
- [ ] the owner: create the bot in @BotFather → token via `set_token.py nba-tracker`; add it to the owner Channel as admin
- [ ] Create the "NBA" topic, put chat/thread ids + `ALLOWED_USER_IDS` in the NAS `.env`, recreate
- [ ] Phase 2: lineups + on/off from ESPN substitutions, inferred schemes (`schemes.py`), player scouting +
      archetypes (`players.py`), shot charts, similarity, `/plays`, full `/player`
- [ ] Phase 3: film room (Agent Reach YouTube first), `/film`
- [ ] Prior: blend in preseason market win totals (results-only prior misses offseason roster moves, e.g. CLE vs CHA on 2026-11-09)
- [ ] Later: real NBA tiebreak rules in the sim; market odds per game in the backtest
