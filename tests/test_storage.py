from pathlib import Path

from tg_scraper.models import Account
from tg_scraper.storage import append_account, iter_accounts, load_accounts, save_accounts


def test_save_and_load_roundtrip(tmp_path: Path):
    accounts = {
        1: Account(id=1, username="ada", bio="Mathematician", seen_in_chats={"chat_a"}),
        2: Account(id=2, username="grace", bio=None, sources={"participants"}),
    }
    path = tmp_path / "accounts.jsonl"

    save_accounts(path, accounts)
    loaded = load_accounts(path)

    assert loaded == accounts


def test_save_accepts_list_as_well_as_dict(tmp_path: Path):
    accounts = [Account(id=1, username="ada")]
    path = tmp_path / "accounts.jsonl"

    save_accounts(path, accounts)
    loaded = load_accounts(path)

    assert list(loaded.keys()) == [1]


def test_save_writes_one_json_object_per_line(tmp_path: Path):
    accounts = [Account(id=1), Account(id=2), Account(id=3)]
    path = tmp_path / "accounts.jsonl"

    save_accounts(path, accounts)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_append_account_adds_a_line_without_touching_existing_ones(tmp_path: Path):
    path = tmp_path / "accounts.jsonl"
    save_accounts(path, [Account(id=1, username="ada")])

    with path.open("a", encoding="utf-8") as handle:
        append_account(handle, Account(id=2, username="grace"), 0)

    loaded = load_accounts(path)
    assert set(loaded) == {1, 2}
    assert loaded[1].username == "ada"
    assert loaded[2].username == "grace"


def test_iter_accounts_on_missing_file_yields_nothing(tmp_path: Path):
    assert list(iter_accounts(tmp_path / "does-not-exist.jsonl")) == []


def test_load_accounts_on_missing_file_returns_empty_dict(tmp_path: Path):
    assert load_accounts(tmp_path / "does-not-exist.jsonl") == {}