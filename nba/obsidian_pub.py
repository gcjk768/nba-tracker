"""Vault writer. Best-effort: any I/O error is logged and swallowed, never breaks a run.

Layout (inside VAULT_DIR = '<vault>/NBA 2026-27'):
  Home.md, Activity/YYYY/MM/YYYY-MM-DD.md, Daily/YYYY/MM/YYYY-MM-DD.md, Games/YYYY/MM/YYYY-MM-DD AWAY @ HOME.md,
  Teams/<Team Name>.md, Weekly/YYYY-Www.md, Model/{Backtest,Scorecard,Tuning}.md,
  Model/Predictions/YYYY/MM/YYYY-MM-DD.md, Model/AAR/YYYY/MM/YYYY-MM-DD.md, Injuries/YYYY-MM.md
"""
import json
import logging
import os
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import espn

log = logging.getLogger(__name__)
SGT = ZoneInfo("Asia/Singapore")


def _yaml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(round(v, 4)) if isinstance(v, float) else str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_yaml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)      # a JSON string is a valid YAML scalar


def frontmatter(fm: dict) -> str:
    return "---\n" + "".join(f"{k}: {_yaml(v)}\n" for k, v in fm.items()) + "---\n"


def link(name: str) -> str:
    return f"[[{name}]]"


def team_link(t: str) -> str:
    return link(espn.NAMES.get(t, t))


def ym(d: str) -> str:
    return f"{d[:4]}/{d[5:7]}"


def game_rel(d, away, home) -> str:
    return f"Games/{ym(d)}/{d} {away} @ {home}.md"


def game_name(d, away, home) -> str:
    return f"{d} {away} @ {home}"


class Vault:
    def __init__(self, root: Path, vault_name="NBA 2026-27", prefix=""):
        self.root, self.vault_name, self.prefix = Path(root), vault_name, prefix

    def _path(self, rel) -> Path:
        return self.root / rel

    def write(self, rel: str, fm: dict, body: str):
        try:
            p = self._path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(frontmatter(fm) + "\n" + body.rstrip() + "\n", encoding="utf-8")
            os.replace(tmp, p)
            _chmod(p)
        except OSError as e:
            log.warning("vault write %s failed: %s", rel, e)

    def append(self, rel: str, line: str, header: str | None = None):
        try:
            p = self._path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            new = not p.exists()
            with p.open("a", encoding="utf-8") as f:
                if new and header:
                    f.write(header.rstrip() + "\n\n")
                f.write(line.rstrip() + "\n")
            if new:
                _chmod(p)
        except OSError as e:
            log.warning("vault append %s failed: %s", rel, e)

    def read(self, rel) -> str:
        try:
            return self._path(rel).read_text(encoding="utf-8")
        except OSError:
            return ""

    def obsidian_url(self, rel: str) -> str:
        f = (f"{self.prefix}/" if self.prefix else "") + rel[:-3]
        return f"obsidian://open?vault={urllib.parse.quote(self.vault_name)}&file={urllib.parse.quote(f)}"

    # ---------- movement log ----------
    def activity(self, emoji: str, what: str, detail: str = "", entity: str | None = None, when: datetime | None = None):
        now = (when or datetime.now(SGT)).astimezone(SGT)
        d = now.date().isoformat()
        line = f"- {now:%H:%M} {emoji} **{what}**" + (f" · {detail}" if detail else "") + (f" · {link(entity)}" if entity else "")
        self.append(f"Activity/{ym(d)}/{d}.md", line,
                    header=frontmatter({"tags": ["activity"], "updated": d}) + f"\n# Activity {d}\n")

    def recent_activity(self, days=3, cap=4000) -> str:
        """Newest-first excerpt of the movement log, for prompts ("what you already did")."""
        files = sorted(self.root.glob("Activity/*/*/*.md"), reverse=True)[:days]
        text = "\n".join(f.read_text(encoding="utf-8") for f in files) if files else ""
        return text[:cap]

    # ---------- team pages ----------
    def team_page(self, team: str, fm: dict, current_md: str, history_line: str | None):
        rel = f"Teams/{espn.NAMES.get(team, team)}.md"
        old = self.read(rel)
        hist = ""
        if "## History" in old:
            hist = old.split("## History", 1)[1].strip("\n")
        if history_line:
            hist = (hist + "\n" if hist else "") + history_line
        body = f"# {espn.NAMES.get(team, team)}\n\n## Current\n\n{current_md.strip()}\n\n## History\n\n{hist}\n"
        self.write(rel, fm, body)

    def home(self, today: str, season: str):
        body = f"""# NBA {season} tracker

Daily digest, ratings, simulation and frozen predictions for the {season} season, written by the `nba-bot`
container on the home NAS. Scores in Telegram sit behind spoilers; this vault has the full detail.

- Today: [[{today}]] (daily) · [[{today[:7]}|this month's injuries]] · activity in `Activity/{ym(today)}/`
- Model: [[Scorecard]] · [[Tuning]] · [[Backtest]]
- Teams: {' · '.join(team_link(t) for t in sorted(espn.NAMES))}
"""
        self.write("Home.md", {"tags": ["active"], "updated": today}, body)


def _chmod(p):
    try:
        os.chmod(p, 0o664)
    except OSError:
        pass


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join("" if c is None else str(c).replace("|", "/") for c in r) + " |" for r in rows]
    return "\n".join(out)
