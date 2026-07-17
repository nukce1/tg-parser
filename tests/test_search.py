from tg_scraper.models import Account
from tg_scraper.search import search_by_keywords


def make_accounts():
    return [
        Account(id=1, username="a", bio="Crypto trader and NFT collector"),
        Account(id=2, username="b", bio="Software manager at a bank"),
        Account(id=3, username="c", bio=None),
        Account(id=4, username="d", bio="Loves crypto AND management"),
    ]


def test_any_keyword_matches_by_default():
    accounts = make_accounts()
    matches = search_by_keywords(accounts, ["crypto", "manager"])
    assert {a.id for a in matches} == {1, 2, 4}


def test_match_all_requires_every_keyword():
    accounts = make_accounts()
    matches = search_by_keywords(accounts, ["crypto", "manage"], match_all=True)
    assert {a.id for a in matches} == {4}


def test_accounts_without_bio_are_skipped():
    accounts = make_accounts()
    matches = search_by_keywords(accounts, ["crypto"])
    assert 3 not in {a.id for a in matches}


def test_case_sensitivity():
    accounts = [Account(id=1, bio="Crypto Trader")]
    assert search_by_keywords(accounts, ["crypto"], case_sensitive=True) == []
    assert len(search_by_keywords(accounts, ["Crypto"], case_sensitive=True)) == 1


def test_regex_mode():
    accounts = [Account(id=1, bio="contact: user@example.com")]
    matches = search_by_keywords(accounts, [r"\w+@\w+\.\w+"], regex=True)
    assert len(matches) == 1


def test_empty_keywords_returns_no_matches():
    assert search_by_keywords(make_accounts(), []) == []