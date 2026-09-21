"""Database persistence for the NisaanSearchEngine crawler."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
from dotenv import load_dotenv

from crawler import Page

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


class PageStore:
    def __init__(self, database_url: str = DATABASE_URL) -> None:
        self.database_url = database_url

    def save_page(self, page: Page) -> None:
        crawled_at = datetime.now(timezone.utc)
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pages (
                        url, canonical_url, domain, title, description, content,
                        status_code, content_hash, word_count, crawled_at, updated_at
                    ) VALUES (
                        %(url)s, %(url)s, %(domain)s, %(title)s, %(description)s,
                        %(content)s, %(status_code)s, %(content_hash)s,
                        %(word_count)s, %(crawled_at)s, %(crawled_at)s
                    )
                    ON CONFLICT (url) DO UPDATE SET
                        canonical_url = EXCLUDED.canonical_url,
                        domain = EXCLUDED.domain,
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        content = EXCLUDED.content,
                        status_code = EXCLUDED.status_code,
                        content_hash = EXCLUDED.content_hash,
                        word_count = EXCLUDED.word_count,
                        crawled_at = EXCLUDED.crawled_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    {
                        "url": page.url,
                        "domain": page.domain,
                        "title": page.title,
                        "description": page.description,
                        "content": page.content,
                        "status_code": page.status_code,
                        "content_hash": page.content_hash,
                        "word_count": page.word_count,
                        "crawled_at": crawled_at,
                    },
                )
            conn.commit()

    def save_pages(self, pages: list[Page]) -> int:
        for page in pages:
            self.save_page(page)
        return len(pages)
