"""Search NisaanSearchEngine's own index plus broad live-web providers."""
from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
import psycopg
from bs4 import BeautifulSoup

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
    if not MEILI_URL:
        raise RuntimeError("Meilisearch is not configured")
    headers = {"Content-Type": "application/json"}
    if MEILI_KEY:
        headers["Authorization"] = f"Bearer {MEILI_KEY}"
    with httpx.Client(timeout=8) as client:
        r = client.post(f"{MEILI_URL}/indexes/{INDEX_NAME}/search", headers=headers, json={"q": query, "limit": limit, "offset": offset})
        r.raise_for_status()
        return r.json()


def _postgres_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    fields = "id,url,title,description,content,domain,language,status_code,crawled_at,word_count"
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                select {fields}, ts_rank_cd(
                    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(content,'')),
                    websearch_to_tsquery('english', %(query)s)
                ) as text_rank
                from pages
                where to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(content,''))
                    @@ websearch_to_tsquery('english', %(query)s)
                order by text_rank desc, crawled_at desc nulls last
                limit %(candidate_limit)s offset %(offset)s
                """, {"query": query, "candidate_limit": min(max(limit + offset, 30), 100), "offset": offset})
            columns = [d.name for d in cur.description]
            hits = [dict(zip(columns, row)) for row in cur.fetchall()]
            cur.execute("""
                select count(*) from pages
                where to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'') || ' ' || coalesce(content,''))
                    @@ websearch_to_tsquery('english', %(query)s)
                """, {"query": query})
            total = int(cur.fetchone()[0] or 0)
            if hits:
                return {"estimatedTotalHits": total, "hits": hits}

            terms = list(dict.fromkeys([query, *_tokens(query)]))[:12]
            clauses, params = [], {}
            for i, term in enumerate(terms):
                key = f"p{i}"
                params[key] = f"%{term}%"
                clauses.append(f"(title ilike %({key})s or description ilike %({key})s or content ilike %({key})s or domain ilike %({key})s)")
            where = " or ".join(clauses) or "false"
            cur.execute(f"select count(*) from pages where {where}", params)
            fallback_total = int(cur.fetchone()[0] or 0)
            cur.execute(f"select {fields} from pages where {where} order by crawled_at desc nulls last limit %(limit)s offset %(offset)s", {**params, "limit": limit, "offset": offset})
            columns = [d.name for d in cur.description]
            fallback_hits = [dict(zip(columns, row)) for row in cur.fetchall()]
    return {"estimatedTotalHits": fallback_total, "hits": fallback_hits}


def _supabase_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase REST configuration is missing")
    terms = list(dict.fromkeys([query, *_tokens(query)]))[:12]
    fields = "id,url,title,description,content,domain,language,status_code,crawled_at,word_count"
    collected, seen = [], set()
    with httpx.Client(timeout=8) as client:
        for term in terms:
            safe = re.sub(r"[*,%()]", " ", term).strip()
            if not safe:
                continue
            params = {"select": fields, "or": f"(title.ilike.*{safe}*,description.ilike.*{safe}*,content.ilike.*{safe}*,domain.ilike.*{safe}*)", "limit": str(min(max(limit + offset, 20), 100)), "order": "crawled_at.desc"}
            r = client.get(f"{SUPABASE_URL}/rest/v1/pages", headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}, params=params)
            r.raise_for_status()
            for hit in r.json():
                url = str(hit.get("url") or "")
                if url and url not in seen:
                    seen.add(url)
                    collected.append(hit)
    return {"estimatedTotalHits": len(collected), "hits": collected[offset : offset + limit]}


def _local_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    providers = []
    if MEILI_URL:
        providers.append(_meili_search)
    if DATABASE_URL:
        providers.append(_postgres_search)
    if SUPABASE_URL and SUPABASE_KEY:
        providers.append(_supabase_search)
    for provider in providers:
        try:
            result = provider(query, limit, offset)
            if result.get("hits"):
                return result
        except Exception:
            continue
    return {"estimatedTotalHits": 0, "hits": []}


def _brave_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    if not BRAVE_API_KEY:
        return {"estimatedTotalHits": 0, "hits": []}
    with httpx.Client(timeout=8) as client:
        r = client.get("https://api.search.brave.com/res/v1/web/search", headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}, params={"q": query, "count": min(max(limit, 1), 20), "offset": max(offset, 0), "safesearch": "moderate"})
        r.raise_for_status()
        data = r.json()
    web = data.get("web", {})
    hits = []
    for i, item in enumerate(web.get("results", [])):
        url = item.get("url") or ""
        hits.append({"id": f"brave-{offset+i}-{abs(hash(url))}", "url": url, "title": item.get("title") or url, "description": item.get("description") or "", "content": item.get("description") or "", "domain": item.get("profile", {}).get("long_name") or urlparse(url).netloc, "source": "brave"})
    return {"estimatedTotalHits": int(web.get("totalEstimatedMatches") or len(hits)), "hits": hits}


def _unwrap_url(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return unquote(target) if target else href


def _ddg_search(query: str, limit: int, offset: int, lite: bool = False) -> dict[str, Any]:
    endpoint = "https://lite.duckduckgo.com/lite/" if lite else "https://html.duckduckgo.com/html/"
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; NisaanSearchEngine/1.0)", "Accept-Language": "en-US,en;q=0.9"}) as client:
        r = client.get(endpoint, params={"q": query})
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    selectors = ["a.result__a"] if not lite else ["a.result-link"]
    links = []
    for selector in selectors:
        links = soup.select(selector)
        if links:
            break
    hits = []
    for i, link in enumerate(links):
        url = _unwrap_url(link.get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        parent = link.parent
        block = link.find_parent(class_=re.compile(r"result", re.I)) or parent
        snippet_node = block.select_one(".result__snippet, .result-snippet") if block else None
        description = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        hits.append({"id": f"ddg-{offset+i}-{abs(hash(url))}", "url": url, "title": link.get_text(" ", strip=True) or url, "description": description, "content": description, "domain": urlparse(url).netloc, "source": "duckduckgo"})
        if len(hits) >= min(max(limit + offset, 10), 20):
            break
    return {"estimatedTotalHits": len(hits), "hits": hits}


def _bing_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    with httpx.Client(timeout=8, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (compatible; NisaanSearchEngine/1.0)", "Accept-Language": "en-US,en;q=0.9"}) as client:
        r = client.get("https://www.bing.com/search", params={"q": query, "count": min(max(limit + offset, 10), 20)})
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hits = []
    for i, item in enumerate(soup.select("li.b_algo")):
        link = item.select_one("h2 a")
        if not link:
            continue
        url = _unwrap_url(link.get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        p = item.select_one(".b_caption p, p")
        description = p.get_text(" ", strip=True) if p else ""
        hits.append({"id": f"bing-{offset+i}-{abs(hash(url))}", "url": url, "title": link.get_text(" ", strip=True) or url, "description": description, "content": description, "domain": urlparse(url).netloc, "source": "bing"})
        if len(hits) >= min(max(limit + offset, 10), 20):
            break
    return {"estimatedTotalHits": len(hits), "hits": hits}


def _web_search(query: str, limit: int, offset: int) -> dict[str, Any]:
    providers = []
    if BRAVE_API_KEY:
        providers.append(lambda: _brave_search(query, limit, offset))
    providers.extend([lambda: _ddg_search(query, limit, offset, lite=False), lambda: _bing_search(query, limit, offset), lambda: _ddg_search(query, limit, offset, lite=True)])
    errors = []
    for provider in providers:
        try:
            result = provider()
            if result.get("hits"):
                result["provider_errors"] = errors
                return result
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {"estimatedTotalHits": 0, "hits": [], "provider_errors": errors}


def _score_hit(hit: dict[str, Any], query: str) -> float:
    q = query.lower().strip()
    q_tokens = list(dict.fromkeys(_tokens(q)))
    title = str(hit.get("title") or "").lower()
    description = str(hit.get("description") or "").lower()
    content = str(hit.get("content") or "").lower()
    domain = str(hit.get("domain") or "").lower()
    haystack = f"{title} {description} {content}"
    score = 0.0
    if q in title: score += 60
    if q in domain: score += 35
    if q in description: score += 20
    if q in content: score += 8
    title_tokens = _tokens(title)
    domain_tokens = _tokens(domain)
    description_tokens = _tokens(description)
    content_tokens = _tokens(content)
    for token in q_tokens:
        if token in title_tokens: score += 28
        if token in domain_tokens: score += 14
        if token in description_tokens: score += 8
        if token in content_tokens: score += min(12, 2 + content.count(token) * 1.5)
    if q_tokens:
        score += 25 * sum(token in haystack for token in q_tokens) / len(q_tokens)
    if hit.get("source") == "local": score += 6
    if title and q:
        ratio = SequenceMatcher(None, q, title).ratio()
        if ratio >= 0.55: score += ratio * 8
    score += float(hit.get("text_rank") or 0) * 20
    return score


def _rank_hits(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    ranked = [(hit, _score_hit(hit, query)) for hit in hits]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return [dict(hit, relevance=round(score, 3)) for hit, score in ranked]


def _filter_relevant_local(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Reject weak local matches so common words such as 'is' cannot pollute web results."""
    q_tokens = list(dict.fromkeys(_tokens(query)))
    if not q_tokens:
        return []
    required = 1 if len(q_tokens) == 1 else 2
    filtered = []
    for hit in hits:
        text = " ".join(str(hit.get(k) or "").lower() for k in ("title", "description", "content", "domain"))
        text_tokens = set(_tokens(text))
        if query.lower() in text or sum(token in text_tokens for token in q_tokens) >= required:
            filtered.append(hit)
    return filtered


def _merge_hits(primary: list[dict[str, Any]], secondary: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged, seen = [], set()
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
        return {"estimatedTotalHits": 0, "hits": [], "source": "none"}

    candidate_limit = min(max(limit + offset, 30), 100)

    # Live web is PRIMARY. The local crawl index is only a supplement.
    web = _web_search(query, candidate_limit, 0)
    web_hits = web.get("hits", [])

    local = _local_search(query, candidate_limit, 0)
    local_hits = _filter_relevant_local(local.get("hits", []), query)
    for hit in local_hits:
        hit.setdefault("source", "local")

    merged = _merge_hits(web_hits, local_hits, candidate_limit)
    ranked = _rank_hits(merged, query)
    page = ranked[offset : offset + limit]
    total = max(int(web.get("estimatedTotalHits", 0) or 0), int(local.get("estimatedTotalHits", 0) or 0), len(ranked))
    return {"estimatedTotalHits": total, "hits": page, "source": "web+local" if web_hits and local_hits else ("web" if web_hits else "local" if local_hits else "none"), "provider_errors": web.get("provider_errors", [])}
