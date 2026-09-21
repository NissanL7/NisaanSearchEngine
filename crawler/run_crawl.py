"""Run a controlled crawl and persist pages to PostgreSQL."""

from __future__ import annotations

import argparse

from crawler import crawl
from storage import PageStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run NisaanSearchEngine crawler")
    parser.add_argument("seeds", nargs="+", help="Starting URLs")
    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--max-depth", type=int, default=1)
    args = parser.parse_args()

    pages = crawl(args.seeds, max_pages=args.max_pages, max_depth=args.max_depth)
    saved = PageStore().save_pages(pages)
    print(f"Crawled {len(pages)} pages; persisted {saved} pages.")


if __name__ == "__main__":
    main()
