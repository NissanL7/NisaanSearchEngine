"""Search adapter with Meilisearch, PostgreSQL, and Supabase REST fallbacks."""
from __future__ import annotations

import os
from typing import Any

import httpx
import psycopg

MEILI_URL = os.getenv("MEILISEARCH_URL", "").rstrip("/")
MEILI_KEY = os.getenv("MEILISEARCH_MASTER_KEY", "")
INDEX_NAME = os.getenv("MEILISEARCH_INDEX", "pages")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")


def _meili_search(query: str, limit: int, offset: int) -> dict[str, Any]:
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


def _postgres_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    pattern = f"%{query}%"
    where = "title ilike %(p)s or description ilike %(p)s or content ilike %(p)s or domain ilike %(p)s"
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select count(*) from pages where {where}", {"p": pattern})
            total = cur.fetchone()[0]
            cur.execute(
                f"""select id,url,title,description,content,domain,language,status_code,crawled_at,word_count
                from pages where {where}
                order by case when title ilike %(p)s then 0 else 1 end,
                crawled_at desc nulls last limit %(limit)s offset %(offset)s""",
                {"p": pattern, "limit": limit, "offset": offset},
            )
            columns = [d.name for d in cur.description]
            hits = [dict(zip(columns, row)) for row in cur.fetchall()]
    return {"estimatedTotalHits": total, "hits": hits}


def _supabase_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase REST configuration is missing")

    # Keep the free V1 search deliberately simple. Supabase/PostgREST performs
    # the filtering server-side; httpx handles URL encoding for the parameters.
    safe_query = query.replace("*", " ").strip()
    or_filter = (
        f"(title.ilike.*{safe_query}*,"
        f"description.ilike.*{safe_query}*,"
        f"content.ilike.*{safe_query}*,"
        f"domain.ilike.*{safe_query}*)"
    )
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    params = {
        "select": "id,url,title,description,content,domain,language,status_code,crawled_at,word_count",
        "or": or_filter,
        "limit": str(limit),
        "offset": str(offset),
        "order": "crawled_at.desc",
    }
    with httpx.Client(timeout=20) as client:
        response = client.get(f"{SUPABASE_URL}/rest/v1/pages", headers=headers, params=params)
        response.raise_for_status()
        hits = response.json()
    return {"estimatedTotalHits": len(hits) + offset, "hits": hits}


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    if MEILI_URL:
        try:
            return _meili_search(query, limit, offset)
        except Exception:
            pass
    if DATABASE_URL:
        try:
            return _postgres_search(query, limit, offset)
        except Exception:
            pass
    return _supabase_search(query, limit, offset)
