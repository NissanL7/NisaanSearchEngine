"""Search NisaanSearchEngine's own index, with an optional web provider.

The primary source is always the independently crawled Nisaan index. A web
provider is only used when BRAVE_SEARCH_API_KEY is explicitly configured;
Wikipedia is not used as a hidden search backend.
"""
from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
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
    safe_query = safe_query.replace("%", " ").replace("(", " ").replace(")", " ")
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
    if not BRAVE_API_KEY:
        return {"estimatedTotalHits": 0, "hits": []}
    page_size = min(max(limit, 1), 20)
    params = {"q": query, "count": str(page_size), "offset": max(offset, 0), "safesearch": "moderate"}
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    with httpx.Client(timeout=20) as client:
        response = client.get("https://api.search.brave.com/res/v1/web/search", headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
    web = data.get("web", {})
    hits = []
    for index, item in enumerate(web.get("results", [])):
        url = item.get("url") or ""
        hits.append({
            "id": f"web-{offset + index}-{abs(hash(url))}",
            "url": url,
            "title": item.get("title") or url,
            "description": item.get("description") or "",
            "content": item.get("description") or "",
            "domain": item.get("profile", {}).get("long_name") or "",
            "source": "web-provider",
        })
    return {"estimatedTotalHits": int(web.get("totalEstimatedMatches") or len(hits)), "hits": hits}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u0900-\u097F]+", text.lower(), flags=re.UNICODE)


def _score_hit(hit: dict[str, Any], query: str) -> float:
    q = query.lower().strip()
    q_tokens = list(dict.fromkeys(_tokens(q)))
    title = str(hit.get("title") or "").lower()
    description = str(hit.get("description") or "").lower()
    content = str(hit.get("content") or "").lower()
    domain = str(hit.get("domain") or "").lower()
    haystack = f"{title} {description} {content}"

    score = 0.0
    if q and q in title:
        score += 60.0
    if q and q in domain:
        score += 35.0
    if q and q in description:
        score += 20.0
    if q and q in content:
        score += 8.0

    title_tokens = _tokens(title)
    domain_tokens = _tokens(domain)
    description_tokens = _tokens(description)
    content_tokens = _tokens(content)
    for token in q_tokens:
        if token in title_tokens:
            score += 28.0
        if token in domain_tokens:
            score += 14.0
        if token in description_tokens:
            score += 8.0
        if token in content_tokens:
            score += min(12.0, 2.0 + content.count(token) * 1.5)

    coverage = sum(1 for token in q_tokens if token in haystack)
    if q_tokens:
        score += 25.0 * coverage / len(q_tokens)

    if str(hit.get("source") or "local") == "local":
        score += 6.0

    if q and title:
        ratio = SequenceMatcher(None, q, title).ratio()
        if ratio >= 0.55:
            score += ratio * 8.0
    return score


def _rank_hits(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    ranked = []
    for hit in hits:
        item = dict(hit)
        item["_score"] = round(_score_hit(item, query), 3)
        ranked.append(item)
    ranked.sort(key=lambda item: item.get("_score", 0), reverse=True)
    for item in ranked:
        item.pop("_score", None)
    return ranked


def _merge_hits(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for hit in [*primary, *secondary]:
        url = str(hit.get("url") or "").rstrip("/")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(hit)
        if len(merged) >= limit:
            break
    return merged


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Search Nisaan's own index, optionally supplemented by a configured web provider."""
    query = query.strip()
    if not query:
        return {"estimatedTotalHits": 0, "hits": []}

    candidate_limit = min(max(limit + offset, 20), 100)
    local = _local_search(query, candidate_limit, 0)
    local_hits = local.get("hits", [])
    for hit in local_hits:
        hit.setdefault("source", "local")

    merged = list(local_hits)
    web = {"estimatedTotalHits": 0, "hits": []}
    if BRAVE_API_KEY and len(merged) < candidate_limit:
        try:
            web = _brave_search(query, candidate_limit, 0)
            merged = _merge_hits(merged, web.get("hits", []), candidate_limit)
        except Exception:
            pass

    ranked = _rank_hits(merged, query)
    page = ranked[offset:offset + limit]
    total = max(
        int(local.get("estimatedTotalHits", 0) or 0),
        int(web.get("estimatedTotalHits", 0) or 0),
        len(ranked),
    )
    return {"estimatedTotalHits": total, "hits": page}
