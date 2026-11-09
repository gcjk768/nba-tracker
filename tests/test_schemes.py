from nba import schemes


def test_kind_and_zone():
    assert schemes.kind_of("Driving Layup Shot", False) == "drive"
    assert schemes.kind_of("Pullup Jump Shot", True) == "pull-up"
    assert schemes.kind_of("Jump Shot", False) == "spot-up jumper"
    assert schemes.kind_of("Layup Shot Putback", False) == "putback"
    assert schemes.kind_of("Heave Jump Shot", False) is None
    assert schemes.zone_of("makes 25-foot three point jumper", 46, 14, True) == "above-break 3"
    assert schemes.zone_of("makes 23-foot three point jumper", 1, 0, True) == "corner 3"
    assert schemes.zone_of("makes driving layup", 25, 1, False) == "rim"
    assert schemes.zone_of("misses 17-foot pullup", 30, 16, False) == "long mid"


def test_shots_and_profiles_on_real_game(conn):
    from nba import collector
    from test_collector import FakeClient
    collector.collect_date(conn, FakeClient(), "2026-11-08", 2027)
    S = schemes.shots(conn, 2027)
    assert len(S) > 400 and {s["kind"] for s in S} <= set(schemes.KINDS)
    assert any(s["assisted"] for s in S) and any(s["transition"] for s in S)
    P = schemes.team_profiles(conn, 2027, {})
    for t in ("PHI", "CHI"):
        assert abs(sum(P[t]["offense"]["kind"].values()) - 1) < 1e-9
        assert all(l["source"] == "inferred" and 0.2 <= l["confidence"] <= 0.7 for l in P[t]["labels"])


def test_what_wins_sign():
    rows = [{"net": n, "metrics": {"def:forced_tov": n / 100}} for n in range(-10, 11)]
    out = {w["metric"]: w["r"] for w in schemes.what_wins(rows)}
    assert out["Forced turnovers"] > 0.99
