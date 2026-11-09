"""Backtest the same method on the last 10 seasons, point in time (only games before each date count).

Tunes params on 2017-2021 and checks them on 2022-2026. If the tuned params also win out of sample, they
become the live starting params (model_params row). Injury adjustments are not tested: no injury history.
"""
import json
import logging
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

import numpy as np
from scipy.special import ndtr

from . import elo as elo_mod
from . import fallback, ratings, simulate, tuning
from . import obsidian_pub as ob

log = logging.getLogger(__name__)
SEASONS = list(range(2017, 2027))           # 2016-17 .. 2025-26
TRAIN, TEST = SEASONS[:5], SEASONS[5:]
CHECKPOINTS = ("12-01", "02-15", "04-01")


def _metrics(a, p):
    m = a["base"] + p.hca * (1 - a["neutral"]) - p.b2b * a["b2b_h"] + p.b2b * a["b2b_a"] + p.rest * a["rest_edge"] + p.travel * a["tz_edge"]
    pr = np.clip(ndtr(m / p.sigma), 1e-6, 1 - 1e-6)
    y = a["y"]
    return {"n": int(len(y)), "acc": float(np.mean((pr >= 0.5) == (y == 1))), "brier": float(np.mean((pr - y) ** 2)),
            "logloss": float(-np.mean(y * np.log(pr) + (1 - y) * np.log(1 - pr))),
            "mae": float(np.mean(np.abs(a["margin"] - m)))}


def _cat(arrs):
    return {k: np.concatenate([a[k] for a in arrs]) for k in arrs[0]}


def _champion(res):
    po = res[res.season_type == 3]
    if po.empty:
        return None
    last = po.iloc[-1]
    return last.home if last.home_pts > last.away_pts else last.away


def _title_checks(res, prior, p, season):
    reg = res[res.season_type == 2]
    out = []
    champ = _champion(res)
    for md in CHECKPOINTS:
        yr = season - 1 if md >= "07" else season
        cut = f"{yr}-{md}"
        past, future = reg[reg.date < cut], reg[reg.date >= cut]
        if past.empty or future.empty:
            continue
        R = ratings.current(past, prior, p, cut)
        lg = sum(R[t]["pace"] for t in ratings.TEAMS) / 30
        s = {t: R[t]["net"] * lg / 100 for t in ratings.TEAMS}
        wins = {t: 0 for t in ratings.TEAMS}
        for g in past.itertuples():
            wins[g.home if g.home_pts > g.away_pts else g.away] += 1
        sim = simulate.simulate(s, s, wins, {}, [(g.home, g.away, False) for g in future.itertuples()], p.hca, p.sigma,
                                n=2000, seed=season)
        probs = {t: v["title"] for t, v in sim.items()}
        fav = max(probs, key=probs.get)
        out.append({"checkpoint": cut, "favourite": fav, "fav_pct": probs[fav], "champion": champ,
                    "champion_pct": probs.get(champ, 0.0), "hit": fav == champ,
                    "brier": sum((probs[t] - (t == champ)) ** 2 for t in probs)})
    return out


