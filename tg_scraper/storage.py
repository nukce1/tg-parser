"""Persist collected accounts to / load them back from a SQLite database.

Unlike the JSON Lines file this replaces, an account already on file is
updated in place via `INSERT ... ON CONFLICT DO UPDATE` instead of requiring
the whole file to be re-read and rewritten whenever a previously-seen
account turns up again in a later collection run. WAL journaling plus
`synchronous=NORMAL` keeps every committed write durable against the
process being killed (the same guarantee the old flush/fsync dance gave
JSON Lines) without paying for a full fsync on every single commit.

`iter_accounts_jsonl`/`iter_chat_statuses_jsonl` read the legacy JSON Lines
format still produced by older runs of this tool, so a one-off migration
into a fresh database can be written on top of them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from tg_scraper.models import Account, ChatStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    phone TEXT,
    bio TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS account_chats (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    chat TEXT NOT NULL,
    PRIMARY KEY (account_id, chat)
);

CREATE TABLE IF NOT EXISTS account_sources (
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    PRIMARY KEY (account_id, source)
);

CREATE TABLE IF NOT EXISTS chat_statuses (
    chat TEXT PRIMARY KEY,
    success INTEGER NOT NULL,
    reason TEXT
);
"""

_UPSERT_ACCOUNT_SQL = """
INSERT INTO accounts (id, username, first_name, last_name, phone, bio, is_bot)
VALUES (:id, :username, :first_name, :last_name, :phone, :bio, :is_bot)
ON CONFLICT(id) DO UPDATE SET
    username = COALESCE(excluded.username, accounts.username),
    first_name = COALESCE(excluded.first_name, accounts.first_name),
    last_name = COALESCE(excluded.last_name, accounts.last_name),
    phone = COALESCE(excluded.phone, accounts.phone),
    bio = COALESCE(excluded.bio, accounts.bio),
    is_bot = accounts.is_bot OR excluded.is_bot
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the accounts database at `path`.

    WAL mode lets `collect`'s streaming writes commit cheaply one account at
    a time instead of batching flushes/fsyncs the way the JSON Lines writer
    had to; `synchronous=NORMAL` is the standard pairing with WAL that still
    survives an app crash (only a whole-OS crash at the wrong instant could
    lose the last commit), which is the same risk the old code accepted for
    everything between its periodic fsyncs.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def upsert_account(conn: sqlite3.Connection, account: Account) -> Literal["new", "updated"]:
    """Insert `account`, or fold it into the existing row for its id.

    Mirrors `Account.merge`: a `None` field never overwrites an existing
    value, and `is_bot` only ever flips true->stays true. `seen_in_chats`/
    `sources` are unioned in via `INSERT OR IGNORE` on their own tables
    rather than merged in Python, so the whole operation is one transaction
    regardless of how many chats/sources are new.
    """
    is_new = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account.id,)).fetchone() is None

    conn.execute(
        _UPSERT_ACCOUNT_SQL,
        {
            "id": account.id,
            "username": account.username,
            "first_name": account.first_name,
            "last_name": account.last_name,
            "phone": account.phone,
            "bio": account.bio,
            "is_bot": int(account.is_bot),
        },
    )
    conn.executemany(
        "INSERT OR IGNORE INTO account_chats (account_id, chat) VALUES (?, ?)",
        [(account.id, chat) for chat in account.seen_in_chats],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO account_sources (account_id, source) VALUES (?, ?)",
        [(account.id, source) for source in account.sources],
    )
    conn.commit()
    return "new" if is_new else "updated"


def get_known_ids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT id FROM accounts")}


def iter_accounts(conn: sqlite3.Connection) -> Iterator[Account]:
    """Yield every stored account, each with its full seen_in_chats/sources.

    Loads the (much smaller) association tables into memory up front and
    groups them by account id, rather than issuing a per-account query for
    each one's chats/sources.
    """
    chats_by_id: dict[int, set[str]] = {}
    for account_id, chat in conn.execute("SELECT account_id, chat FROM account_chats"):
        chats_by_id.setdefault(account_id, set()).add(chat)

    sources_by_id: dict[int, set[str]] = {}
    for account_id, source in conn.execute("SELECT account_id, source FROM account_sources"):
        sources_by_id.setdefault(account_id, set()).add(source)

    rows = conn.execute(
        "SELECT id, username, first_name, last_name, phone, bio, is_bot FROM accounts"
    )
    for account_id, username, first_name, last_name, phone, bio, is_bot in rows:
        yield Account(
            id=account_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            bio=bio,
            is_bot=bool(is_bot),
            seen_in_chats=chats_by_id.get(account_id, set()),
            sources=sources_by_id.get(account_id, set()),
        )


def load_accounts(conn: sqlite3.Connection) -> dict[int, Account]:
    return {account.id: account for account in iter_accounts(conn)}


def upsert_chat_status(conn: sqlite3.Connection, status: ChatStatus) -> None:
    conn.execute(
        "INSERT INTO chat_statuses (chat, success, reason) VALUES (?, ?, ?) "
        "ON CONFLICT(chat) DO UPDATE SET success = excluded.success, reason = excluded.reason",
        (status.chat, int(status.success), status.reason),
    )
    conn.commit()


def load_chat_statuses(conn: sqlite3.Connection) -> dict[str, ChatStatus]:
    return {
        chat: ChatStatus(chat=chat, success=bool(success), reason=reason)
        for chat, success, reason in conn.execute("SELECT chat, success, reason FROM chat_statuses")
    }


def iter_accounts_jsonl(path: str | Path) -> Iterator[Account]:
    """Read accounts from a legacy JSON Lines file, for one-off migration."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield Account.from_dict(json.loads(line))


def iter_chat_statuses_jsonl(path: str | Path) -> Iterator[ChatStatus]:
    """Read chat statuses from a legacy JSON Lines file, for one-off migration."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield ChatStatus.from_dict(json.loads(line))
