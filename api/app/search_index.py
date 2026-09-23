"""Hybrid search for NisaanSearchEngine.

The project stays free-first: search the project's own index first, then use
Wikimedia's public search API as a no-key fallback when the local index has no
match. Optional paid/provider integrations can be added later without making
them required.
"""
from __future__ import annotations

import os
import re
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

    safe_query = query.replace("*", " ").replace(",", " ").strip()
    # Search the complete phrase first. This keeps the existing simple REST
    # index inexpensive; the public-web fallback handles queries absent locally.
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


def _wiki_language(query: str) -> str:
    # Use Hindi Wikipedia for Devanagari queries; English otherwise.
    return "hi" if re.search(r"[\u0900-\u097F]", query) else "en"


def _wikipedia_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    """Free no-key fallback using Wikimedia's public MediaWiki search API."""
    language = _wiki_language(query)
    # MediaWiki's search endpoint has a bounded page size; keep V1 cheap.
    page_limit = min(max(limit, 1), 20)
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": page_limit,
        "sroffset": max(offset, 0),
        "srprop": "snippet",
        "format": "json",
        "formatversion": "2",
    }
    headers = {
        "User-Agent": "NisaanSearchEngine/1.0 (https://github.com/NissanL7/NisaanSearchEngine)"
    }
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        response = client.get(
            f"https://{language}.wikipedia.org/w/api.php",
            params=params,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()

    query_data = data.get("query", {})
    raw_results = query_data.get("search", [])
    hits = []
    for item in raw_results:
        title = item.get("title") or "Wikipedia"
        page_id = item.get("pageid")
        url = f"https://{language}.wikipedia.org/wiki/{title.replace(' ', '_')}"
        snippet = re.sub(r"<[^>]+>", "", item.get("snippet") or "").strip()
        hits.append(
            {
                "id": f"wiki-{page_id or abs(hash(url))}",
                "url": url,
                "title": title,
                "description": snippet,
                "content": snippet,
                "domain": f"{language}.wikipedia.org",
                "language": language,
                "source": "wikipedia",
            }
        )

    return {
        "estimatedTotalHits": int(query_data.get("searchinfo", {}).get("totalhits") or len(hits)),
        "hits": hits,
    }


def _brave_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    """Optional live-web provider. Never required for the free setup."""
    if not BRAVE_API_KEY:
        return {"estimatedTotalHits": 0, "hits": []}

    page_size = min(max(limit, 1), 20)
    params = {
        "q": query,
        "count": str(page_size),
        "offset": str(max(offset, 0)),
        "safesearch": "moderate",
    }
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    with httpx.Client(timeout=20) as client:
        response = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    web = data.get("web", {})
    hits = []
    for index, item in enumerate(web.get("results", [])):
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
    """Search local index first, then free Wikimedia, then optional web provider."""
    local = _local_search(query, limit, offset)
    local_hits = local.get("hits", [])
    if local_hits:
        return local

    try:
        wiki = _wikipedia_search(query, limit, offset)
        if wiki.get("hits"):
            return wiki
    except Exception:
        pass

    if BRAVE_API_KEY:
        try:
            return _brave_search(query, limit, offset)
        except Exception:
            pass

    return local
