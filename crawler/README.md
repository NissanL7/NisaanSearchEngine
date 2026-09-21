# NisaanSearchEngine crawler

The V1 crawler is intentionally conservative. It:

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

Database persistence and the search index are the next stages. Do not point this crawler at unrestricted large-scale crawling jobs yet.
