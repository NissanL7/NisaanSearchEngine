# NisaanSearchEngine crawler

The V1 crawler is intentionally controlled. It:

- only follows HTTP(S) URLs;
- removes fragments and normalizes URLs;
- stays on the seed domains;
- checks `robots.txt` before fetching;
- uses a descriptive User-Agent;
- waits between requests;
- only extracts HTML pages;
- limits response size;
- avoids duplicate URLs;
- supports a maximum page count and crawl depth.

## Local test

```bash
cd crawler
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python crawler.py https://example.com --max-pages 5 --max-depth 1
```

## Persist pages

Set the server-side `DATABASE_URL`, then run:

```bash
python run_crawl.py https://example.com --max-pages 10 --max-depth 1
```

This upserts crawled HTML documents into the PostgreSQL/Supabase `pages` table. Never commit database credentials.

The next stage is synchronizing these documents into the search index.
