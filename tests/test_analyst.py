import json
import subprocess

from nba import analyst

GOOD = {"date": "2026-11-09", "games": [{"game_id": "1", "headline": "h", "key_stat": "k", "verdict": "v", "tactics": []}],
        "teams": [{"team": "BOS", "verdict": "v", "trend": "Rising", "confidence": "Likely"}],
        "title_race": [{"team": "BOS", "model_pct": 0.2, "market_pct": 0.1, "note": ""}], "summary": ["a", "b", "c"]}


class Runner:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), 0

    def __call__(self, cmd, stdin, timeout):
        self.calls += 1
        assert cmd[:2] == ["claude", "-p"] and "--model" in cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": self.outputs.pop(0)}), "")


def test_valid_first_try():
    r = Runner(["```json\n" + json.dumps(GOOD) + "\n```"])
    v, _ = analyst.run("daily.md", "{}", "m", runner=r)
    assert v and v.teams[0].trend == "rising" and r.calls == 1


def test_retry_then_fallback():
    r = Runner(["not json", '{"date": 5, "teams": "x"}'])
    v, _ = analyst.run("daily.md", "{}", "m", runner=r)
    assert v is None and r.calls == 2


def test_rejects_upgraded_scheme_source():
    bad = dict(GOOD, games=[{"game_id": "1", "headline": "h", "key_stat": "k", "verdict": "v",
                             "tactics": [{"team": "BOS", "defense_scheme": {"scheme": "drop", "source": "fact"}}]}])
    r = Runner([json.dumps(bad), json.dumps(GOOD)])
    v, _ = analyst.run("daily.md", "{}", "m", runner=r)
    assert v and r.calls == 2                      # 'fact' is not a valid source tag: retried
