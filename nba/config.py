"""Settings from the environment (NAS .env). One place for paths, ids and limits."""
import os
from dataclasses import dataclass
from pathlib import Path


def _on(name, default="on"):
    return os.environ.get(name, default).strip().lower() in ("1", "on", "true", "yes")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    vault_dir: Path
    season: str
    espn_season: int          # ESPN labels 2026-27 as 2027
    tg_token: str
    tg_chat_id: str
    tg_thread_id: int | None
    allowed_ids: frozenset      # ALLOWED_USER_IDS: Telegram users who may run commands
    spoiler: bool
    ask_limit: int
    model_daily: str
    model_ask: str
    vault_name: str           # Obsidian vault name for obsidian:// links
    vault_prefix: str         # folder of this app inside that vault ("" if the vault root is the app)

    @property
    def db_path(self):
        return self.data_dir / "nba.db"


def load() -> Settings:
    thread = os.environ.get("TELEGRAM_THREAD_ID", "").strip()
    return Settings(
        data_dir=Path(os.environ.get("DATA_DIR", "/app/data")),
        vault_dir=Path(os.environ.get("VAULT_DIR", "/vault")),
        season=os.environ.get("SEASON", "2026-27"),
        espn_season=int(os.environ.get("ESPN_SEASON", "2027")),
        tg_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        tg_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        tg_thread_id=int(thread) if thread else None,
        allowed_ids=frozenset(int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x),
        spoiler=_on("SPOILER_MODE"),
        ask_limit=int(os.environ.get("ASK_DAILY_LIMIT", "20")),
        model_daily=os.environ.get("CLAUDE_MODEL_DAILY", "claude-sonnet-5-5"),
        model_ask=os.environ.get("CLAUDE_MODEL_ASK", "claude-haiku-5-5"),
        vault_name=os.environ.get("OBSIDIAN_VAULT_NAME", "Obsidian"),        # /volume1/<USER>/Obsidian
        vault_prefix=os.environ.get("OBSIDIAN_VAULT_PREFIX", "NBA Tracker"),  # this app's folder in it
    )
