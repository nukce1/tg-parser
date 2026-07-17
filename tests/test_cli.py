from pathlib import Path

from click.testing import CliRunner

import tg_scraper.cli as cli_module
from tg_scraper.cli import main
from tg_scraper.config import Settings
from tg_scraper.models import Account
from tg_scraper.storage import load_accounts, save_accounts


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_settings(monkeypatch):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(1, "hash", "session")))
    monkeypatch.setattr(cli_module, "build_client", lambda settings: FakeClient())


def test_collect_writes_each_account_as_it_streams_in(monkeypatch, tmp_path: Path):
    _patch_settings(monkeypatch)
    output = tmp_path / "accounts.jsonl"

    async def fake_collect_accounts(client, chats, **kwargs):
        for account in [Account(id=1, username="a"), Account(id=2, username="b")]:
            yield account

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "somechat", "--output", str(output)])

    assert result.exit_code == 0, result.output
    saved = load_accounts(output)
    assert set(saved) == {1, 2}


def test_collect_skips_known_ids_and_appends(monkeypatch, tmp_path: Path):
    _patch_settings(monkeypatch)
    output = tmp_path / "accounts.jsonl"
    save_accounts(output, [Account(id=1, username="already-done")])

    captured_known_ids = {}

    async def fake_collect_accounts(client, chats, *, known_ids=None, **kwargs):
        captured_known_ids["value"] = known_ids
        yield Account(id=2, username="new")

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "somechat", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert captured_known_ids["value"] == {1}

    saved = load_accounts(output)
    assert set(saved) == {1, 2}
    assert saved[1].username == "already-done"


def test_collect_never_drops_existing_accounts_from_a_different_chat(monkeypatch, tmp_path: Path):
    output = tmp_path / "accounts.jsonl"
    save_accounts(output, [Account(id=99, username="stale", seen_in_chats={"old_chat"})])
    _patch_settings(monkeypatch)

    async def fake_collect_accounts(client, chats, **kwargs):
        yield Account(id=2, username="fresh")

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "newchat", "--output", str(output)])

    assert result.exit_code == 0, result.output
    saved = load_accounts(output)
    assert set(saved) == {99, 2}
    assert saved[99].username == "stale"


def test_collect_merges_chat_list_when_a_known_account_reappears(monkeypatch, tmp_path: Path):
    output = tmp_path / "accounts.jsonl"
    save_accounts(
        output,
        [Account(id=1, username="bob", seen_in_chats={"chat_a"}, sources={"participants"})],
    )
    _patch_settings(monkeypatch)

    async def fake_collect_accounts(client, chats, **kwargs):
        # a known account re-seen in a new chat: bare id + chat/source delta, no profile
        yield Account(id=1, seen_in_chats={"chat_b"}, sources={"messages"})

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "chat_b", "--output", str(output)])

    assert result.exit_code == 0, result.output
    saved = load_accounts(output)
    assert set(saved) == {1}
    assert saved[1].username == "bob"
    assert saved[1].seen_in_chats == {"chat_a", "chat_b"}
    assert saved[1].sources == {"participants", "messages"}