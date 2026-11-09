import yaml

from nba import obsidian_pub as ob


def _fm(text):
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_frontmatter_is_valid_yaml(tmp_path):
    v = ob.Vault(tmp_path)
    v.write("Daily/2026/11/2026-11-09.md", {"date": "2026-11-09", "teams": ["BOS", "NY"], "verdict": 'He said "no": ok #1',
                                             "net": 3.25, "tags": ["daily"]}, "# x")
    fm = _fm((tmp_path / "Daily/2026/11/2026-11-09.md").read_text(encoding="utf-8"))
    assert fm["teams"] == ["BOS", "NY"] and fm["net"] == 3.25 and fm["verdict"] == 'He said "no": ok #1'


def test_team_page_keeps_history(tmp_path):
    v = ob.Vault(tmp_path)
    v.team_page("BOS", {"team": "BOS"}, "net +5", "- 2026-11-08 net +4")
    v.team_page("BOS", {"team": "BOS"}, "net +6", "- 2026-11-09 net +5")
    t = (tmp_path / "Teams/Boston Celtics.md").read_text(encoding="utf-8")
    assert "net +6" in t and "net +5\n" not in t.split("## History")[0]
    assert "- 2026-11-08 net +4\n- 2026-11-09 net +5" in t


def test_activity_and_links(tmp_path):
    v = ob.Vault(tmp_path, "NBA 2026-27")
    v.activity("📊", "daily done", "7 games", "2026-11-09")
    files = list(tmp_path.glob("Activity/2026/*/*.md"))
    assert len(files) == 1 and "**daily done** · 7 games · [[2026-11-09]]" in files[0].read_text(encoding="utf-8")
    assert ob.game_name("2026-11-08", "PHI", "CHI") == "2026-11-08 PHI @ CHI"
    assert v.obsidian_url("Daily/2026/11/2026-11-09.md").startswith("obsidian://open?vault=NBA%202026-27&file=Daily/")
