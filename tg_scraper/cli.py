"""Command-line interface.

    tg-scraper collect --chat mychat --chat -1001234567890 --db accounts.db
    tg-scraper collect --chat anotherchat --db accounts.db
    tg-scraper search --db accounts.db --keyword crypto --keyword manager
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import click

from tg_scraper.client import build_client
from tg_scraper.collector import collect_accounts, join_chat, resolve_addlist_chats
from tg_scraper.config import ConfigError, Settings
from tg_scraper.models import ChatStatus
from tg_scraper.search import search_by_keywords
from tg_scraper.storage import (
    connect,
    get_known_ids,
    iter_accounts_jsonl,
    iter_chat_statuses_jsonl,
    load_accounts,
    load_chat_statuses,
    upsert_account,
    upsert_chat_status,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")


@click.group()
def main() -> None:
    """Collect Telegram chat participants and search their bios."""


@main.command()
@click.option(
    "--chat",
    "chats",
    multiple=True,
    help="Chat username, invite link, or numeric id. Repeat for multiple chats.",
)
@click.option(
    "--addlist",
    "addlists",
    multiple=True,
    help="t.me/addlist/<slug> chat-folder link. Every chat in the folder is "
    "added to --chat. Repeat for multiple folders.",
)
@click.option(
    "--db",
    "db_path",
    default="accounts.db",
    show_default=True,
    help="SQLite database to store collected accounts and each chat's "
    "join/collect outcome in (a chat already collected successfully is "
    "skipped on a later run).",
)
@click.option("--delay", default=3.5, show_default=True, help="Seconds to wait between profile lookups.")
@click.option(
    "--list-delay",
    default=0.3,
    show_default=True,
    help="Seconds to wait between paginated message/participant listing requests.",
)
@click.option(
    "--messages/--no-messages",
    "include_messages",
    default=False,
    show_default=True,
    help="Collect accounts that wrote at least one message.",
)
@click.option(
    "--participants/--no-participants",
    "include_participants",
    default=True,
    show_default=True,
    help="Collect the full member list where Telegram exposes it.",
)
@click.option(
    "--join",
    "join_chats",
    is_flag=True,
    help=(
        "Join each --chat before collecting. Telegram only exposes the full "
        "member list to chats/channels this account has joined; without this, "
        "--participants on a chat you haven't joined may return few or no results."
    ),
)
def collect(
    chats: tuple[str, ...],
    addlists: tuple[str, ...],
    db_path: str,
    delay: float,
    list_delay: float,
    include_messages: bool,
    include_participants: bool,
    join_chats: bool,
) -> None:
    """Scrape accounts from one or more chats, saving each one to --db as
    soon as its profile is fetched (not all at once at the end).

    Accounts already saved in --db (from a previous run, possibly against
    different chats) are never dropped: their profile isn't re-fetched, but
    if this run finds them again, the newly seen chat(s) are folded into
    their existing seen_in_chats/sources instead of being discarded — so
    running collect against a new chat accumulates onto the same database
    rather than starting over.

    Each chat's outcome (joined/collected successfully, or the reason it
    wasn't) is recorded in --db too. A chat already marked successful there
    is skipped entirely on a later run — no rejoining, no re-scraping.

    --addlist folders are resolved to their member chats before the
    already-collected check, so a chat already collected (whether it came
    from --chat or an earlier --addlist run) is still skipped.
    """
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None

    if not include_messages and not include_participants:
        raise click.ClickException("Pass at least one of --messages / --participants.")

    if not chats and not addlists:
        raise click.ClickException("Pass at least one of --chat / --addlist.")

    conn = connect(db_path)
    try:
        known_ids = get_known_ids(conn)
        if known_ids:
            click.echo(f"Loaded {len(known_ids)} previously collected account(s) from {db_path}")

        chat_statuses = load_chat_statuses(conn)

        async def run() -> tuple[int, int]:
            client = build_client(settings)
            run_new_count = 0
            run_updated_count = 0
            async with client:
                all_chats = list(chats)
                for addlist in addlists:
                    all_chats.extend(await resolve_addlist_chats(client, addlist, progress=click.echo))

                pending_chats = []
                for chat in all_chats:
                    status = chat_statuses.get(str(chat))
                    if status and status.success:
                        click.echo(f"Skipping {chat}: already collected successfully")
                    else:
                        pending_chats.append(chat)

                if not pending_chats:
                    click.echo("Nothing to do: every chat was already collected successfully.")
                    return run_new_count, run_updated_count

                for chat in pending_chats:
                    if join_chats:
                        click.echo(f"Joining {chat}...")
                        join_result = await join_chat(client, chat)
                        if not join_result.success:
                            click.echo(f"Skipping {chat}: {join_result.reason}")
                            upsert_chat_status(conn, ChatStatus(str(chat), False, join_result.reason))
                            continue

                    try:
                        async for account in collect_accounts(
                            client,
                            [chat],
                            include_message_senders=include_messages,
                            include_participants=include_participants,
                            delay=delay,
                            list_delay=list_delay,
                            progress=click.echo,
                            known_ids=known_ids,
                        ):
                            outcome = upsert_account(conn, account)
                            if outcome == "new":
                                run_new_count += 1
                            else:
                                run_updated_count += 1
                            known_ids.add(account.id)

                    except Exception as exc:
                        click.echo(f"Failed collecting from {chat}: {exc}")
                        upsert_chat_status(conn, ChatStatus(str(chat), False, str(exc)))
                        continue

                    upsert_chat_status(conn, ChatStatus(str(chat), True))
            return run_new_count, run_updated_count

        new_count, updated_count = asyncio.run(run())
        total = len(get_known_ids(conn))
    finally:
        conn.close()

    click.echo(
        f"Saved {new_count} new account(s), updated {updated_count} existing account(s) "
        f"({total} total in {db_path})"
    )


@main.command()
@click.option(
    "--accounts-input",
    "accounts_input_path",
    default="accounts.jsonl",
    show_default=True,
    help="Legacy JSON Lines accounts file to import.",
)
@click.option(
    "--chats-input",
    "chats_input_path",
    default="chats.jsonl",
    show_default=True,
    help="Legacy JSON Lines chat-status file to import.",
)
@click.option("--db", "db_path", default="accounts.db", show_default=True, help="SQLite database to import into.")
def migrate(accounts_input_path: str, chats_input_path: str, db_path: str) -> None:
    """One-off import of accounts and chat statuses from the old JSON Lines
    files (--accounts-input/--chats-input) into --db.

    Safe to run against a --db that already has data in it: each account is
    upserted the same way a `collect` run folds in a re-seen account, so
    running this twice (or against a --db that already has some overlapping
    accounts) doesn't create duplicates or lose newer data.
    """
    conn = connect(db_path)
    try:
        account_count = 0
        for account in iter_accounts_jsonl(accounts_input_path):
            upsert_account(conn, account)
            account_count += 1

        chat_count = 0
        for status in iter_chat_statuses_jsonl(chats_input_path):
            upsert_chat_status(conn, status)
            chat_count += 1
    finally:
        conn.close()

    click.echo(
        f"Migrated {account_count} account(s) from {accounts_input_path} and "
        f"{chat_count} chat status entry/entries from {chats_input_path} into {db_path}"
    )


@main.command()
@click.option("--db", "db_path", default="accounts.db", show_default=True)
@click.option("--keyword", "keywords", multiple=True, required=True, help="Repeat for multiple keywords.")
@click.option("--match-all", is_flag=True, help="Require every keyword to match (default: any).")
@click.option("--case-sensitive", is_flag=True)
@click.option("--regex", is_flag=True, help="Treat keywords as regular expressions.")
@click.option("--whole-word", is_flag=True, help="Only match a keyword as a whole word.")
@click.option("--username", "search_username", is_flag=True, help="Match against username instead of bio.")
@click.option("--output", "output_path", default=None, help="Optionally save matches to a JSON Lines file.")
def search(
    db_path: str,
    keywords: tuple[str, ...],
    match_all: bool,
    case_sensitive: bool,
    regex: bool,
    whole_word: bool,
    search_username: bool,
    output_path: str | None,
) -> None:
    """Search previously collected accounts' bios (or usernames) for keyword matches."""
    conn = connect(db_path)
    try:
        accounts = load_accounts(conn)
    finally:
        conn.close()

    matches = search_by_keywords(
        accounts.values(),
        keywords,
        match_all=match_all,
        case_sensitive=case_sensitive,
        regex=regex,
        whole_word=whole_word,
        search_username=search_username,
    )

    for account in matches:
        matched_field = account.username if search_username else account.bio
        click.echo(f"{account.display_name} (id={account.id}): {matched_field}")
    click.echo(f"\n{len(matches)} match(es) out of {len(accounts)} accounts")

    if output_path:
        with Path(output_path).open("w", encoding="utf-8") as handle:
            for account in matches:
                handle.write(json.dumps(account.to_dict(), ensure_ascii=False) + "\n")
        click.echo(f"Saved matches to {output_path}")


if __name__ == "__main__":
    main()
