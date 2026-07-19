"""Core scraping logic.

Two independent ways to discover accounts in a chat:

* `collect_chat_sender_ids` — scans message history for unique senders.
  Works for any chat/channel the client can read, regardless of whether
  the member list is public.
* `collect_chat_participant_ids` — lists the chat/channel's full member
  roster via Telethon's `iter_participants`. Only works where Telegram
  exposes the member list (not for broadcast channels without admin
  rights, or chats with hidden members) — falls back to an empty set
  when it doesn't.

`collect_accounts` runs either or both and merges the results, tracking
per-account which source(s) it came from.

Every network-facing loop in this module (message scanning, participant
listing, and per-user profile fetches) retries on `FloodWaitError` with
a sleep instead of dying, and paces its requests with an artificial
delay — large chats mean thousands of requests, and both mechanisms
exist to make that survivable instead of triggering a flood ban.

The Telethon client is injected rather than constructed here, so this
module can be exercised with a fake/mock client in tests without
touching the network.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from telethon import utils
from telethon.errors import (
    ChannelPrivateError,
    ChannelsTooMuchError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError, InviteRequestSentError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import GetParticipantRequest, JoinChannelRequest
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import ChatInviteAlready, InputPeerSelf

from tg_scraper.models import Account

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Any]

SOURCE_MESSAGES = "messages"
SOURCE_PARTICIPANTS = "participants"


class TelegramClientLike(Protocol):
    """The subset of TelegramClient's interface this module depends on."""

    def iter_messages(self, entity: Any) -> Any: ...

    def iter_participants(self, entity: Any) -> Any: ...

    async def get_entity(self, entity: Any) -> Any: ...

    async def __call__(self, request: Any) -> Any: ...


@dataclass
class JoinResult:
    """Outcome of a `join_chat` call.

    `success` is True whenever the account ends up with access to the chat
    (already a member, or the join just went through). `reason` explains why
    not otherwise — e.g. a pending join request or a private channel — and
    is `None` on success, so callers have something to record alongside a
    failure without having to re-derive it from logs.
    """

    success: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.success


async def join_chat(
    client: TelegramClientLike, chat: Any, *, max_retries: int = 3
) -> JoinResult:
    """Join `chat` so its participant list (and, for private chats, its
    message history) becomes accessible to this account before collection.

    `chat` may be a username (`@name`, `name`, `t.me/name`), a private
    invite link (`t.me/+hash`, `t.me/joinchat/hash`), or an already-resolvable
    entity/numeric id. `telethon.utils.parse_username` is the same parser
    Telethon itself uses internally to tell a public username from an
    invite hash, so it's reused here instead of hand-rolling that parsing.
    An invite hash goes through a different API call (`ImportChatInviteRequest`)
    than a public username or entity (`JoinChannelRequest`), hence the branch.

    Checks whether the account is already a participant before joining, so
    a chat we're already in never triggers a redundant join (or join-request)
    call. Returns a `JoinResult` — `success=True` if the account has (or now
    has) access to the chat, whether because it was already a member or
    because the join just succeeded. `success=False` if it does not have
    access: a join request is pending approval, an expired/invalid invite
    hash, a private channel that refuses new members, or having hit
    Telegram's per-account channel-join limit — the `reason` string
    explains which, for the caller to record rather than failing the whole
    run.
    """
    invite_hash, is_invite = utils.parse_username(str(chat))
    print(invite_hash)
    print(is_invite)
    for attempt in range(max_retries):
        try:
            if is_invite:
                invite = await client(CheckChatInviteRequest(invite_hash))

                if isinstance(invite, ChatInviteAlready):
                    logger.info("Already a member of %s, skipping join", chat)
                    return JoinResult(True, "already a member")
                await client(ImportChatInviteRequest(invite_hash))
                logger.info("Joined %s", chat)

            else:
                entity = await client.get_entity(chat)
                already_member = False
                try:
                    await client(GetParticipantRequest(entity, InputPeerSelf()))
                    already_member = True
                except UserNotParticipantError:
                    logger.warning("User %s is not a member", chat)
                    pass
                # except Exception:
                #     # Membership can't be checked this way for every entity
                #     # kind (e.g. basic groups aren't channels) — fall through
                #     # and let JoinChannelRequest/UserAlreadyParticipantError
                #     # settle it instead.
                #     pass
                if already_member:
                    logger.info("Already a member of %s, skipping join", chat)
                    return JoinResult(True, "already a member")

                await client(JoinChannelRequest(entity))
                logger.info("Joined %s", chat)
            return JoinResult(True)
        except UserAlreadyParticipantError:
            return JoinResult(True, "already a member")
        except InviteRequestSentError:
            logger.warning("You have successfully requested to join %s", chat)
            return JoinResult(False, "join request sent, pending approval")
        except FloodWaitError as exc:
            wait = exc.seconds + 1
            logger.warning("Flood wait while joining %s: sleeping %ss", chat, wait)
            await asyncio.sleep(wait)
        except (
            ChannelPrivateError,
            InviteHashExpiredError,
            InviteHashInvalidError,
            ChannelsTooMuchError,
        ) as exc:
            logger.warning("Could not join %s: %s", chat, exc)
            return JoinResult(False, str(exc))
    logger.error("Giving up joining %s after %s retries", chat, max_retries)
    return JoinResult(False, f"gave up after {max_retries} retries (flood wait)")


