"""Small, controlled web crawler used by the API to grow the V1 index."""
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
    "NisaanSearchEngineBot/0.1 (+https://github.com/NissanL7/NisaanSearchEngine)",
)
DELAY = float(os.getenv("CRAWLER_REQUEST_DELAY_SECONDS", "1"))
MAX_BYTES = 3 * 1024 * 1024


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
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _get_seeds() -> list[str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase REST configuration is missing")
    with httpx.Client(timeout=20) as client:
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/crawl_seeds",
            headers=_supabase_headers(),
            params={"select": "url", "enabled": "eq.true", "order": "id"},
        )
        response.raise_for_status()
        rows = response.json()
    return [row["url"] for row in rows if row.get("url")]


def _save_page(page: dict) -> None:
    # Prefer Supabase REST for the free V1 deployment. PostgreSQL remains a
    # fallback for installations that already provide DATABASE_URL.
    if SUPABASE_URL and SUPABASE_KEY:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                f"{SUPABASE_URL}/rest/v1/pages",
                headers={
                    **_supabase_headers(),
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
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


def crawl_seeds(max_pages: int = 10, max_depth: int = 1) -> int:
    seeds = [u for u in (normalize(s) for s in _get_seeds()) if u]
    if not seeds:
        return 0

    queue: deque[tuple[str, int]] = deque((u, 0) for u in seeds)
    seen = set(seeds)
    robots: dict[str, RobotFileParser] = {}
    saved = 0
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

    with httpx.Client(headers=headers, follow_redirects=True, timeout=12) as client:
        while queue and saved < max_pages:
            url, depth = queue.popleft()
            if not allowed_by_robots(url, robots):
                continue
            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue
            if "text/html" not in response.headers.get("content-type", "").lower():
                continue

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
            _save_page(page)
            saved += 1

            if depth < max_depth and response.is_success:
                domain = urlparse(url).netloc
                for a in soup.find_all("a", href=True):
                    link = normalize(a.get("href", ""), url)
                    if link and urlparse(link).netloc == domain and link not in seen:
                        seen.add(link)
                        queue.append((link, depth + 1))
            time.sleep(DELAY)

    return saved
