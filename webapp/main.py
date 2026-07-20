"""FastAPI frontend exposing tg_scraper's bio keyword search over the web.

Reuses tg_scraper.storage/search as-is (the same functions the `tg-scraper
search` CLI command calls) — this is a read-only view over an
already-collected accounts.db, it never touches the Telegram API itself.
"""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook

from tg_scraper.models import Account
from tg_scraper.search import search_by_keywords
from tg_scraper.storage import connect, load_accounts

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = Path(os.getenv("TG_SCRAPER_ACCOUNTS_PATH", "accounts.db"))
DISPLAY_LIMIT = 100

app = FastAPI(title="TG Scraper Search")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def parse_keywords(raw: str) -> list[str]:
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


def run_search(
    keywords_raw: str,
    *,
    match_all: bool,
    case_sensitive: bool,
    regex: bool,
    whole_word: bool,
    search_username: bool,
) -> tuple[list[Account], int, list[str]]:
    """Load accounts.db and return (matches, total_accounts_on_file, keywords)."""
    keywords = parse_keywords(keywords_raw)
    conn = connect(ACCOUNTS_PATH)
    try:
        accounts = load_accounts(conn)
    finally:
        conn.close()
    if not keywords:
        return [], len(accounts), keywords
    matches = search_by_keywords(
        accounts.values(),
        keywords,
        match_all=match_all,
        case_sensitive=case_sensitive,
        regex=regex,
        whole_word=whole_word,
        search_username=search_username,
    )
    return matches, len(accounts), keywords


def build_query(
    q: str, match_all: bool, case_sensitive: bool, regex: bool, whole_word: bool, search_username: bool
) -> str:
    params: dict[str, str] = {"q": q}
    if match_all:
        params["match_all"] = "true"
    if case_sensitive:
        params["case_sensitive"] = "true"
    if regex:
        params["regex"] = "true"
    if whole_word:
        params["whole_word"] = "true"
    if search_username:
        params["search_username"] = "true"
    return urlencode(params)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = Query(default=""),
    match_all: bool = Query(default=False),
    case_sensitive: bool = Query(default=False),
    regex: bool = Query(default=False),
    whole_word: bool = Query(default=False),
    search_username: bool = Query(default=False),
) -> HTMLResponse:
    searched = bool(q.strip())
    matches: list[Account] = []
    total_accounts = 0
    error: str | None = None

    if searched:
        if not ACCOUNTS_PATH.exists():
            error = f"База с аккаунтами не найдена: {ACCOUNTS_PATH}. Сначала запустите `tg-scraper collect`."
        else:
            try:
                matches, total_accounts, _ = run_search(
                    q,
                    match_all=match_all,
                    case_sensitive=case_sensitive,
                    regex=regex,
                    whole_word=whole_word,
                    search_username=search_username,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, e.g. bad regex
                error = f"Ошибка поиска: {exc}"

    display_matches = matches[:DISPLAY_LIMIT]
    truncated = len(matches) > DISPLAY_LIMIT
    download_url = "/download?" + build_query(q, match_all, case_sensitive, regex, whole_word, search_username)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "q": q,
            "match_all": match_all,
            "case_sensitive": case_sensitive,
            "regex": regex,
            "whole_word": whole_word,
            "search_username": search_username,
            "searched": searched,
            "error": error,
            "total_accounts": total_accounts,
            "match_count": len(matches),
            "matches": display_matches,
            "truncated": truncated,
            "display_limit": DISPLAY_LIMIT,
            "download_url": download_url,
        },
    )


ACCOUNT_COLUMNS = (
    "id",
    "username",
    "first_name",
    "last_name",
    "phone",
    "bio",
    "is_bot",
    "seen_in_chats",
    "sources",
)


def accounts_to_xlsx(accounts: list[Account]) -> bytes:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Accounts")
    ws.append(ACCOUNT_COLUMNS)
    for account in accounts:
        row = account.to_dict()
        ws.append(
            [
                row["id"],
                row["username"],
                row["first_name"],
                row["last_name"],
                row["phone"],
                row["bio"],
                row["is_bot"],
                ", ".join(row["seen_in_chats"]),
                ", ".join(row["sources"]),
            ]
        )
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@app.get("/download")
def download(
    q: str = Query(default=""),
    match_all: bool = Query(default=False),
    case_sensitive: bool = Query(default=False),
    regex: bool = Query(default=False),
    whole_word: bool = Query(default=False),
    search_username: bool = Query(default=False),
) -> Response:
    """Export every matching account (no display cap) as an XLSX file."""
    matches: list[Account] = []
    if q.strip() and ACCOUNTS_PATH.exists():
        matches, _, _ = run_search(
            q,
            match_all=match_all,
            case_sensitive=case_sensitive,
            regex=regex,
            whole_word=whole_word,
            search_username=search_username,
        )

    payload = accounts_to_xlsx(matches)
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="search_results.xlsx"'},
    )


def run() -> None:
    """Entry point for the `tg-scraper-web` script: `poetry run tg-scraper-web`."""
    import uvicorn

    uvicorn.run("webapp.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    run()