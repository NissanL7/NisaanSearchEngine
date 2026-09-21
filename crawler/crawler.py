"""NisaanSearchEngine V1 crawler.

Controlled crawler for public HTML pages. It respects robots.txt, limits
requests per host, avoids duplicate URLs, and stays within configured domains.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

USER_AGENT = os.getenv(
    "CRAWLER_USER_AGENT",
    "NisaanSearchEngineBot/0.1 (+https://github.com/NissanL7/NisaanSearchEngine)",
)
REQUEST_DELAY = float(os.getenv("CRAWLER_REQUEST_DELAY_SECONDS", "1"))
MAX_BYTES = int(os.getenv("CRAWLER_MAX_BYTES", str(5 * 1024 * 1024)))


@dataclass
class Page:
    url: str
    title: str
    description: str
    content: str
    domain: str
    status_code: int
    content_hash: str
    word_count: int


def normalize_url(url: str, base: str | None = None) -> str | None:
    if base:
        url = urljoin(base, url)
    url, _fragment = urldefrag(url.strip())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    host = parsed.hostname.lower() if parsed.hostname else ""
    if not host:
        return None

    # Remove default ports and normalize the path.
    port = parsed.port
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


class RobotsCache:
    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._cache:
            rp = RobotFileParser()
            rp.set_url(f"{origin}/robots.txt")
            try:
                rp.read()
            except Exception:
                # If robots.txt cannot be fetched, fail closed for this V1 crawler.
                self._cache[origin] = RobotFileParser()
                self._cache[origin].parse(["User-agent: *", "Disallow: /"])
                return False
            self._cache[origin] = rp
        return self._cache[origin].can_fetch(USER_AGENT, url)


def extract_page(url: str, response: httpx.Response) -> Page | None:
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        return None

    body = response.content[:MAX_BYTES]
    soup = BeautifulSoup(body, "lxml")

    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description = description_tag.get("content", "").strip() if description_tag else ""
    content = " ".join(soup.stripped_strings)
    content = re.sub(r"\s+", " ", content).strip()

    digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
    return Page(
        url=url,
        title=title[:1000],
        description=description[:3000],
        content=content[:1_000_000],
        domain=urlparse(url).netloc,
        status_code=response.status_code,
        content_hash=digest,
        word_count=len(content.split()),
    )


def discover_links(url: str, response: httpx.Response, allowed_domains: set[str]) -> list[str]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        return []
    soup = BeautifulSoup(response.content[:MAX_BYTES], "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        candidate = normalize_url(anchor["href"], url)
        if not candidate:
            continue
        if urlparse(candidate).netloc in allowed_domains:
            links.append(candidate)
    return links


def crawl(seeds: list[str], max_pages: int = 25, max_depth: int = 1) -> list[Page]:
    normalized_seeds = [normalize_url(url) for url in seeds]
    normalized_seeds = [url for url in normalized_seeds if url]
    allowed_domains = {urlparse(url).netloc for url in normalized_seeds}

    queue: deque[tuple[str, int]] = deque((url, 0) for url in normalized_seeds)
    seen: set[str] = set(normalized_seeds)
    robots = RobotsCache()
    pages: list[Page] = []

    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
        while queue and len(pages) < max_pages:
            url, depth = queue.popleft()
            if not robots.allowed(url):
                continue

            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue

            page = extract_page(url, response)
            if page:
                pages.append(page)
                if depth < max_depth and response.is_success:
                    for link in discover_links(url, response, allowed_domains):
                        if link not in seen:
                            seen.add(link)
                            queue.append((link, depth + 1))

            time.sleep(REQUEST_DELAY)

    return pages


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crawl permitted public web pages")
    parser.add_argument("seeds", nargs="+", help="Starting URLs")
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--max-depth", type=int, default=1)
    args = parser.parse_args()

    for page in crawl(args.seeds, args.max_pages, args.max_depth):
        print(f"{page.status_code} {page.url} — {page.title} ({page.word_count} words)")
