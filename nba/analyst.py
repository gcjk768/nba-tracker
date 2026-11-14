"""claude -p analyst: compact JSON in, schema-checked JSON out. One retry, then None (stats-only digest)."""
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).parent / "prompts"


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Action(_M):
    action: str
    source: Literal["measured", "inferred", "reported"]
    confidence: float = 0.0
    ppp: float | None = None
    source_url: str = ""


class Scheme(_M):
    scheme: str
    source: Literal["measured", "inferred", "reported"]
    confidence: float = 0.0
    source_url: str = ""


class Tactics(_M):
    team: str
    offense_actions: list[Action] = []
    defense_scheme: Scheme | None = None
    what_worked: str = ""
    what_failed: str = ""
    adjustment: str = ""


class Game(_M):
    game_id: str
    headline: str
    key_stat: str
    verdict: str
    tactics: list[Tactics] = []


class KeyPlayer(_M):
    player: str
    role: str = ""
    status: str = ""
    impact: str = ""


class Team(_M):
    team: str
    verdict: str
    strengths: list[str] = []
    weaknesses: list[str] = []
    ceiling: str = ""
    floor: str = ""
    trend: Literal["rising", "steady", "falling"] = "steady"
    go_to_action: str = ""
    counter_action: str = ""
    struggles_against: str = ""
    key_players: list[KeyPlayer] = []
    contender: bool = False
    confidence: Literal["certain", "likely", "guessing"] = "guessing"

    @field_validator("confidence", "trend", mode="before")
    @classmethod
    def lower(cls, v):
        return str(v).lower()


class Title(_M):
    team: str
    model_pct: float
    market_pct: float | None = None
    note: str = ""


class Pick(_M):
    team: str
    confidence: str = ""
    reasons: list[str] = []


class AarNote(_M):
    game_id: str
    note: str


class Verdict(_M):
    date: str
    games: list[Game] = []
    teams: list[Team] = []
    players: list[dict] = []
    title_race: list[Title] = []
    pick: Pick | None = None
    watch_list: list[str] = []
    aar_notes: list[AarNote] = []
    summary: list[str] = []          # the 3-5 line verdict for the digest


class Check(_M):                     # film room verifier (prompts/film_verify.md): anything left out counts as "no"
    id: int
    supported: bool = False
    quote: str = ""
    scope_ok: bool = False
    side_ok: bool = False


class Checks(_M):
    checks: list[Check]


def _cmd(prompt: str, model: str):
    return ["claude", "-p", "--bare", "--output-format", "json", "--model", model, "--tools", "",
            "--no-session-persistence", "--system-prompt", prompt]


def _default_runner(cmd, stdin, timeout):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, encoding="utf-8")


def extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def call(prompt: str, user: str, model: str, runner=_default_runner, timeout=600) -> str:
    """Raw text result of one claude -p call (raises on CLI failure)."""
    p = runner(_cmd(prompt, model), user, timeout)
    if p.returncode != 0:
        raise RuntimeError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}")
    out = json.loads(p.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude error: {str(out.get('result'))[:300]}")
    return out.get("result", "")


def run(prompt_name: str, summary_json: str, model: str, runner=_default_runner, schema=Verdict) -> tuple[BaseModel | None, str]:
    prompt = (PROMPTS / prompt_name).read_text(encoding="utf-8")
    user = summary_json
    raw = ""
    for attempt in range(2):
        try:
            raw = call(prompt, user, model, runner)
            return schema.model_validate(extract_json(raw)), raw
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            log.warning("analyst output invalid (attempt %d): %s", attempt + 1, str(e)[:300])
            user = summary_json + "\n\nYOUR PREVIOUS REPLY WAS INVALID: " + str(e)[:800] + \
                "\nReply again with ONLY the JSON object matching the schema."
        except Exception as e:                       # CLI failure, timeout: retry once too
            log.warning("analyst call failed (attempt %d): %s", attempt + 1, e)
    return None, raw


def ask(question: str, context_json: str, model: str, runner=_default_runner) -> str:
    prompt = (PROMPTS / "ask.md").read_text(encoding="utf-8")
    return call(prompt, f"DATA:\n{context_json}\n\nQUESTION: {question}", model, runner, timeout=300)
