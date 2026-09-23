"""Free-first persistent crawler for NisaanSearchEngine.

The crawler uses the Supabase crawl_queue as a durable frontier, respects
robots.txt, discovers sitemaps and links, deduplicates URLs, and gradually
expands to new domains. It is intentionally conservative for free hosting.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
USER_AGENT = os.getenv("CRAWLER_USER_AGENT", "NisaanSearchEngineBot/0.3 (+https://github.com/NissanL7/NisaanSearchEngine)")
DELAY = float(os.getenv("CRAWLER_REQUEST_DELAY_SECONDS", "1"))
MAX_BYTES = 3 * 1024 * 1024
MAX_NEW_DOMAINS_PER_RUN = int(os.getenv("CRAWLER_MAX_NEW_DOMAINS", "5"))


def normalize(url: str, base: str | None = None) -> str | None:
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        return None
    host = p.hostname.lower()
    port = f":{p.port}" if p.port and p.port not in {80, 443} else ""
    return f"{p.scheme.lower()}://{host}{port}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def _headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase REST configuration is missing")
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def _get_seeds() -> list[str]:
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{SUPABASE_URL}/rest/v1/crawl_seeds", headers=_headers(), params={"select": "url", "enabled": "eq.true", "order": "id"})
        r.raise_for_status()
        return [x["url"] for x in r.json() if x.get("url")]


def _queue_urls(urls: list[tuple[str, str | None, int]]) -> None:
    if not urls:
        return
    unique: dict[str, dict] = {}
    for url, source, depth in urls:
        n = normalize(url)
        if n:
            unique[n] = {"url": n, "source_url": source, "depth": depth, "status": "pending"}
    if not unique:
        return
    with httpx.Client(timeout=20) as client:
        r = client.post(f"{SUPABASE_URL}/rest/v1/crawl_queue", headers={**_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"}, params={"on_conflict": "url"}, json=list(unique.values()))
        r.raise_for_status()


def _get_frontier(limit: int) -> list[tuple[int, str, int]]:
    with httpx.Client(timeout=20) as client:
        r = client.get(
            f"{SUPABASE_URL}/rest/v1/crawl_queue",
            headers=_headers(),
            params={"select": "id,url,depth", "status": "eq.pending", "next_attempt_at": "lte.now()", "order": "id", "limit": str(limit)},
        )
        r.raise_for_status()
        return [(int(x["id"]), x["url"], int(x.get("depth") or 0)) for x in r.json()]


def _mark_queue(item_id: int, status: str, error: str | None = None) -> None:
    payload = {"status": status}
    if error:
        payload["last_error"] = error[:1000]
    with httpx.Client(timeout=15) as client:
        r = client.patch(f"{SUPABASE_URL}/rest/v1/crawl_queue", headers=_headers(), params={"id": f"eq.{item_id}"}, json=payload)
        r.raise_for_status()


def _save_seed(url: str) -> None:
    try:
        n = normalize(url)
        if not n:
            return
        with httpx.Client(timeout=15) as client:
            r = client.post(f"{SUPABASE_URL}/rest/v1/crawl_seeds", headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}, params={"on_conflict": "url"}, json=[{"url": n, "enabled": True, "max_depth": 2}])
            r.raise_for_status()
    except Exception:
        pass


def _save_page(page: dict) -> None:
    with httpx.Client(timeout=20) as client:
        r = client.post(f"{SUPABASE_URL}/rest/v1/pages", headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"}, params={"on_conflict": "url"}, json=[page])
        r.raise_for_status()


def _robots_allowed(url: str, cache: dict[str, RobotFileParser]) -> bool:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in cache:
        rp = RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            rp.read()
        except Exception:
            return False
        cache[origin] = rp
    return cache[origin].can_fetch(USER_AGENT, url)


def _sitemap_urls(seed: str, client: httpx.Client, limit: int = 20) -> list[str]:
    p = urlparse(seed)
    origin = f"{p.scheme}://{p.netloc}"
    found: list[str] = []
    for location in (f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"):
        try:
            r = client.get(location, headers={"Accept": "application/xml,text/xml"})
            if not r.is_success:
                continue
            for value in re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text[:2_000_000], flags=re.I | re.S):
                u = normalize(value)
                if u and urlparse(u).netloc == p.netloc:
                    found.append(u)
                    if len(found) >= limit:
                        return list(dict.fromkeys(found))[:limit]
        except httpx.HTTPError:
            continue
    return list(dict.fromkeys(found))[:limit]


def _extract(response: httpx.Response, url: str):
    if "text/html" not in response.headers.get("content-type", "").lower():
        return None
    soup = BeautifulSoup(response.content[:MAX_BYTES], "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = meta.get("content", "").strip() if meta else ""
    canonical_tag = soup.find("link", attrs={"rel": lambda v: v and "canonical" in v})
    canonical = normalize(canonical_tag.get("href", ""), url) if canonical_tag else url
    content = re.sub(r"\s+", " ", " ".join(soup.stripped_strings)).strip()
    return {"page": {"url": url, "canonical_url": canonical or url, "domain": urlparse(url).netloc, "title": title[:1000], "description": description[:3000], "content": content[:1000000], "status_code": response.status_code, "content_hash": hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest(), "word_count": len(content.split())}, "soup": soup}


def crawl_seeds(max_pages: int = 10, max_depth: int = 2) -> int:
    seeds = [normalize(x) for x in _get_seeds()]
    seeds = [x for x in seeds if x]
    if not seeds:
        return 0
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=12) as client:
        initial = [(s, None, 0) for s in seeds]
        for seed in seeds:
            initial.extend((u, seed, 1) for u in _sitemap_urls(seed, client, limit=20))
        _queue_urls(initial)

    frontier = _get_frontier(max_pages)
    robots: dict[str, RobotFileParser] = {}
    saved = 0
    new_domains: set[str] = set()

    with httpx.Client(headers=headers, follow_redirects=True, timeout=12) as client:
        for item_id, url, depth in frontier:
            _mark_queue(item_id, "processing")
            try:
                if not _robots_allowed(url, robots):
                    _mark_queue(item_id, "blocked")
                    continue
                response = client.get(url)
                extracted = _extract(response, url)
                if not extracted:
                    _mark_queue(item_id, "completed")
                    continue
                _save_page(extracted["page"])
                _mark_queue(item_id, "completed")
                saved += 1
                discovered: list[tuple[str, str | None, int]] = []
                if response.is_success and depth < max_depth:
                    current_domain = urlparse(url).netloc
                    for a in extracted["soup"].find_all("a", href=True):
                        link = normalize(a.get("href", ""), url)
                        if not link:
                            continue
                        domain = urlparse(link).netloc
                        if domain == current_domain:
                            discovered.append((link, url, depth + 1))
                        elif domain and domain not in new_domains and len(new_domains) < MAX_NEW_DOMAINS_PER_RUN:
                            new_domains.add(domain)
                            root = f"{urlparse(link).scheme}://{domain}/"
                            _save_seed(root)
                            discovered.append((root, url, 0))
                _queue_urls(discovered)
            except Exception as exc:
                _mark_queue(item_id, "failed", str(exc))
            time.sleep(DELAY)
    return saved