ADDLIST_LINK_RE = re.compile(r"(?:https?://)?t\.me/addlist/(?P<slug>[A-Za-z0-9_-]+)")


def parse_addlist_slug(addlist: str) -> str:
    """Extract the slug from a t.me/addlist/<slug> chat-folder link.

    Accepts a full URL (with or without scheme) or an already-bare slug and
    returns just the slug, since that's what `CheckChatlistInviteRequest`
    expects.
    """
    match = ADDLIST_LINK_RE.search(addlist)
    return match.group("slug") if match else addlist


async def resolve_addlist_chats(
    client: TelegramClientLike,
    addlist: str,
    *,
    max_retries: int = 3,
    progress: ProgressCallback | None = None,
) -> list[Any]:
    """Resolve a t.me/addlist/<slug> chat-folder invite link to the chats inside it.

    A chat-folder link is a different Telegram concept from a regular chat
    invite link (`t.me/+hash`): `CheckChatlistInviteRequest` enumerates every
    chat in the folder without joining the folder (or any of its chats)
    itself — the caller decides what to do with each chat afterwards, e.g.
    feed it to `join_chat`/`collect_accounts` like any other `--chat` value.

    Each chat is returned as `@username` where the chat is public, or its
    marked numeric id (e.g. -1001234567890) otherwise. The numeric id form
    resolves later without another network round trip: Telethon caches every
    entity returned by any RPC call (including this one) in the client's
    session, so a subsequent `get_entity`/`iter_participants` call on that id
    finds it in the cache instead of needing a username to look up.
    """
    slug = parse_addlist_slug(addlist)
    logger.info("Resolving chat folder %s...", addlist)
    if progress:
        progress(f"Resolving chat folder {addlist}...")

    result = None
    for attempt in range(max_retries):
        try:
            result = await client(CheckChatlistInviteRequest(slug))
            break
        except FloodWaitError as exc:
            wait = exc.seconds + 1
            logger.warning("Flood wait while resolving chat folder %s: sleeping %ss", addlist, wait)
            await asyncio.sleep(wait)
        except (InviteHashExpiredError, InviteHashInvalidError) as exc:
            logger.warning("Could not resolve chat folder %s: %s", addlist, exc)
            if progress:
                progress(f"Could not resolve chat folder {addlist}: {exc}")
            return []

    if result is None:
        logger.error("Giving up resolving chat folder %s after %s retries", addlist, max_retries)
        if progress:
            progress(f"Giving up resolving chat folder {addlist} after {max_retries} retries")
        return []

    resolved: list[Any] = []
    for chat in result.chats:
        username = getattr(chat, "username", None)
        identifier: Any = f"@{username}" if username else utils.get_peer_id(chat)
        resolved.append(identifier)
        logger.info("Found chat in folder %s: %s (%s)", addlist, getattr(chat, "title", identifier), identifier)

    logger.info("Resolved %s chat(s) from folder %s", len(resolved), addlist)
    if progress:
        progress(f"Resolved {len(resolved)} chat(s) from folder {addlist}")
    return resolved


