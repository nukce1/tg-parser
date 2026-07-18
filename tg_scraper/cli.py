"""Command-line interface.

    tg-scraper collect --chat mychat --chat -1001234567890 --output accounts.jsonl
    tg-scraper collect --chat anotherchat --output accounts.jsonl
    tg-scraper search --input accounts.jsonl --keyword crypto --keyword manager
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from tg_scraper.client import build_client
from tg_scraper.collector import collect_accounts, join_chat
from tg_scraper.config import ConfigError, Settings
from tg_scraper.models import Account, ChatStatus
from tg_scraper.search import search_by_keywords
from tg_scraper.storage import (
    append_account,
    append_chat_status,
    load_accounts,
    load_chat_statuses,
    save_accounts,
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
    required=True,
    help="Chat username, invite link, or numeric id. Repeat for multiple chats.",
)
@click.option("--output", "output_path", default="accounts.jsonl", show_default=True)
@click.option(
    "--chats-output",
    "chats_output_path",
    default="chats.jsonl",
    show_default=True,
    help="Where to record each chat's join/collect outcome, so a chat already "
    "collected successfully is skipped on a later run.",
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
    output_path: str,
    chats_output_path: str,
    delay: float,
    list_delay: float,
    include_messages: bool,
    include_participants: bool,
    join_chats: bool,
) -> None:
    """Scrape accounts from one or more chats, writing each one to --output
    as soon as its profile is fetched (not all at once at the end).

    Accounts already saved in --output (from a previous run, possibly
    against different chats) are never dropped: their profile isn't
    re-fetched, but if this run finds them again, the newly seen chat(s)
    are folded into their existing seen_in_chats/sources instead of being
    discarded — so running collect against a new chat accumulates onto
    the same output file rather than starting over.

    Each chat's outcome (joined/collected successfully, or the reason it
    wasn't) is recorded in --chats-output. A chat already marked successful
    there is skipped entirely on a later run — no rejoining, no re-scraping.
    """
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None

    if not include_messages and not include_participants:
        raise click.ClickException("Pass at least one of --messages / --participants.")

    existing_accounts: dict[int, Account] = {}
    if Path(output_path).exists():
        existing_accounts = load_accounts(output_path)
        click.echo(f"Loaded {len(existing_accounts)} previously collected account(s) from {output_path}")
    known_ids = set(existing_accounts)

    chat_statuses = load_chat_statuses(chats_output_path)
    pending_chats = []
    for chat in chats:
        status = chat_statuses.get(str(chat))
        if status and status.success:
            click.echo(f"Skipping {chat}: already collected successfully")
        else:
            pending_chats.append(chat)

    if not pending_chats:
        click.echo("Nothing to do: every chat was already collected successfully.")
        return

    async def run() -> tuple[int, int]:
        client = build_client(settings)
        run_new_count = 0
        run_updated_count = 0
        async with client:
            with open(output_path, "a", encoding="utf-8") as handle, \
                    open(chats_output_path, "a", encoding="utf-8") as chats_handle:
                for chat in pending_chats:
                    if join_chats:
                        click.echo(f"Joining {chat}...")
                        join_result = await join_chat(client, chat)
                        if not join_result.success:
                            click.echo(f"Skipping {chat}: {join_result.reason}")
                            append_chat_status(chats_handle, ChatStatus(str(chat), False, join_result.reason))
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
                            if account.id in existing_accounts:
                                existing_accounts[account.id].merge(account)
                                run_updated_count += 1

                            else:
                                append_account(handle, account, run_new_count)
                                existing_accounts[account.id] = account
                                run_new_count += 1
                            known_ids.add(account.id)

                    except Exception as exc:
                        click.echo(f"Failed collecting from {chat}: {exc}")
                        append_chat_status(chats_handle, ChatStatus(str(chat), False, str(exc)))
                        continue

                    append_chat_status(chats_handle, ChatStatus(str(chat), True))
        return run_new_count, run_updated_count

    new_count, updated_count = asyncio.run(run())

    if updated_count:
        # New accounts this run were already appended above; rewriting now
        # (with everything, old and new) is the only way to persist the
        # merged chat list for accounts that existed before this run, since
        # JSON Lines has no way to patch a single already-written line.
        save_accounts(output_path, existing_accounts)

    click.echo(
        f"Saved {new_count} new account(s), updated {updated_count} existing account(s) "
        f"({len(existing_accounts)} total in {output_path})"
    )


@main.command()
@click.option("--input", "input_path", default="accounts.jsonl", show_default=True)
@click.option("--keyword", "keywords", multiple=True, required=True, help="Repeat for multiple keywords.")
@click.option("--match-all", is_flag=True, help="Require every keyword to match (default: any).")
@click.option("--case-sensitive", is_flag=True)
@click.option("--regex", is_flag=True, help="Treat keywords as regular expressions.")
@click.option("--whole-word", is_flag=True, help="Only match a keyword as a whole word.")
@click.option("--output", "output_path", default=None, help="Optionally save matches to a JSON Lines file.")
def search(
    input_path: str,
    keywords: tuple[str, ...],
    match_all: bool,
    case_sensitive: bool,
    regex: bool,
    whole_word: bool,
    output_path: str | None,
) -> None:
    """Search previously collected accounts' bios for keyword matches."""
    accounts = load_accounts(input_path)
    matches = search_by_keywords(
        accounts.values(),
        keywords,
        match_all=match_all,
        case_sensitive=case_sensitive,
        regex=regex,
        whole_word=whole_word,
    )

    for account in matches:
        click.echo(f"{account.display_name} (id={account.id}): {account.bio}")
    click.echo(f"\n{len(matches)} match(es) out of {len(accounts)} accounts")

    if output_path:
        save_accounts(output_path, matches)
        click.echo(f"Saved matches to {output_path}")


if __name__ == "__main__":
    main()