def run(ctx=None, adopt=True) -> dict:
    results = {s: fallback.season_results(s) for s in [SEASONS[0] - 1] + SEASONS}
    priors = {s: ratings.prior_from_season(results[s - 1][results[s - 1].season_type == 2]) for s in SEASONS}
    # Elo baseline, seeded from the 538 archive at the end of 2015-16
    arc = elo_mod.load_archive(Path(ctx.s.data_dir) / "nba_elo.csv" if ctx and (Path(ctx.s.data_dir) / "nba_elo.csv").exists()
                               else elo_mod.ARCHIVE_URL)
    er = elo_mod.seed_from_archive(arc, 2016)
    elo_m = {}
    for s in SEASONS:
        df = results[s]
        er_new, pre = elo_mod.replay(er, df)
        y = (df.home_pts > df.away_pts).to_numpy(float)
        pr = np.array([elo_mod.prob(h, a) for _, h, a in pre])
        reg = (df.season_type == 2).to_numpy()
        elo_m[s] = {"acc": float(np.mean((pr[reg] >= 0.5) == (y[reg] == 1))), "brier": float(np.mean((pr[reg] - y[reg]) ** 2))}
        er = er_new
    base = tuning.Params()
    arrays = {}
    for s in SEASONS:
        reg = results[s][results[s].season_type == 2]
        for hl in tuning.GRID["half_life"]:
            arrays[(s, hl)] = tuning.walk_forward_arrays(reg, priors[s], replace(base, half_life=hl))
        log.info("backtest walk-forward %s done", s)
    # tune on the first five seasons
    best, best_ll = base, None
    for c in tuning.candidates(base):
        ll = _metrics(_cat([arrays[(s, c.half_life)] for s in TRAIN]), c)["logloss"]
        if best_ll is None or ll < best_ll:
            best, best_ll = c, ll
    test_default = _metrics(_cat([arrays[(s, base.half_life)] for s in TEST]), base)
    test_tuned = _metrics(_cat([arrays[(s, best.half_life)] for s in TEST]), best)
    chosen = best if test_tuned["logloss"] < test_default["logloss"] else base
    per_season = []
    for s in SEASONS:
        m = _metrics(arrays[(s, chosen.half_life)], chosen)
        checks = _title_checks(results[s], priors[s], chosen, s)
        per_season.append({"season": f"{s - 1}-{str(s)[2:]}", "games": m, "elo": elo_m[s], "title": checks,
                           "champion": _champion(results[s])})
    title_rows = [c for x in per_season for c in x["title"]]
    out = {"ran": date.today().isoformat(), "default": asdict(base), "tuned_on_2017_2021": asdict(best),
           "chosen": asdict(chosen), "test_2022_2026": {"default": test_default, "tuned": test_tuned},
           "all_seasons": _metrics(_cat([arrays[(s, chosen.half_life)] for s in SEASONS]), chosen),
           "elo_all": {"acc": float(np.mean([elo_m[s]["acc"] for s in SEASONS])),
                       "brier": float(np.mean([elo_m[s]["brier"] for s in SEASONS]))},
           "title": {cp: {"hits": sum(c["hit"] for c in title_rows if c["checkpoint"].endswith(cp)),
                          "n": sum(1 for c in title_rows if c["checkpoint"].endswith(cp)),
                          "brier": float(np.mean([c["brier"] for c in title_rows if c["checkpoint"].endswith(cp)] or [0]))}
                     for cp in CHECKPOINTS},
           "seasons": per_season}
    if ctx:
        Path(ctx.s.data_dir, "backtest.json").write_text(json.dumps(out, indent=1, default=str))
        _note(ctx, out)
        if adopt and chosen != tuning.current(ctx.conn) and not tuning.history(ctx.conn, 1):
            tuning.save(ctx.conn, chosen, test_default["logloss"], test_tuned["logloss"],
                        "backtest: tuned on 2017-2021, validated on 2022-2026")
    return out


def _note(ctx, out):
    rows = []
    for s in out["seasons"]:
        g, e = s["games"], s["elo"]
        t = {c["checkpoint"][5:]: c for c in s["title"]}
        rows.append([s["season"], g["n"], f"{g['acc']:.1%}", f"{g['brier']:.3f}", f"{e['acc']:.1%}", f"{e['brier']:.3f}",
                     s["champion"]] + [f"{t[cp]['favourite']} {t[cp]['champion_pct']:.0%}" + (" ✓" if t[cp]["hit"] else "")
                                      if cp in t else "–" for cp in CHECKPOINTS])
    a, el, td = out["all_seasons"], out["elo_all"], out["test_2022_2026"]
    body = [f"# Backtest (ran {out['ran']})", "",
            "Same method as live, point in time: each game is predicted from ratings fit only on earlier games, with the "
            "preseason prior from the season before. Injury adjustments are not tested (no injury history).", "",
            f"**All 10 seasons:** {a['acc']:.1%} picks right, Brier {a['brier']:.3f}, log loss {a['logloss']:.3f}, "
            f"margin error {a['mae']:.1f} · Elo alone {el['acc']:.1%}, Brier {el['brier']:.3f}", "",
            f"**Out of sample (2022-2026):** default params log loss {td['default']['logloss']:.4f} vs tuned "
            f"{td['tuned']['logloss']:.4f} → chosen `{json.dumps(out['chosen'])}`", "",
            "**Champion picked (model favourite):** " + " · ".join(
                f"{cp}: {v['hits']}/{v['n']} (title Brier {v['brier']:.3f})" for cp, v in out["title"].items()), "",
            "Title columns: model favourite at that date, then the probability it gave the eventual champion (✓ = favourite won).", "",
            ob.table(["Season", "Games", "Acc", "Brier", "Elo acc", "Elo Brier", "Champion", "Dec 1", "Feb 15", "Apr 1"], rows)]
    ctx.vault.write("Model/Backtest.md", {"updated": out["ran"], "tags": ["model"]}, "\n".join(body))
