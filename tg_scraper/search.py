"""Keyword search over collected accounts' bios."""

from __future__ import annotations

import re
from collections.abc import Iterable

from tg_scraper.models import Account


def search_by_keywords(
    accounts: Iterable[Account],
    keywords: Iterable[str],
    *,
    match_all: bool = False,
    case_sensitive: bool = False,
    regex: bool = False,
    whole_word: bool = False,
    search_username: bool = False,
) -> list[Account]:
    """Return accounts whose bio (or username) matches the given keywords.

    match_all=False (default): matches if ANY keyword is found (OR).
    match_all=True: matches only if ALL keywords are found (AND).
    regex=True: treat each keyword as a regular expression instead of
    a plain substring.
    whole_word=True: only match the keyword as a whole word, e.g. "AU"
    matches "AU citizen" but not "AUTH" or "BAU".
    search_username=True: match against the account's username instead of
    its bio.
    """
    keywords = [kw for kw in keywords if kw]
    if not keywords:
        return []

    flags = 0 if case_sensitive else re.IGNORECASE
    patterns = []
    for kw in keywords:
        body = kw if regex else re.escape(kw)
        if whole_word:
            body = rf"\b{body}\b"
        patterns.append(re.compile(body, flags))

    matches = []
    for account in accounts:
        field = account.username if search_username else account.bio
        if not field:
            continue
        hits = [bool(pattern.search(field)) for pattern in patterns]
        if (match_all and all(hits)) or (not match_all and any(hits)):
            matches.append(account)
    return matches
