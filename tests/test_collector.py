from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError, FloodWaitError

from tg_scraper.collector import (
    SOURCE_MESSAGES,
    SOURCE_PARTICIPANTS,
    collect_accounts,
    collect_chat_participant_ids,
    collect_chat_sender_ids,
    fetch_account,
)


async def _async_iter(items):
    for item in items:
        yield item


class FlakyAsyncIterator:
    """Async iterator that raises `exc` on the `fail_at`-th call to
    __anext__, then resumes from the same position on the next call --
    mirroring Telethon's real behavior where FloodWaitError happens
    before a page is added to the iterator's buffer, so nothing is lost
    on retry."""

    def __init__(self, items, *, fail_at, exc):
        self._items = iter(items)
        self._fail_at = fail_at
        self._exc = exc
        self._calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._calls += 1
        if self._calls == self._fail_at:
            raise self._exc
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


async def _noop():
    pass


class AlwaysFloodyIterator:
    """Async iterator that always raises, to test giving up after max_retries."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise FloodWaitError(None)


def _resolve_iterable(value):
    if value is None:
        return _async_iter([])
    if hasattr(value, "__aiter__"):
        return value
    if isinstance(value, Exception):
        async def _raise():
            raise value
            yield  # pragma: no cover - unreachable, makes this an async generator

        return _raise()
    return _async_iter(value)


@dataclass
class FakeUser:
    id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    bot: bool = False


@dataclass
class FakeFullUser:
    users: list = field(default_factory=list)
    full_user: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(about=None))


class FakeTelegramClient:
    """Minimal stand-in for TelegramClient satisfying TelegramClientLike."""

    def __init__(self, messages_by_chat=None, participants_by_chat=None, profiles=None):
        self.messages_by_chat = messages_by_chat or {}
        self.participants_by_chat = participants_by_chat or {}
        self.profiles = profiles or {}
        self.call_log = []

    def iter_messages(self, chat):
        return _resolve_iterable(self.messages_by_chat.get(chat))

    def iter_participants(self, chat):
        return _resolve_iterable(self.participants_by_chat.get(chat))

    async def __call__(self, request):
        self.call_log.append(request)
        user_id = request.id
        profile = self.profiles.get(user_id)
        if profile is None:
            user = FakeUser(id=user_id)
            return FakeFullUser(users=[user], full_user=SimpleNamespace(about=None))
        return profile


def make_message(sender_id):
    return SimpleNamespace(sender_id=sender_id)


@pytest.mark.asyncio
async def test_collect_chat_sender_ids_dedupes_and_skips_none():
    client = FakeTelegramClient(
        messages_by_chat={"chat": [make_message(1), make_message(2), make_message(1), make_message(None)]}
    )
    result = await collect_chat_sender_ids(client, "chat")
    assert result == {1, 2}


@pytest.mark.asyncio
async def test_collect_chat_participant_ids_returns_member_ids():
    client = FakeTelegramClient(participants_by_chat={"chat": [FakeUser(id=1), FakeUser(id=2)]})
    result = await collect_chat_participant_ids(client, "chat")
    assert result == {1, 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ChatAdminRequiredError(None), ChannelPrivateError(None)])
async def test_collect_chat_participant_ids_falls_back_to_empty_on_restricted_chats(error):
    client = FakeTelegramClient(participants_by_chat={"chat": error})
    result = await collect_chat_participant_ids(client, "chat")
    assert result == set()


@pytest.mark.asyncio
async def test_collect_chat_sender_ids_retries_on_flood_wait_then_continues(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", fake_sleep)

    client = FakeTelegramClient(
        messages_by_chat={
            "chat": FlakyAsyncIterator(
                [make_message(1), make_message(2)], fail_at=2, exc=FloodWaitError(None)
            )
        }
    )

    result = await collect_chat_sender_ids(client, "chat")

    assert result == {1, 2}
    assert sleeps  # slept before retrying instead of raising


@pytest.mark.asyncio
async def test_collect_chat_participant_ids_retries_on_flood_wait_then_continues(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", fake_sleep)

    client = FakeTelegramClient(
        participants_by_chat={
            "chat": FlakyAsyncIterator(
                [FakeUser(id=1), FakeUser(id=2)], fail_at=2, exc=FloodWaitError(None)
            )
        }
    )

    result = await collect_chat_participant_ids(client, "chat")

    assert result == {1, 2}
    assert sleeps


@pytest.mark.asyncio
async def test_collect_chat_sender_ids_gives_up_after_max_retries_without_raising(monkeypatch):
    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", lambda seconds: _noop())

    client = FakeTelegramClient(messages_by_chat={"chat": AlwaysFloodyIterator()})

    result = await collect_chat_sender_ids(client, "chat", max_retries=2)

    assert result == set()


@pytest.mark.asyncio
async def test_collect_chat_participant_ids_gives_up_after_max_retries_without_raising(monkeypatch):
    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", lambda seconds: _noop())

    client = FakeTelegramClient(participants_by_chat={"chat": AlwaysFloodyIterator()})

    result = await collect_chat_participant_ids(client, "chat", max_retries=2)

    assert result == set()


@pytest.mark.asyncio
async def test_collect_chat_sender_ids_paces_requests_with_delay(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", fake_sleep)

    client = FakeTelegramClient(
        messages_by_chat={"chat": [make_message(1), make_message(2)]}
    )

    await collect_chat_sender_ids(client, "chat", delay=0.3)

    assert sleeps.count(0.3) == 2


@pytest.mark.asyncio
async def test_collect_chat_participant_ids_paces_requests_with_delay(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", fake_sleep)

    client = FakeTelegramClient(
        participants_by_chat={"chat": [FakeUser(id=1), FakeUser(id=2)]}
    )

    await collect_chat_participant_ids(client, "chat", delay=0.3)

    assert sleeps.count(0.3) == 2


@pytest.mark.asyncio
async def test_fetch_account_maps_profile_fields():
    profile = FakeFullUser(
        users=[FakeUser(id=5, username="ada", first_name="Ada", last_name="Lovelace", phone="+1", bot=False)],
        full_user=SimpleNamespace(about="Mathematician"),
    )
    client = FakeTelegramClient(profiles={5: profile})

    account = await fetch_account(client, 5)

    assert account.id == 5
    assert account.username == "ada"
    assert account.bio == "Mathematician"
    assert account.is_bot is False


@pytest.mark.asyncio
async def test_fetch_account_retries_on_flood_wait_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("tg_scraper.collector.asyncio.sleep", fake_sleep)

    calls = {"count": 0}

    class FloodyClient(FakeTelegramClient):
        async def __call__(self, request):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FloodWaitError(None)
            return await super().__call__(request)

    client = FloodyClient(profiles={5: FakeFullUser(users=[FakeUser(id=5)], full_user=SimpleNamespace(about=None))})

    account = await fetch_account(client, 5)

    assert account is not None
    assert calls["count"] == 2
    assert sleeps  # slept at least once before retrying


async def _collect(client, chats, **kwargs):
    """Drain the collect_accounts async generator into a dict, like the old API did."""
    kwargs.setdefault("list_delay", 0)
    return {account.id: account async for account in collect_accounts(client, chats, **kwargs)}


@pytest.mark.asyncio
async def test_collect_accounts_streams_rather_than_returning_a_coroutine():
    client = FakeTelegramClient(participants_by_chat={"chat": [FakeUser(id=1)]})

    result = collect_accounts(client, ["chat"], delay=0, list_delay=0)

    assert hasattr(result, "__anext__")  # it's an async generator, not an awaitable
    accounts = [account async for account in result]
    assert [a.id for a in accounts] == [1]


@pytest.mark.asyncio
async def test_collect_accounts_merges_message_and_participant_sources():
    client = FakeTelegramClient(
        messages_by_chat={"chat": [make_message(1)]},
        participants_by_chat={"chat": [FakeUser(id=1), FakeUser(id=2)]},
    )

    accounts = await _collect(client, ["chat"], delay=0)

    assert set(accounts) == {1, 2}
    assert accounts[1].sources == {SOURCE_MESSAGES, SOURCE_PARTICIPANTS}
    assert accounts[2].sources == {SOURCE_PARTICIPANTS}
    assert accounts[1].seen_in_chats == {"chat"}


@pytest.mark.asyncio
async def test_collect_accounts_can_disable_participant_listing():
    client = FakeTelegramClient(
        messages_by_chat={"chat": [make_message(1)]},
        participants_by_chat={"chat": [FakeUser(id=1), FakeUser(id=2)]},
    )

    accounts = await _collect(client, ["chat"], include_participants=False, delay=0)

    assert set(accounts) == {1}
    assert accounts[1].sources == {SOURCE_MESSAGES}


@pytest.mark.asyncio
async def test_collect_accounts_requires_at_least_one_source():
    client = FakeTelegramClient()
    with pytest.raises(ValueError):
        await _collect(
            client, ["chat"], include_message_senders=False, include_participants=False
        )


@pytest.mark.asyncio
async def test_collect_accounts_skips_known_ids_without_fetching_them():
    client = FakeTelegramClient(
        participants_by_chat={"chat": [FakeUser(id=1), FakeUser(id=2), FakeUser(id=3)]},
    )

    accounts = await _collect(client, ["chat"], known_ids={1, 2}, delay=0)

    # known ids are still yielded (so the caller can update their chat list),
    # but only as a bare id + chat/source delta, never with a re-fetched profile
    assert set(accounts) == {1, 2, 3}
    assert accounts[1].username is None
    assert accounts[1].seen_in_chats == {"chat"}
    assert accounts[3].sources == {SOURCE_PARTICIPANTS}

    # checkpointed accounts must never trigger a GetFullUserRequest call
    fetched_ids = {call.id for call in client.call_log}
    assert fetched_ids == {3}


@pytest.mark.asyncio
async def test_collect_accounts_yields_as_it_goes_not_all_at_once():
    client = FakeTelegramClient(
        participants_by_chat={"chat": [FakeUser(id=1), FakeUser(id=2)]},
    )

    seen = []
    async for account in collect_accounts(client, ["chat"], delay=0, list_delay=0):
        seen.append(account.id)
        # after the first yield, the second profile shouldn't have been
        # fetched yet -- proves this streams instead of batching internally
        if len(seen) == 1:
            assert len(client.call_log) == 1

    assert set(seen) == {1, 2}