async def _iter_with_flood_retry(
    async_iterable: Any,
    *,
    delay: float = 0.0,
    max_retries: int = 3,
    label: str = "item",
) -> AsyncIterator[Any]:
    """Drive a Telethon async iterator (`iter_messages`/`iter_participants`),
    retrying with a sleep instead of dying on `FloodWaitError`, and pacing
    requests with `delay` between chunks (not between individual items).

    A large chat/channel pages through hundreds of requests just to list
    its senders or members; without this, a single flood wait partway
    through would kill the whole `collect_accounts` run instead of just
    pausing it. The failed page isn't lost — Telethon's iterators only
    raise `FloodWaitError` before a page is added to their internal
    buffer, so calling `__anext__` again re-requests the same page.

    `delay` paces network requests, not individual items. Telethon's
    listing iterators (`telethon.requestiter.RequestIter`, the shared base
    for `iter_messages`/`iter_participants`) fetch results in chunks
    (~100-200 items) buffered internally, and only issue a new request
    once the buffer is drained — so sleeping after every yielded item
    would wait far longer than needed (e.g. ~200x too long for
    `iter_participants`). `RequestIter` resets `index` to 0 then
    increments it to 1 for the first item pulled from each freshly-loaded
    chunk; that's the same public bookkeeping every one of its iterators
    relies on internally, so checking `index == 1` here (without
    depending on any private iterator class) tells us a new chunk — and
    thus a new request — just landed, and that's the only point where we
    sleep.
    """
    iterator = async_iterable.__aiter__()
    chunk_count = 0
    while True:
        for attempt in range(max_retries):
            try:
                item = await iterator.__anext__()
                break
            except StopAsyncIteration:
                return
            except FloodWaitError as exc:
                wait = exc.seconds + 1
                logger.warning("Flood wait while listing %s: sleeping %ss", label, wait)
                await asyncio.sleep(wait)
        else:
            logger.error("Giving up listing %s after %s retries", label, max_retries)
            return

        yield item
        if getattr(iterator, "index", None) == 1:
            chunk_count += 1
            # Skip the pause after the very first chunk: there's no prior
            # request yet to pace against.
            if delay and chunk_count > 1:
                await asyncio.sleep(delay)


MESSAGE_SCAN_TIME_LIMIT = 20 * 60


async def collect_chat_sender_ids(
    client: TelegramClientLike,
    chat: Any,
    *,
    delay: float = 0.0,
    max_retries: int = 3,
    time_limit: float | None = MESSAGE_SCAN_TIME_LIMIT,
) -> set[int]:
    """Return the set of unique sender ids that posted at least one message in `chat`.

    Unlike the participant list, a chat's message history has no natural
    size cap — an old, very active chat can have millions of messages. To
    keep a single chat from running away with the whole collection run,
    scanning stops after `time_limit` seconds even if history remains;
    pass `time_limit=None` to scan to the end regardless.
    """
    sender_ids: set[int] = set()
    deadline = time.monotonic() + time_limit if time_limit else None
    async for message in _iter_with_flood_retry(
        client.iter_messages(chat), delay=delay, max_retries=max_retries, label=f"messages in {chat}"
    ):
        sender_id = getattr(message, "sender_id", None)
        if sender_id is not None:
            sender_ids.add(sender_id)
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "Message scan time limit (%ss) reached for %s, stopping with %s sender(s) found so far",
                time_limit, chat, len(sender_ids),
            )
            break
    return sender_ids


async def collect_chat_participant_ids(
    client: TelegramClientLike, chat: Any, *, delay: float = 0.0, max_retries: int = 3
) -> set[int]:
    """Return every member id Telegram will let us list for `chat`.

    Broadcast channels without admin rights, or chats with a hidden
    member list, refuse this request — in that case we log a warning
    and return an empty set instead of failing the whole run.
    """
    participant_ids: set[int] = set()
    try:
        async for user in _iter_with_flood_retry(
            client.iter_participants(chat),
            delay=delay,
            max_retries=max_retries,
            label=f"participants in {chat}",
        ):
            user_id = getattr(user, "id", None)
            if user_id is not None:
                participant_ids.add(user_id)
    except (ChatAdminRequiredError, ChannelPrivateError) as exc:
        logger.warning("Participant list unavailable for %s: %s", chat, exc)
    return participant_ids


async def fetch_account(
    client: TelegramClientLike, user_id: int, *, max_retries: int = 3
) -> Account | None:
    """Fetch a single user's full profile (including bio) with flood-wait retries."""
    for attempt in range(max_retries):
        try:
            full = await client(GetFullUserRequest(user_id))
            break
        except FloodWaitError as exc:
            wait = exc.seconds + 1
            logger.warning("Flood wait for user %s: sleeping %ss", user_id, wait)
            await asyncio.sleep(wait)
        except Exception:
            logger.exception("Failed to fetch user %s", user_id)
            return None
    else:
        logger.error("Giving up on user %s after %s retries", user_id, max_retries)
        return None

    user = full.users[0] if getattr(full, "users", None) else None
    about = getattr(getattr(full, "full_user", None), "about", None)

    return Account(
        id=user_id,
        username=getattr(user, "username", None),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        phone=getattr(user, "phone", None),
        bio=about,
        is_bot=getattr(user, "bot", False) or False,
    )


