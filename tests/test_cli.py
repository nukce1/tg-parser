from pathlib import Path

from click.testing import CliRunner

import tg_scraper.cli as cli_module
from tg_scraper.cli import main
from tg_scraper.config import Settings
from tg_scraper.models import Account, ChatStatus
from tg_scraper.storage import connect, load_accounts, load_chat_statuses, upsert_account


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_settings(monkeypatch):
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(1, "hash", "session")))
    monkeypatch.setattr(cli_module, "build_client", lambda settings: FakeClient())


def _load(db_path: Path) -> dict[int, Account]:
    conn = connect(db_path)
    try:
        return load_accounts(conn)
    finally:
        conn.close()


def test_collect_writes_each_account_as_it_streams_in(monkeypatch, tmp_path: Path):
    _patch_settings(monkeypatch)
    db_path = tmp_path / "accounts.db"

    async def fake_collect_accounts(client, chats, **kwargs):
        for account in [Account(id=1, username="a"), Account(id=2, username="b")]:
            yield account

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "somechat", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    saved = _load(db_path)
    assert set(saved) == {1, 2}


def test_collect_never_drops_existing_accounts_from_a_different_chat(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "accounts.db"
    conn = connect(db_path)
    upsert_account(conn, Account(id=99, username="stale", seen_in_chats={"old_chat"}))
    conn.close()
    _patch_settings(monkeypatch)

    async def fake_collect_accounts(client, chats, **kwargs):
        yield Account(id=2, username="fresh")

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "newchat", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    saved = _load(db_path)
    assert set(saved) == {99, 2}
    assert saved[99].username == "stale"


def test_collect_merges_chat_list_when_a_known_account_reappears(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "accounts.db"
    conn = connect(db_path)
    upsert_account(conn, Account(id=1, username="bob", seen_in_chats={"chat_a"}, sources={"participants"}))
    conn.close()
    _patch_settings(monkeypatch)

    async def fake_collect_accounts(client, chats, **kwargs):
        # a known account re-seen in a new chat: bare id + chat/source delta, no profile
        yield Account(id=1, seen_in_chats={"chat_b"}, sources={"messages"})

    monkeypatch.setattr(cli_module, "collect_accounts", fake_collect_accounts)

    result = CliRunner().invoke(main, ["collect", "--chat", "chat_b", "--db", str(db_path)])

    assert result.exit_code == 0, result.output
    saved = _load(db_path)
    assert set(saved) == {1}
    assert saved[1].username == "bob"
    assert saved[1].seen_in_chats == {"chat_a", "chat_b"}
    assert saved[1].sources == {"participants", "messages"}


def test_migrate_imports_legacy_jsonl_files_into_db(tmp_path: Path):
    accounts_path = tmp_path / "accounts.jsonl"
    accounts_path.write_text(
        '{"id": 1, "username": "ada", "bio": "mathematician", "seen_in_chats": ["chat_a"], "sources": ["participants"]}\n'
        '{"id": 2, "username": "grace", "seen_in_chats": ["chat_a"], "sources": ["messages"]}\n',
        encoding="utf-8",
    )
    chats_path = tmp_path / "chats.jsonl"
    chats_path.write_text('{"chat": "chat_a", "success": true, "reason": null}\n', encoding="utf-8")
    db_path = tmp_path / "accounts.db"

    result = CliRunner().invoke(
        main,
        [
            "migrate",
            "--accounts-input",
            str(accounts_path),
            "--chats-input",
            str(chats_path),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    saved = _load(db_path)
    assert set(saved) == {1, 2}
    assert saved[1].bio == "mathematician"

    conn = connect(db_path)
    try:
        statuses = load_chat_statuses(conn)
    finally:
        conn.close()
    assert statuses["chat_a"] == ChatStatus("chat_a", True, None)


def test_migrate_merges_into_a_db_with_existing_overlapping_accounts(tmp_path: Path):
    accounts_path = tmp_path / "accounts.jsonl"
    accounts_path.write_text(
        '{"id": 1, "seen_in_chats": ["chat_b"], "sources": ["messages"]}\n',
        encoding="utf-8",
    )
    chats_path = tmp_path / "chats.jsonl"
    chats_path.write_text("", encoding="utf-8")
    db_path = tmp_path / "accounts.db"
    conn = connect(db_path)
    upsert_account(conn, Account(id=1, username="ada", seen_in_chats={"chat_a"}, sources={"participants"}))
    conn.close()

    result = CliRunner().invoke(
        main,
        [
            "migrate",
            "--accounts-input",
            str(accounts_path),
            "--chats-input",
            str(chats_path),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    saved = _load(db_path)
    assert saved[1].username == "ada"
    assert saved[1].seen_in_chats == {"chat_a", "chat_b"}
    assert saved[1].sources == {"participants", "messages"}