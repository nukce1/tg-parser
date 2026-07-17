"""Data model for a collected Telegram account."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Account:
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    bio: str | None = None
    is_bot: bool = False
    seen_in_chats: set[str] = field(default_factory=set)
    # How the account was found: "messages" (wrote at least one message),
    # "participants" (listed as a chat/channel member), or both.
    sources: set[str] = field(default_factory=set)

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part)
        if name:
            return name
        if self.username:
            return f"@{self.username}"
        return str(self.id)

    def merge(self, other: "Account") -> None:
        """Fold another sighting of the same account into this one."""
        self.username = other.username or self.username
        self.first_name = other.first_name or self.first_name
        self.last_name = other.last_name or self.last_name
        self.phone = other.phone or self.phone
        self.bio = other.bio or self.bio
        self.is_bot = self.is_bot or other.is_bot
        self.seen_in_chats |= other.seen_in_chats
        self.sources |= other.sources

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "phone": self.phone,
            "bio": self.bio,
            "is_bot": self.is_bot,
            "seen_in_chats": sorted(self.seen_in_chats),
            "sources": sorted(self.sources),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Account":
        return cls(
            id=data["id"],
            username=data.get("username"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            phone=data.get("phone"),
            bio=data.get("bio"),
            is_bot=data.get("is_bot", False),
            seen_in_chats=set(data.get("seen_in_chats", [])),
            sources=set(data.get("sources", [])),
        )


@dataclass
class ChatStatus:
    """Outcome of running `collect` against one chat.

    Checkpoints per-chat, the same way `Account` checkpoints per-account:
    a chat recorded here with `success=True` doesn't need to be joined or
    scraped again on a later run. `reason` explains a failure (e.g. "join
    request sent, pending approval") or is `None` on success.
    """

    chat: str
    success: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"chat": self.chat, "success": self.success, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatStatus":
        return cls(chat=data["chat"], success=data["success"], reason=data.get("reason"))