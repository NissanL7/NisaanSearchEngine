"""Controlled free-first crawler for NisaanSearchEngine.

The crawler respects robots.txt, discovers sitemaps and same-domain links,
and gradually expands the frontier to newly discovered domains. Limits keep
free infrastructure safe while allowing the index to grow beyond the initial
seed sites.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

DATABASE_URL = os.getenv("DATABASE_URL", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "NisaanSearchEngineBot/0.2 (+https://github.com/NissanL7/NisaanSearchEngine)",
)
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
    return f"{p.scheme.lower()}://{p.hostname.lower()}{p.path or '/'}" + (f"?{p.query}" if p.query else "")


def allowed_by_robots(url: str, cache: dict[str, RobotFileParser]) -> bool:
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


def _supabase_headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase REST configuration is missing")
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}


def _get_seeds() -> list[str]:
    with httpx.Client(timeout=20) as client:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/crawl_seeds",
            headers=_supabase_headers(),
            params={"select": "url", "enabled": "eq.true", "order": "id"},
        )
        response.raise_for_status()
        rows = response.json()
    return [row["url"] for row in rows if row.get("url")]


def _save_seed(url: str) -> None:
    """Persist a newly discovered domain as a future crawl starting point."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        with httpx.Client(timeout=15) as client:
            response = client.post(
                f"{SUPABASE_URL}/rest/v1/crawl_seeds",
                headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "url"},
                json=[{"url": url, "enabled": True, "max_depth": 2}],
            )
            response.raise_for_status()
    except httpx.HTTPError:
        # Discovery should never make an otherwise successful crawl fail.
        return


def _save_page(page: dict) -> None:
    if SUPABASE_URL and SUPABASE_KEY:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f"{SUPABASE_URL}/rest/v1/pages",
                headers={**_supabase_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": "url"},
                json=[page],
            )
            response.raise_for_status()
        return

    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """insert into pages
                    (url,canonical_url,domain,title,description,content,status_code,content_hash,word_count,crawled_at,updated_at)
                    values (%(url)s,%(url)s,%(domain)s,%(title)s,%(description)s,%(content)s,%(status)s,%(hash)s,%(words)s,now(),now())
                    on conflict (url) do update set title=excluded.title,description=excluded.description,
                    content=excluded.content,status_code=excluded.status_code,content_hash=excluded.content_hash,
                    word_count=excluded.word_count,crawled_at=now(),updated_at=now()""",
                    page,
                )
            conn.commit()
        return
    raise RuntimeError("No database or Supabase REST configuration is available")


def _discover_sitemap_urls(seed: str, client: httpx.Client, limit: int = 40) -> list[str]:
    p = urlparse(seed)
    origin = f"{p.scheme}://{p.netloc}"
    candidates = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
    urls: list[str] = []
    for sitemap in candidates:
        try:
            response = client.get(sitemap, headers={"Accept": "application/xml,text/xml"})
            if not response.is_success:
                continue
            text = response.text[:2_000_000]
            for match in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, flags=re.I | re.S):
                candidate = normalize(match)
                if candidate and urlparse(candidate).netloc == p.netloc:
                    urls.append(candidate)
                    if len(urls) >= limit:
                        return list(dict.fromkeys(urls))[:limit]
        except httpx.HTTPError:
            continue
    return list(dict.fromkeys(urls))[:limit]


def _extract_page(response: httpx.Response, url: str) -> tuple[dict, BeautifulSoup] | None:
    if "text/html" not in response.headers.get("content-type", "").lower():
        return None
    soup = BeautifulSoup(response.content[:MAX_BYTES], "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = meta.get("content", "").strip() if meta else ""
    content = re.sub(r"\s+", " ", " ".join(soup.stripped_strings)).strip()
    digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    page = {
        "url": url,
        "canonical_url": url,
        "domain": urlparse(url).netloc,
        "title": title[:1000],
        "description": description[:3000],
        "content": content[:1000000],
        "status_code": response.status_code,
        "content_hash": digest,
        "word_count": len(content.split()),
    }
    return page, soup


def crawl_seeds(max_pages: int = 10, max_depth: int = 2) -> int:
    seeds = [u for u in (normalize(s) for s in _get_seeds()) if u]
    if not seeds:
        return 0

    queue: deque[tuple[str, int]] = deque((u, 0) for u in seeds)
    seen = set(seeds)
    robots: dict[str, RobotFileParser] = {}
    saved = 0
    new_domains: set[str] = set()
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    with httpx.Client(headers=headers, follow_redirects=True, timeout=12) as client:
        for seed in seeds:
            for sitemap_url in _discover_sitemap_urls(seed, client, limit=20):
                if sitemap_url not in seen:
                    seen.add(sitemap_url)
                    queue.append((sitemap_url, 1))

        while queue and saved < max_pages:
            url, depth = queue.popleft()
            if not allowed_by_robots(url, robots):
                continue
            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue
            extracted = _extract_page(response, url)
            if not extracted:
                continue
            page, soup = extracted
            _save_page(page)
            saved += 1

            if depth < max_depth and response.is_success:
                current_domain = urlparse(url).netloc
                for a in soup.find_all("a", href=True):
                    link = normalize(a.get("href", ""), url)
                    if not link:
                        continue
                    link_domain = urlparse(link).netloc
                    if link_domain == current_domain:
                        if link not in seen:
                            seen.add(link)
                            queue.append((link, depth + 1))
                    elif link_domain and len(new_domains) < MAX_NEW_DOMAINS_PER_RUN:
                        # Grow the independent web frontier gradually. We store
                        # only the domain root, then future runs crawl it under
                        # the same robots/depth controls.
                        domain_root = f"{urlparse(link).scheme}://{link_domain}/"
                        if link_domain not in new_domains:
                            new_domains.add(link_domain)
                            _save_seed(domain_root)
            time.sleep(DELAY)

    return saved
