import re

from nba import telegram_pub as tp


def _night(n):
    codes = "ATL BOS BKN CHA CHI CLE DAL DEN DET GS HOU IND LAC LAL MEM MIA".split()
    games = [{"matchup": f"{codes[i]} @ {codes[-1 - i]}", "score": "121-127", "ot": 1, "pick_right": i % 2 == 0,
              "stats_line": "pace 101 · eFG 55%/52% · TOV 12/15 · REB +6", "top_line": "Somebody Long-Name 41p 9r 7a"}
             for i in range(n)]
    watch = [{"label": f"{codes[i]} @ {codes[-1 - i]}", "stars": 1 + i % 5, "reasons": ["late swing", "overtime"]} for i in range(n)]
    return games, watch


def _digest(n, spoiler=True):
    sp = tp.Spoiler(spoiler)
    games, watch = _night(n)
    blocks = [("watch", tp.worth_watching(watch)), ("results", tp.results(games, sp)),
              ("impact", tp.impact_block([{"label": g["matchup"], "best": [("A Player", "X", 0.31)],
                                            "worst": [("B Player", "Y", -0.22)]} for g in games], sp)),
              ("movers", tp.movers_block([("BOS", 2.1, 8.4), ("NY", -1.7, 3.2)] * 5, sp)),
              ("title", tp.title_block([{"team": "SA", "model": 0.2, "market": 0.25, "delta": 0.01}] * 5, sp)),
              ("verdict", tp.verdict_block(["line one " * 10] * 5, sp)),
              ("tonight", tp.tonight_block([{"home": "BOS", "away": "NY", "home_wp": 0.6, "market_wp": 0.55,
                                             "tip_sgt": "08:30"}] * n)),
              ("record", tp.record_block({"n": 50, "wins": 33, "acc": 0.66, "brier": 0.21}))]
    return tp.fit(blocks, tp.title("digest", "NBA DAILY", "Mon 9 Nov"))


def test_digest_never_over_limit():
    for n in (1, 8, 15):
        msgs = _digest(n)
        assert all(len(m) <= tp.LIMIT for m in msgs), [len(m) for m in msgs]


def test_busy_night_drops_movers_first():
    msgs = _digest(12)
    joined = "\n".join(msgs)
    if len(msgs) == 1:
        assert "Net rating movers" not in joined


def test_worth_watching_has_no_scores_or_winners():
    _, watch = _night(10)
    block = tp.worth_watching(watch)
    body = block.split("\n", 1)[1]
    assert not re.search(r"\d", body)                        # no score, margin or count of any kind
    assert "<tg-spoiler>" not in block


def test_spoiler_off_and_escaping():
    msgs = _digest(3, spoiler=False)
    assert all("tg-spoiler" not in m for m in msgs)
    assert "&lt;script&gt;" in tp.results([{"matchup": "<script>", "score": "1-2", "pick_right": None}], tp.Spoiler(True))
    assert tp.plain("<b>a &amp; b</b>") == "a & b"
