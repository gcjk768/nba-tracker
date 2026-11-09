import json
import re

from nba import jobs, web
from test_jobs import ctx  # noqa: F401  (fixture)


def test_dashboard_data_and_render(ctx):
    jobs.daily(ctx)
    d = web.data(ctx)
    assert d["title"]["dates"] == ["2026-11-09"] and len(d["rankings"]) == 30
    assert len(d["tonight"]) == 8 and d["last_night"]["games"]
    html = web.render(d)
    blob = re.search(r"const D = (.*?);\n", html).group(1)
    assert json.loads(blob)["season"] == d["season"]
    assert "/*__DATA__*/" not in html and "</script" not in blob


def test_history_fills_past_days(ctx):
    jobs.daily(ctx)
    ctx.now = lambda: __import__("datetime").datetime(2026, 11, 12, 7, 15, tzinfo=__import__("datetime").timezone.utc)
    n = jobs.rebuild_history(ctx, n=500)
    dates = [r[0] for r in ctx.conn.execute("select distinct date from sims where kind='daily' order by date")]
    assert n == 2 and dates == ["2026-11-09", "2026-11-10", "2026-11-11"]
    assert jobs.rebuild_history(ctx, n=500) == 0                         # idempotent
