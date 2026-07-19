"""Runtime configuration loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_name: str

    @classmethod
    def from_env(cls) -> "Settings":
        api_id = os.getenv("TG_API_ID")
        api_hash = os.getenv("TG_API_HASH")
        session_name = os.getenv("TG_SESSION_NAME", "tg_scraper")

        if not api_id or not api_hash:
            raise ConfigError(
                "TG_API_ID and TG_API_HASH must be set (env vars or .env file). "
                "Get them from https://my.telegram.org."
            )

        try:
            api_id_int = int(api_id)
        except ValueError as exc:
            raise ConfigError("TG_API_ID must be an integer") from exc

        return cls(api_id=api_id_int, api_hash=api_hash, session_name=session_name)