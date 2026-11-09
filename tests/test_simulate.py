import itertools

from nba import simulate as sim


def _remaining():
    return [(h, a, False) for h, a in itertools.permutations(sim.TEAMS, 2)][:400]


def test_probabilities_are_consistent():
    s = {t: 0.0 for t in sim.TEAMS}
    s["BOS"] = 15.0
    out = sim.simulate(s, s, {}, {}, _remaining(), 2.5, 12.0, n=3000, seed=7)
    assert abs(sum(v["title"] for v in out.values()) - 1) < 1e-9
    assert abs(sum(v["finals"] for v in out.values()) - 2) < 1e-9
    assert abs(sum(v["playoffs"] for v in out.values()) - 16) < 1e-9
    assert abs(sum(v["conf_finals"] for v in out.values()) - 4) < 1e-9
    assert out["BOS"]["title"] > 0.5
    assert all(v["title"] <= v["finals"] <= v["conf_finals"] <= v["playoffs"] for v in out.values())


def test_deterministic_with_seed():
    s = {t: i * 0.3 for i, t in enumerate(sim.TEAMS)}
    a = sim.simulate(s, s, {}, {}, _remaining(), 2.5, 12.0, n=500, seed=1)
    b = sim.simulate(s, s, {}, {}, _remaining(), 2.5, 12.0, n=500, seed=1)
    assert a == b


def test_series_prob():
    assert abs(sim.series_prob(0, 0, 0, 12) - 0.5) < 1e-12
    assert sim.series_prob(5, 0, 2.5, 12) > 0.75
    assert sim.series_prob(0, 0, 2.5, 12, wins_hi=3, wins_lo=0) > 0.9
    assert sim.series_prob(0, 0, 2.5, 12, wins_hi=0, wins_lo=4) == 0.0
