"""Search adapter with local index plus optional live Brave web search.

Local results remain the project's own index. When BRAVE_SEARCH_API_KEY is
configured, live web results are used to fill searches that the local index
cannot satisfy. Live provider results are not persisted.
"""
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
BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")


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


def _local_search(query: str, limit: int, offset: int) -> dict[str, Any]:
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


def _brave_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    """Search the live public web through Brave's independent web index.

    Results are returned directly and deliberately not persisted. Brave's
    current API terms restrict retaining API response data unless the account
    has the appropriate storage rights.
    """
    if not BRAVE_API_KEY:
        return {"estimatedTotalHits": 0, "hits": []}

    # Brave uses 1-based pagination. Keep this endpoint bounded for V1.
    page = (offset // max(limit, 1)) + 1
    params = {
        "q": query,
        "count": str(min(limit, 20)),
        "offset": str(max(page - 1, 0) * min(limit, 20)),
        "safesearch": "moderate",
    }
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    with httpx.Client(timeout=20) as client:
        response = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    web = data.get("web", {})
    raw_results = web.get("results", [])
    hits = []
    for index, item in enumerate(raw_results):
        url = item.get("url") or ""
        hits.append(
            {
                "id": f"web-{offset + index}-{abs(hash(url))}",
                "url": url,
                "title": item.get("title") or url,
                "description": item.get("description") or "",
                "content": item.get("description") or "",
                "domain": item.get("profile", {}).get("long_name") or "",
                "source": "web",
            }
        )
    return {
        "estimatedTotalHits": int(web.get("totalEstimatedMatches") or len(hits)),
        "hits": hits,
    }


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Hybrid search: own index first, then live web coverage when enabled."""
    local = _local_search(query, limit, offset)
    local_hits = local.get("hits", [])

    # Keep our own index authoritative when it has matching pages. If it has
    # no matches, use the live web index so ordinary queries don't look empty.
    if local_hits or not BRAVE_API_KEY:
        return local

    return _brave_search(query, limit, offset)
