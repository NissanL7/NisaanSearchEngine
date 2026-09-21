# NisaanSearchEngine

A from-scratch web search engine project by NissanL7.

## Vision

NisaanSearchEngine will crawl permitted public web pages, store normalized documents, index them for fast retrieval, expose a search API, and provide a clean search interface.

## Architecture

- `frontend/` — Next.js search interface
- `api/` — FastAPI search API
- `crawler/` — Python crawler and parser
- `database/` — PostgreSQL schema and migrations

## V1 pipeline

```text
Web → Crawler → PostgreSQL → Search Index → FastAPI → Next.js
```

## Development status

- [x] Repository initialized
- [ ] Database schema
- [ ] Crawler
- [ ] Search index
- [ ] API
- [ ] Frontend
- [ ] Deployment

## Safety and crawling policy

The crawler is intended to respect robots.txt, reasonable request rates, URL scope, and HTTP status codes. It should not bypass access controls or crawl restricted content.
