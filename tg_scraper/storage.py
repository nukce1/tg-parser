"""Persist collected accounts to / load them back from a JSON Lines file.

JSON Lines (one JSON object per line) lets accounts be appended one at a
time as they're collected instead of holding the whole result set in
memory and writing it out in a single shot at the end. That's also what
makes checkpointing possible: the output file doubles as the record of
which accounts are already done, so a resumed run can skip re-fetching
them (each profile fetch is rate-limited, so re-doing tens of thousands
of them after a crash is what a checkpoint exists to avoid).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO

from tg_scraper.models import Account, ChatStatus


def append_account(handle: IO[str], account: Account, count: int) -> None:
    """Append one account to an already-open file handle and force it to disk.

    Flushing and fsyncing after every write is what makes the output file
    a reliable checkpoint: if the process is killed mid-run, everything
    written so far is guaranteed to be safely on disk, not sitting in a
    buffer that dies with the process. Flush every 10 accounts. Write to HDD every 100 accounts.
    """
    handle.write(json.dumps(account.to_dict(), ensure_ascii=False) + "\n")
    if count % 10 == 0:
        handle.flush()
    if count % 100 == 0:
        os.fsync(handle.fileno())


def iter_accounts(path: str | Path) -> Iterator[Account]:
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield Account.from_dict(json.loads(line))


def load_accounts(path: str | Path) -> dict[int, Account]:
    return {account.id: account for account in iter_accounts(path)}


def save_accounts(path: str | Path, accounts: dict[int, Account] | Iterable[Account]) -> None:
    """Write a full account collection to `path` in one shot.

    Fine for one-off exports such as saving search results. For a long
    collection run, stream with `append_account` instead so progress
    isn't lost if the process is interrupted.
    """
    values = accounts.values() if isinstance(accounts, dict) else accounts
    with Path(path).open("w", encoding="utf-8") as handle:
        for account in values:
            handle.write(json.dumps(account.to_dict(), ensure_ascii=False) + "\n")


def append_chat_status(handle: IO[str], status: ChatStatus) -> None:
    """Append one chat's run outcome to an already-open file handle and force
    it to disk, so a killed process still leaves a record of chats already
    done. There are only ever a handful of chats per run (unlike the
    thousands of accounts `append_account` paces its flushes for), so every
    write is flushed and fsynced immediately.
    """
    handle.write(json.dumps(status.to_dict(), ensure_ascii=False) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def iter_chat_statuses(path: str | Path) -> Iterator[ChatStatus]:
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield ChatStatus.from_dict(json.loads(line))


def load_chat_statuses(path: str | Path) -> dict[str, ChatStatus]:
    """Latest recorded status per chat.

    Statuses are append-only (a rerun writes a new line rather than patching
    the old one), so later lines for the same chat simply shadow earlier
    ones here — same convention as `load_accounts`/`Account.merge`, just
    without needing a merge since only the latest outcome matters.
    """
    statuses: dict[str, ChatStatus] = {}
    for status in iter_chat_statuses(path):
        statuses[status.chat] = status
    return statuses