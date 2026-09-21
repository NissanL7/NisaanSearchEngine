"""Small API-local adapter for the search index."""
from __future__ import annotations

import os
from typing import Any

import httpx

MEILI_URL = os.getenv("MEILISEARCH_URL", "http://localhost:7700").rstrip("/")
MEILI_KEY = os.getenv("MEILISEARCH_MASTER_KEY", "")
INDEX_NAME = os.getenv("MEILISEARCH_INDEX", "pages")


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if MEILI_KEY:
        headers["Authorization"] = f"Bearer {MEILI_KEY}"
    with httpx.Client(timeout=20) as client:
        response = client.post(
            f"{MEILI_URL}/indexes/{INDEX_NAME}/search",
            headers=headers,
            json={"q": query, "limit": limit, "offset": offset},
        )
        response.raise_for_status()
        return response.json()
