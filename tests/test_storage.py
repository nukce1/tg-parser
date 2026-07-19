from pathlib import Path

from tg_scraper.models import Account, ChatStatus
from tg_scraper.storage import (
    connect,
    get_known_ids,
    iter_accounts,
    iter_accounts_jsonl,
    iter_chat_statuses_jsonl,
    load_accounts,
    load_chat_statuses,
    upsert_account,
    upsert_chat_status,
)


def test_upsert_and_load_roundtrip(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    accounts = {
        1: Account(id=1, username="ada", bio="Mathematician", seen_in_chats={"chat_a"}),
        2: Account(id=2, username="grace", bio=None, sources={"participants"}),
    }
    for account in accounts.values():
        upsert_account(conn, account)

    assert load_accounts(conn) == accounts


def test_upsert_reports_new_vs_updated(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")

    assert upsert_account(conn, Account(id=1, username="ada")) == "new"
    assert upsert_account(conn, Account(id=1, username="ada")) == "updated"


def test_upsert_merges_without_clobbering_existing_fields(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    upsert_account(conn, Account(id=1, username="ada", bio="Mathematician", seen_in_chats={"chat_a"}))

    # a later sighting with no bio/username shouldn't erase the ones already stored
    upsert_account(conn, Account(id=1, seen_in_chats={"chat_b"}, sources={"messages"}))

    saved = load_accounts(conn)[1]
    assert saved.username == "ada"
    assert saved.bio == "Mathematician"
    assert saved.seen_in_chats == {"chat_a", "chat_b"}
    assert saved.sources == {"messages"}


def test_upsert_is_bot_only_ever_turns_true(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    upsert_account(conn, Account(id=1, is_bot=True))
    upsert_account(conn, Account(id=1, is_bot=False))

    assert load_accounts(conn)[1].is_bot is True


def test_get_known_ids(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    upsert_account(conn, Account(id=1))
    upsert_account(conn, Account(id=2))

    assert get_known_ids(conn) == {1, 2}


def test_iter_accounts_on_empty_db_yields_nothing(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    assert list(iter_accounts(conn)) == []


def test_load_accounts_on_empty_db_returns_empty_dict(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    assert load_accounts(conn) == {}


def test_chat_status_upsert_and_load(tmp_path: Path):
    conn = connect(tmp_path / "accounts.db")
    upsert_chat_status(conn, ChatStatus("chat_a", False, "join request sent"))
    upsert_chat_status(conn, ChatStatus("chat_a", True))
    upsert_chat_status(conn, ChatStatus("chat_b", False, "private"))

    statuses = load_chat_statuses(conn)
    assert statuses["chat_a"] == ChatStatus("chat_a", True, None)
    assert statuses["chat_b"] == ChatStatus("chat_b", False, "private")


def test_iter_accounts_jsonl_reads_legacy_file(tmp_path: Path):
    path = tmp_path / "accounts.jsonl"
    path.write_text(
        '{"id": 1, "username": "ada", "seen_in_chats": ["chat_a"], "sources": []}\n',
        encoding="utf-8",
    )

    accounts = list(iter_accounts_jsonl(path))

    assert accounts == [Account(id=1, username="ada", seen_in_chats={"chat_a"})]


def test_iter_accounts_jsonl_on_missing_file_yields_nothing(tmp_path: Path):
    assert list(iter_accounts_jsonl(tmp_path / "does-not-exist.jsonl")) == []


def test_iter_chat_statuses_jsonl_reads_legacy_file(tmp_path: Path):
    path = tmp_path / "chats.jsonl"
    path.write_text('{"chat": "chat_a", "success": true, "reason": null}\n', encoding="utf-8")

    statuses = list(iter_chat_statuses_jsonl(path))

    assert statuses == [ChatStatus("chat_a", True, None)]


def test_iter_chat_statuses_jsonl_on_missing_file_yields_nothing(tmp_path: Path):
    assert list(iter_chat_statuses_jsonl(tmp_path / "does-not-exist.jsonl")) == []