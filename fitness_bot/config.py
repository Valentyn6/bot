from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_FILE)

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_path: str
    admin_telegram_id: int | None = None
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        token = self.bot_token.strip()
        if not token or ":" not in token or any(char.isspace() for char in token):
            raise RuntimeError("BOT_TOKEN має бути коректним Telegram bot token")
        level = self.log_level.strip().upper()
        if level not in VALID_LOG_LEVELS:
            raise RuntimeError(f"Невідомий LOG_LEVEL: {self.log_level}")
        object.__setattr__(self, "bot_token", token)
        object.__setattr__(self, "log_level", level)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN не заданий у .env")
        admin_id = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        try:
            parsed_admin_id = int(admin_id) if admin_id else None
        except ValueError as exc:
            raise RuntimeError("ADMIN_TELEGRAM_ID має бути числовим Telegram user id") from exc
        database_path = Path(os.getenv("DATABASE_PATH", "fitness_bot.sqlite3").strip())
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path
        return cls(
            token,
            str(database_path),
            parsed_admin_id,
            os.getenv("LOG_LEVEL", "INFO"),
        )
