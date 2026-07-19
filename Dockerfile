FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml poetry.lock ./
COPY tg_scraper ./tg_scraper
COPY webapp ./webapp
COPY main.py ./
COPY .env ./
COPY collector-entrypoint.sh ./

RUN pip install --no-cache-dir . && chmod +x collector-entrypoint.sh

EXPOSE 8000

CMD ["uvicorn", "webapp.main:app", "--host", "0.0.0.0", "--port", "8989"]