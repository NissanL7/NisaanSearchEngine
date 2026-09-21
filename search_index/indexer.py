"""Synchronize NisaanSearchEngine pages into a Meilisearch index."""
from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

MEILI_URL = os.getenv("MEILISEARCH_URL", "http://localhost:7700").rstrip("/")
MEILI_KEY = os.getenv("MEILISEARCH_MASTER_KEY", "")
INDEX_NAME = os.getenv("MEILISEARCH_INDEX", "pages")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if MEILI_KEY:
        headers["Authorization"] = f"Bearer {MEILI_KEY}"
    return headers


def configure_index(client: httpx.Client) -> None:
    client.patch(
        f"{MEILI_URL}/indexes/{INDEX_NAME}/settings",
        headers=_headers(),
        json={
            "searchableAttributes": ["title", "description", "content", "domain"],
            "filterableAttributes": ["domain", "language", "status_code"],
            "sortableAttributes": ["crawled_at", "word_count"],
            "displayedAttributes": [
                "id", "url", "title", "description", "content", "domain",
                "language", "status_code", "crawled_at", "word_count"
            ],
        },
        timeout=20,
    ).raise_for_status()


def index_pages(pages: list[dict[str, Any]]) -> dict[str, Any]:
    if not pages:
        return {"indexed": 0, "task": None}

    documents = []
    for page in pages:
        documents.append({
            "id": page["id"],
            "url": page["url"],
            "title": page.get("title") or "",
            "description": page.get("description") or "",
            "content": page.get("content") or "",
            "domain": page.get("domain") or "",
            "language": page.get("language"),
            "status_code": page.get("status_code"),
            "crawled_at": page.get("crawled_at"),
            "word_count": page.get("word_count", 0),
        })

    with httpx.Client() as client:
        configure_index(client)
        response = client.post(
            f"{MEILI_URL}/indexes/{INDEX_NAME}/documents",
            headers=_headers(),
            params={"primaryKey": "id"},
            json=documents,
            timeout=60,
        )
        response.raise_for_status()
        return {"indexed": len(documents), "task": response.json()}


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    with httpx.Client() as client:
        response = client.post(
            f"{MEILI_URL}/indexes/{INDEX_NAME}/search",
            headers=_headers(),
            json={"q": query, "limit": limit, "offset": offset},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
