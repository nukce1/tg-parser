from tg_scraper.models import Account


def test_display_name_prefers_full_name():
    account = Account(id=1, first_name="Ada", last_name="Lovelace", username="ada")
    assert account.display_name == "Ada Lovelace"


def test_display_name_falls_back_to_username():
    account = Account(id=1, username="ada")
    assert account.display_name == "@ada"


def test_display_name_falls_back_to_id():
    account = Account(id=42)
    assert account.display_name == "42"


def test_merge_fills_missing_fields_and_unions_sets():
    base = Account(id=1, username="ada", seen_in_chats={"chat_a"}, sources={"messages"})
    other = Account(
        id=1,
        first_name="Ada",
        bio="Mathematician",
        seen_in_chats={"chat_b"},
        sources={"participants"},
    )

    base.merge(other)

    assert base.username == "ada"
    assert base.first_name == "Ada"
    assert base.bio == "Mathematician"
    assert base.seen_in_chats == {"chat_a", "chat_b"}
    assert base.sources == {"messages", "participants"}


def test_to_dict_from_dict_roundtrip():
    account = Account(
        id=7,
        username="grace",
        first_name="Grace",
        last_name="Hopper",
        phone="+123",
        bio="Compiler pioneer",
        is_bot=False,
        seen_in_chats={"chat_a", "chat_b"},
        sources={"messages"},
    )

    restored = Account.from_dict(account.to_dict())

    assert restored == account