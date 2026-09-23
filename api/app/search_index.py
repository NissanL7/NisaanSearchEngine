"""Search engine backend: strict, query-aware live web search with local-index fallback."""
from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None

MEILI_URL = os.getenv("MEILISEARCH_URL", "").rstrip("/")
MEILI_KEY = os.getenv("MEILISEARCH_MASTER_KEY", "")
INDEX_NAME = os.getenv("MEILISEARCH_INDEX", "pages")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
BRAVE_API_KEY = os.getenv("BRAVE_SEARCH_API_KEY", "")

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
    "how", "i", "in", "is", "it", "me", "of", "on", "or", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "with", "you", "your", "can", "could",
    "would", "should", "about", "into", "than", "that", "these", "those", "tell", "give",
    "please", "explain", "show", "find", "search", "tell", "know", "doesn", "its",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u0900-\u097F]+", text.lower(), flags=re.UNICODE)


def _topics(query: str) -> list[str]:
    return list(dict.fromkeys(t for t in _tokens(query) if t not in _STOPWORDS and len(t) > 1))


def _clean_url(url: str) -> str:
    return _unwrap_url(url).strip()


def _unwrap_url(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return unquote(target) if target else href


def _hit(url: str, title: str, description: str, source: str) -> dict[str, Any]:
    return {
        "id": f"{source}-{abs(hash(url))}",
        "url": url,
        "title": title.strip() or url,
        "description": description.strip(),
        "content": description.strip(),
        "domain": urlparse(url).netloc,
        "source": source,
    }


def _bing_search(query: str, count: int = 20) -> list[dict[str, Any]]:
    with httpx.Client(
        timeout=10,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; NisaanSearchEngine/1.1)", "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        r = client.get("https://www.bing.com/search", params={"q": query, "count": min(max(count, 10), 30), "setlang": "en-US"})
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hits: list[dict[str, Any]] = []
    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a")
        if not link:
            continue
        url = _clean_url(link.get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        p = item.select_one(".b_caption p, p")
        desc = p.get_text(" ", strip=True) if p else ""
        hits.append(_hit(url, link.get_text(" ", strip=True), desc, "bing"))
        if len(hits) >= count:
            break
    return hits


def _ddg_search(query: str, count: int = 20, lite: bool = False) -> list[dict[str, Any]]:
    endpoint = "https://lite.duckduckgo.com/lite/" if lite else "https://html.duckduckgo.com/html/"
    with httpx.Client(
        timeout=10,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; NisaanSearchEngine/1.1)", "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        r = client.get(endpoint, params={"q": query})
        r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    selectors = ["a.result__a"] if not lite else ["a.result-link"]
    links: list[Any] = []
    for selector in selectors:
        links = soup.select(selector)
        if links:
            break
    hits: list[dict[str, Any]] = []
    for link in links:
        url = _clean_url(link.get("href", ""))
        if not url.startswith(("http://", "https://")):
            continue
        block = link.find_parent(class_=re.compile(r"result", re.I)) or link.parent
        node = block.select_one(".result__snippet, .result-snippet") if block else None
        desc = node.get_text(" ", strip=True) if node else ""
        hits.append(_hit(url, link.get_text(" ", strip=True), desc, "duckduckgo"))
        if len(hits) >= count:
            break
    return hits


def _brave_search(query: str, count: int = 20) -> list[dict[str, Any]]:
    if not BRAVE_API_KEY:
        return []
    with httpx.Client(timeout=10) as client:
        r = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
            params={"q": query, "count": min(max(count, 1), 20), "safesearch": "moderate"},
        )
        r.raise_for_status()
        data = r.json()
    return [_hit(str(x.get("url") or ""), str(x.get("title") or ""), str(x.get("description") or ""), "brave") for x in data.get("web", {}).get("results", []) if x.get("url")]


def _topic_match(hit: dict[str, Any], query: str) -> tuple[int, int, bool]:
    """Return strong matches, any matches, and whether the full phrase occurs."""
    topics = _topics(query)
    title = str(hit.get("title") or "").lower()
    desc = str(hit.get("description") or "").lower()
    content = str(hit.get("content") or "").lower()
    domain = str(hit.get("domain") or "").lower()
    title_set, desc_set, content_set, domain_set = map(set, (_tokens(title), _tokens(desc), _tokens(content), _tokens(domain)))
    strong = 0
    any_match = 0
    for token in topics:
        in_title = token in title_set or token in title
        in_desc = token in desc_set or token in desc
        in_domain = token in domain_set or token in domain
        in_content = token in content_set or token in content
        if in_title or in_desc or in_domain:
            strong += 1
        if in_title or in_desc or in_domain or in_content:
            any_match += 1
    normalized_query = " ".join(_tokens(query))
    hay = f"{title} {desc} {content}"
    phrase = bool(normalized_query and normalized_query in hay)
    return strong, any_match, phrase


def _is_relevant(hit: dict[str, Any], query: str) -> bool:
    topics = _topics(query)
    if not topics:
        return False
    strong, any_match, phrase = _topic_match(hit, query)
    n = len(topics)
    if n == 1:
        return any_match >= 1
    if phrase:
        return True
    # Require the majority of meaningful terms. This prevents a result about
    # "Rust programming language" from passing a "solar system" query merely
    # because it contains the generic word "system".
    required = n if n <= 2 else max(2, (n + 1) // 2)
    return strong >= required and any_match >= required


def _score(hit: dict[str, Any], query: str) -> float:
    topics = _topics(query)
    title = str(hit.get("title") or "").lower()
    desc = str(hit.get("description") or "").lower()
    domain = str(hit.get("domain") or "").lower()
    q = query.lower().strip()
    score = 0.0
    if q and q in title:
        score += 100
    if q and q in desc:
        score += 25
    strong, any_match, phrase = _topic_match(hit, query)
    score += strong * 35 + any_match * 10
    if phrase:
        score += 60
    for token in topics:
        if token in title:
            score += 25
        if token in domain:
            score += 10
    if title and q:
        score += SequenceMatcher(None, q, title).ratio() * 5
    return score


def _dedupe(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hits:
        url = _clean_url(str(hit.get("url") or "")).rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        hit["url"] = url
        result.append(hit)
    return result


def _local_hits(query: str, limit: int) -> list[dict[str, Any]]:
    # Local data is supplementary only. It can never override live-web results.
    hits: list[dict[str, Any]] = []
    if MEILI_URL:
        try:
            headers = {"Content-Type": "application/json"}
            if MEILI_KEY:
                headers["Authorization"] = f"Bearer {MEILI_KEY}"
            with httpx.Client(timeout=8) as client:
                r = client.post(f"{MEILI_URL}/indexes/{INDEX_NAME}/search", headers=headers, json={"q": query, "limit": limit})
                r.raise_for_status()
                data = r.json()
            hits.extend(data.get("hits", []))
        except Exception:
            pass
    if not hits and DATABASE_URL and psycopg:
        try:
            fields = "id,url,title,description,content,domain,language,status_code,crawled_at,word_count"
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"select {fields} from pages where title ilike %(q)s or description ilike %(q)s or content ilike %(q)s or domain ilike %(q)s order by crawled_at desc nulls last limit %(limit)s",
                        {"q": f"%{query}%", "limit": limit},
                    )
                    columns = [d.name for d in cur.description]
                    hits.extend(dict(zip(columns, row)) for row in cur.fetchall())
        except Exception:
            pass
    return [h for h in hits if _is_relevant(h, query)]


def search(query: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    query = query.strip()
    if not query:
        return {"estimatedTotalHits": 0, "hits": [], "source": "none"}

    # Always search the live web. Do not let an indexed/local hit short-circuit it.
    all_hits: list[dict[str, Any]] = []
    errors: list[str] = []
    provider_hits: dict[str, int] = {}
    providers = [("bing", lambda: _bing_search(query, 20)), ("duckduckgo", lambda: _ddg_search(query, 20)), ("duckduckgo-lite", lambda: _ddg_search(query, 20, True))]
    if BRAVE_API_KEY:
        providers.insert(0, ("brave", lambda: _brave_search(query, 20)))

    for name, provider in providers:
        try:
            raw = provider()
            good = [h for h in raw if _is_relevant(h, query)]
            provider_hits[name] = len(good)
            all_hits.extend(good)
        except Exception as exc:
            provider_hits[name] = 0
            errors.append(f"{name}:{type(exc).__name__}")

    web_hits = _dedupe(all_hits)
    local_hits = _dedupe(_local_hits(query, 30))
    merged = _dedupe(web_hits + local_hits)
    merged.sort(key=lambda h: _score(h, query), reverse=True)
    for hit in merged:
        hit["relevance"] = round(_score(hit, query), 3)

    page = merged[offset : offset + limit]
    source = "web+local" if web_hits and local_hits else "web" if web_hits else "local" if local_hits else "none"
    return {
        "estimatedTotalHits": len(merged),
        "hits": page,
        "source": source,
        "provider_errors": errors,
        "provider_hits": provider_hits,
    }
