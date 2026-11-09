import json
import subprocess

from nba import news
from conftest import load

RSS = """<rss><channel><item><title>Celtics star ruled out vs. Sixers</title><link>https://x/1</link><pubDate>Mon</pubDate>
<description>&lt;p&gt;Jayson Tatum will sit with a sore ankle.&lt;/p&gt;</description></item>
<item><title>Power rankings week 3</title><link>https://x/2</link></item></channel></rss>"""


def fake_claude(cmd, stdin, timeout):
    items = json.loads(stdin)["items"]
    out = []
    for it in items:
        if "ruled out" in it["title"]:
            out.append({"i": it["i"], "keep": True, "kind": "injury", "teams": ["BOS"], "players": ["Jayson Tatum"],
                        "status_implied": "out", "summary": "Tatum out with an ankle issue."})
        else:
            out.append({"i": it["i"], "keep": False, "kind": "other", "teams": [], "players": [], "status_implied": None, "summary": ""})
    return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": json.dumps({"items": out})}), "")


class FakeHTTP:
    def get(self, url, **k):
        class R:
            text = RSS
            def json(self_inner):
                return {"articles": [{"type": "Recap", "headline": "Bulls lose", "categories": []},
                                     {"type": "Story", "headline": "Spurs sign veteran guard", "categories": [], "links": {"web": {"href": "u"}}}]}
        return R()


def test_parsers_skip_recaps_and_strip_html():
    assert [x["title"] for x in news.from_rss(RSS)] == ["Celtics star ruled out vs. Sixers", "Power rankings week 3"]
    assert "<p>" not in news.from_rss(RSS)[0]["text"]
    arts = news.from_espn_news(FakeHTTP().get("x").json())
    assert [a["title"] for a in arts] == ["Spurs sign veteran guard"]
    inj = news.from_injuries(load("injuries.json"))
    assert inj and all(x["source"] == "ESPN injury report" and x["hint_team"] for x in inj)


def test_collect_tags_flags_and_dedupes(conn):
    values = {"1": {"name": "Jayson Tatum", "team": "BOS", "value": 4.2}}
    kept = news.collect(conn, "m", None, teams_tonight=["BOS"], values=values, injuries={}, runner=fake_claude, http=FakeHTTP())
    assert len(kept) == 1 and kept[0]["ahead"] == 1 and kept[0]["impact"] == 4.2 and kept[0]["tonight"] == 1
    again = news.collect(conn, "m", None, teams_tonight=["BOS"], values=values, injuries={}, runner=fake_claude, http=FakeHTTP())
    assert again == []                                                       # same items: nothing new
    push = news.to_push(conn, contenders={"BOS"}, top_players={"BOS": {"jayson tatum"}})
    assert len(push) == 1
    news.mark_pushed(conn, [push[0]["id"]])
    assert news.to_push(conn, contenders={"BOS"}, top_players={"BOS": {"jayson tatum"}}) == []


def test_spoiler_guard_and_subject_only():
    assert "hidden" in news.spoiler_safe("Doncic rested in Lakers' win over Pistons; Reaves scored 43 in the 121-116 victory.", "rest", ["Luka Doncic"])
    assert news.spoiler_safe("Jokic has an elbow sprain and may miss Monday.", "injury", ["Nikola Jokic"]).startswith("Jokic")
