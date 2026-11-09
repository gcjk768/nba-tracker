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


def test_each_conference_filter_is_scoped_to_its_section():
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent.joinpath("nba", "dashboard.html").read_text(encoding="utf-8")
    assert 'querySelectorAll(".seg button")' not in html          # a page-wide selector made two filters fight
    assert '#s-styles .seg button' in html and '#pbSeg button' in html


def test_reveal_observers_do_not_use_ratio_thresholds_on_tall_sections():
    # a ratio threshold never fires on a section taller than viewport / threshold (the 30-card playbook grid)
    from pathlib import Path
    html = Path(__file__).resolve().parent.parent.joinpath("nba", "dashboard.html").read_text(encoding="utf-8")
    assert "{threshold: .08}" not in html and "{threshold: .05}).observe(box)" not in html
