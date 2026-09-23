"""Search NisaanSearchEngine's own index with free-first ranking."""
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


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u0900-\u097F]+", text.lower(), flags=re.UNICODE)


def _meili_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if MEILI_KEY:
        headers["Authorization"] = f"Bearer {MEILI_KEY}"
    with httpx.Client(timeout=20) as client:
        r = client.post(
            f"{MEILI_URL}/indexes/{INDEX_NAME}/search",
            headers=headers,
            json={"q": query, "limit": limit, "offset": offset},
        )
        r.raise_for_status()
        return r.json()


def _postgres_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    """Search PostgreSQL using the GIN full-text index, with an ILIKE fallback.

    The full-text expression matches the expression used by the Supabase
    migration: title + description + content, using the English dictionary.
    """
    fields = "id,url,title,description,content,domain,language,status_code,crawled_at,word_count"

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select {fields},
                       ts_rank_cd(
                           to_tsvector(
                               'english',
                               coalesce(title, '') || ' ' ||
                               coalesce(description, '') || ' ' ||
                               coalesce(content, '')
                           ),
                           websearch_to_tsquery('english', %(query)s)
                       ) as text_rank
                from pages
                where to_tsvector(
                    'english',
                    coalesce(title, '') || ' ' ||
                    coalesce(description, '') || ' ' ||
                    coalesce(content, '')
                ) @@ websearch_to_tsquery('english', %(query)s)
                order by text_rank desc, crawled_at desc nulls last
                limit %(candidate_limit)s offset %(offset)s
                """,
                {
                    "query": query,
                    "candidate_limit": min(max(limit + offset, 30), 100),
                    "offset": offset,
                },
            )
            columns = [d.name for d in cur.description]
            hits = [dict(zip(columns, row)) for row in cur.fetchall()]

            cur.execute(
                """
                select count(*)
                from pages
                where to_tsvector(
                    'english',
                    coalesce(title, '') || ' ' ||
                    coalesce(description, '') || ' ' ||
                    coalesce(content, '')
                ) @@ websearch_to_tsquery('english', %(query)s)
                """,
                {"query": query},
            )
            total = int(cur.fetchone()[0] or 0)

            if hits:
                return {"estimatedTotalHits": total, "hits": hits}

            tokens = _tokens(query)
            terms = list(dict.fromkeys([query, *tokens]))[:12]
            clauses, params = [], {}
            for i, term in enumerate(terms):
                key = f"p{i}"
                params[key] = f"%{term}%"
                clauses.append(
                    f"(title ilike %({key})s or description ilike %({key})s "
                    f"or content ilike %({key})s or domain ilike %({key})s)"
                )
            where = " or ".join(clauses) or "false"
            cur.execute(f"select count(*) from pages where {where}", params)
            fallback_total = int(cur.fetchone()[0] or 0)
            cur.execute(
                f"""
                select {fields}
                from pages
                where {where}
                order by crawled_at desc nulls last
                limit %(limit)s offset %(offset)s
                """,
                {**params, "limit": limit, "offset": offset},
            )
            columns = [d.name for d in cur.description]
            fallback_hits = [dict(zip(columns, row)) for row in cur.fetchall()]

    return {"estimatedTotalHits": fallback_total, "hits": fallback_hits}


def _supabase_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase REST configuration is missing")
    terms = list(dict.fromkeys([query, *_tokens(query)]))[:12]
    fields = "id,url,title,description,content,domain,language,status_code,crawled_at,word_count"
    collected = []
    seen = set()
    with httpx.Client(timeout=20) as client:
        for term in terms:
            safe = re.sub(r"[*,%()]", " ", term).strip()
            if not safe:
                continue
            params = {
                "select": fields,
                "or": f"(title.ilike.*{safe}*,description.ilike.*{safe}*,content.ilike.*{safe}*,domain.ilike.*{safe}*)",
                "limit": str(min(max(limit + offset, 20), 100)),
                "order": "crawled_at.desc",
            }
            r = client.get(
                f"{SUPABASE_URL}/rest/v1/pages",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                params=params,
            )
            r.raise_for_status()
            for hit in r.json():
                url = str(hit.get("url") or "")
                if url and url not in seen:
                    seen.add(url)
                    collected.append(hit)
    return {"estimatedTotalHits": len(collected), "hits": collected[offset : offset + limit]}


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
    with httpx.Client(timeout=20) as client:
        r = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={
                "q": query,
                "count": min(max(limit, 1), 20),
                "offset": max(offset, 0),
                "safesearch": "moderate",
            },
        )
        r.raise_for_status()
        data = r.json()
    web = data.get("web", {})
    hits = []
    for i, item in enumerate(web.get("results", [])):
        url = item.get("url") or ""
        hits.append(
            {
                "id": f"web-{offset+i}-{abs(hash(url))}",
                "url": url,
                "title": item.get("title") or url,
                "description": item.get("description") or "",
                "content": item.get("description") or "",
                "domain": item.get("profile", {}).get("long_name") or "",
                "source": "web-provider",
            }
        )
    return {"estimatedTotalHits": int(web.get("totalEstimatedMatches") or len(hits)), "hits": hits}


def _score_hit(hit: dict[str, Any], query: str) -> float:
    q = query.lower().strip()
    q_tokens = list(dict.fromkeys(_tokens(q)))
    title = str(hit.get("title") or "").lower()
    description = str(hit.get("description") or "").lower()
    content = str(hit.get("content") or "").lower()
    domain = str(hit.get("domain") or "").lower()
    haystack = f"{title} {description} {content}"
    score = 0.0
    if q in title:
        score += 60
    if q in domain:
        score += 35
    if q in description:
        score += 20
    if q in content:
        score += 8
    title_tokens = _tokens(title)
    domain_tokens = _tokens(domain)
    description_tokens = _tokens(description)
    content_tokens = _tokens(content)
    for token in q_tokens:
        if token in title_tokens:
            score += 28
        if token in domain_tokens:
            score += 14
        if token in description_tokens:
            score += 8
        if token in content_tokens:
            score += min(12, 2 + content.count(token) * 1.5)
    if q_tokens:
        score += 25 * sum(token in haystack for token in q_tokens) / len(q_tokens)
    if hit.get("source", "local") == "local":
        score += 6
    if title and q:
        ratio = SequenceMatcher(None, q, title).ratio()
        if ratio >= 0.55:
            score += ratio * 8
    score += float(hit.get("text_rank") or 0) * 20
    return score


def _rank_hits(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    ranked = [(hit, _score_hit(hit, query)) for hit in hits]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [dict(hit, relevance=round(score, 3)) for hit, score in ranked]


def _merge_hits(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged = []
    seen = set()
    for hit in [*primary, *secondary]:
        url = str(hit.get("url") or "").rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(hit)
        if len(merged) >= limit:
            break
    return merged


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {"estimatedTotalHits": 0, "hits": []}

    # Always query the local index AND the live web provider. Previously the
    # web provider was skipped whenever the local index already had enough
    # results, which made the engine appear to search only its own crawl data.
    candidate_limit = min(max(limit + offset, 30), 100)
    local = _local_search(query, candidate_limit, 0)
    local_hits = local.get("hits", [])
    for hit in local_hits:
        hit.setdefault("source", "local")

    web = {"estimatedTotalHits": 0, "hits": []}
    if BRAVE_API_KEY:
        try:
            web = _brave_search(query, candidate_limit, 0)
        except Exception:
            # Local search must remain available if the live provider is down.
            pass

    merged = _merge_hits(local_hits, web.get("hits", []), candidate_limit)
    ranked = _rank_hits(merged, query)
    page = ranked[offset : offset + limit]
    total = max(
        int(local.get("estimatedTotalHits", 0) or 0),
        int(web.get("estimatedTotalHits", 0) or 0),
        len(ranked),
    )
    return {"estimatedTotalHits": total, "hits": page}