async def collect_accounts(
    client: TelegramClientLike,
    chats: Iterable[Any],
    *,
    include_message_senders: bool = True,
    include_participants: bool = True,
    delay: float = 2.2,
    list_delay: float = 0,
    max_retries: int = 3,
    progress: ProgressCallback | None = None,
    known_ids: set[int] | None = None,
) -> AsyncIterator[Account]:
    """Discover accounts in `chats` and yield each one's full profile as soon
    as it's fetched, instead of collecting them all into memory first.

    Streaming lets the caller persist each account the moment it's ready,
    which is what makes both low memory use and checkpointed resumption
    possible for large chats: on a run that's fetching tens of thousands
    of profiles at a rate-limited pace, the caller can write every result
    to disk immediately rather than risking hours of progress in memory.

    By default both discovery methods run and are merged: every account
    that ever wrote a message, plus every account listed as a member
    (where Telegram exposes that list). Set `include_message_senders` /
    `include_participants` to False to run only one of the two.

    `delay` paces the one-request-per-user profile fetches; `list_delay`
    paces the paginated message/participant listing requests (cheaper
    per request, since each page covers ~100-200 users, so it defaults
    lower). Both phases retry on `FloodWaitError` (up to `max_retries`
    times) instead of letting a single flood wait kill the whole run.

    `known_ids` are accounts already checkpointed from an earlier run —
    their (expensive) profile fetch is skipped since avoiding it is the
    whole point of a checkpoint. If one of them turns up again in this
    run's `chats`, it is still yielded, but as a bare `Account` carrying
    only `id`/`seen_in_chats`/`sources` (profile fields left `None`) —
    a delta to be folded into the caller's existing record for that id
    (e.g. via `Account.merge`), not a fresh profile to save as-is. This
    is what lets a known account accumulate chat history across separate
    collection runs without ever re-fetching its profile.

    Each yielded `Account.seen_in_chats` records which chat(s) it was
    found in, and `Account.sources` records whether it was found by
    scanning messages, listed as a participant, or both.
    """
    if not include_message_senders and not include_participants:
        raise ValueError(
            "At least one of include_message_senders/include_participants must be True"
        )

    known_ids = known_ids or set()

    # chat label -> user id -> set of sources it was found through in that chat
    hits_by_chat: dict[str, dict[int, set[str]]] = {}

    for chat in chats:
        chat_label = str(chat)
        chat_hits: dict[int, set[str]] = {}

        if include_message_senders:
            if progress:
                progress(f"Scanning messages in {chat_label}...")
            sender_ids = await collect_chat_sender_ids(
                client, chat, delay=list_delay, max_retries=max_retries
            )
            for user_id in sender_ids:
                chat_hits.setdefault(user_id, set()).add(SOURCE_MESSAGES)
            if progress:
                progress(f"Found {len(sender_ids)} message senders in {chat_label}")

        if include_participants:
            if progress:
                progress(f"Listing participants in {chat_label}...")
            participant_ids = await collect_chat_participant_ids(
                client, chat, delay=list_delay, max_retries=max_retries
            )
            for user_id in participant_ids:
                chat_hits.setdefault(user_id, set()).add(SOURCE_PARTICIPANTS)
            if progress:
                progress(f"Found {len(participant_ids)} participants in {chat_label}")

        hits_by_chat[chat_label] = chat_hits

    all_user_ids: set[int] = set()
    for chat_hits in hits_by_chat.values():
        all_user_ids |= chat_hits.keys()

    pending_ids = all_user_ids - known_ids
    already_known_ids = all_user_ids & known_ids
    if progress and known_ids:
        progress(
            f"{len(already_known_ids)} already-checkpointed account(s) re-seen this run "
            f"(chat list will be updated, no re-fetch), {len(pending_ids)} new account(s) to fetch"
        )

    for user_id in all_user_ids:
        seen_in_chats: set[str] = set()
        sources: set[str] = set()
        for chat_label, chat_hits in hits_by_chat.items():
            if user_id in chat_hits:
                seen_in_chats.add(chat_label)
                sources |= chat_hits[user_id]

        if user_id in known_ids:
            account = Account(id=user_id, seen_in_chats=seen_in_chats, sources=sources)
            if progress:
                progress(f"Updated chat list for known account {user_id}")
            yield account
            continue

        account = await fetch_account(client, user_id, max_retries=max_retries)
        if account is None:
            continue

        account.seen_in_chats = seen_in_chats
        account.sources = sources

        if progress:
            progress(f"Fetched profile for {account.display_name}")
        yield account
        if delay:
            await asyncio.sleep(delay)
