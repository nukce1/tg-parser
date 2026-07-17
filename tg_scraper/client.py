"""Thin factory around Telethon's client, kept separate so it's easy to mock in tests."""

from __future__ import annotations

from telethon import TelegramClient

from tg_scraper.config import Settings


def build_client(settings: Settings) -> TelegramClient:
    return TelegramClient(settings.session_name, settings.api_id, settings.api_hash)