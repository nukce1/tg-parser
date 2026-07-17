FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml poetry.lock ./
COPY tg_scraper ./tg_scraper
COPY webapp ./webapp

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8989"]