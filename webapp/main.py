"""FastAPI frontend exposing tg_scraper's bio keyword search over the web.

Reuses tg_scraper.storage/search as-is (the same functions the `tg-scraper
search` CLI command calls) — this is a read-only view over an
already-collected accounts.jsonl, it never touches the Telegram API itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tg_scraper.models import Account
from tg_scraper.search import search_by_keywords
from tg_scraper.storage import load_accounts

BASE_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = Path(os.getenv("TG_SCRAPER_ACCOUNTS_PATH", "accounts.jsonl"))
DISPLAY_LIMIT = 100

app = FastAPI(title="TG Scraper Search")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def parse_keywords(raw: str) -> list[str]:
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


def run_search(
    keywords_raw: str, *, match_all: bool, case_sensitive: bool, regex: bool, whole_word: bool
) -> tuple[list[Account], int, list[str]]:
    """Load accounts.jsonl and return (matches, total_accounts_on_file, keywords)."""
    keywords = parse_keywords(keywords_raw)
    accounts = load_accounts(ACCOUNTS_PATH)
    if not keywords:
        return [], len(accounts), keywords
    matches = search_by_keywords(
        accounts.values(),
        keywords,
        match_all=match_all,
        case_sensitive=case_sensitive,
        regex=regex,
        whole_word=whole_word,
    )
    return matches, len(accounts), keywords


def build_query(q: str, match_all: bool, case_sensitive: bool, regex: bool, whole_word: bool) -> str:
    params: dict[str, str] = {"q": q}
    if match_all:
        params["match_all"] = "true"
    if case_sensitive:
        params["case_sensitive"] = "true"
    if regex:
        params["regex"] = "true"
    if whole_word:
        params["whole_word"] = "true"
    return urlencode(params)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = Query(default=""),
    match_all: bool = Query(default=False),
    case_sensitive: bool = Query(default=False),
    regex: bool = Query(default=False),
    whole_word: bool = Query(default=False),
) -> HTMLResponse:
    searched = bool(q.strip())
    matches: list[Account] = []
    total_accounts = 0
    error: str | None = None

    if searched:
        if not ACCOUNTS_PATH.exists():
            error = f"Файл с аккаунтами не найден: {ACCOUNTS_PATH}. Сначала запустите `tg-scraper collect`."
        else:
            try:
                matches, total_accounts, _ = run_search(
                    q,
                    match_all=match_all,
                    case_sensitive=case_sensitive,
                    regex=regex,
                    whole_word=whole_word,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the user, e.g. bad regex
                error = f"Ошибка поиска: {exc}"

    display_matches = matches[:DISPLAY_LIMIT]
    truncated = len(matches) > DISPLAY_LIMIT
    download_url = "/download?" + build_query(q, match_all, case_sensitive, regex, whole_word)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "q": q,
            "match_all": match_all,
            "case_sensitive": case_sensitive,
            "regex": regex,
            "whole_word": whole_word,
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


@app.get("/download")
def download(
    q: str = Query(default=""),
    match_all: bool = Query(default=False),
    case_sensitive: bool = Query(default=False),
    regex: bool = Query(default=False),
    whole_word: bool = Query(default=False),
) -> Response:
    """Export every matching account (no display cap) as a JSON file."""
    matches: list[Account] = []
    if q.strip() and ACCOUNTS_PATH.exists():
        matches, _, _ = run_search(
            q, match_all=match_all, case_sensitive=case_sensitive, regex=regex, whole_word=whole_word
        )

    payload = json.dumps([account.to_dict() for account in matches], ensure_ascii=False, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="search_results.json"'},
    )


def run() -> None:
    """Entry point for the `tg-scraper-web` script: `poetry run tg-scraper-web`."""
    import uvicorn

    uvicorn.run("webapp.main:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    run()