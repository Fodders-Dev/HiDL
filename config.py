import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    """Application settings loaded from environment."""

    bot_token: str
    database_url: str
    default_timezone: str = "UTC"


def get_settings() -> Settings:
    """Return settings from environment variables."""
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise ValueError("BOT_TOKEN is required. Put it in .env or environment.")

    database_url = os.getenv("DATABASE_URL", "sqlite:///hidl.db")
    return Settings(bot_token=bot_token, database_url=database_url)
