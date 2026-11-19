"""Scrape Google's public GitHub repository listing into SQLite (no GitHub API)."""

from __future__ import annotations

import re
import random
import time
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


ORG = "google"
BASE_URL = f"https://github.com/orgs/{ORG}/repositories"
DB_FILE = "repositories.db"
PAGE_SIZE = 30
MIN_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 3.0
MAX_RETRIES = 6


def parse_count(text: str) -> Optional[int]:
    """Convert GitHub counts such as 1,234 or 2.1k to an integer."""
    text = text.strip().lower().replace(",", "")
    match = re.fullmatch(r"([\d.]+)\s*([km]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return int(number * multiplier)


def _repository_card(link: Tag) -> Tag:
    """Find the smallest likely listing container holding language and stars."""
    node = link
    for _ in range(7):
        if not isinstance(node, Tag) or node.parent is None:
            break
        node = node.parent
        if node.name in {"li", "article"}:
            return node
    return link.parent if isinstance(link.parent, Tag) else link


def parse_listing(html: str) -> list[tuple[str, Optional[str], Optional[int]]]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[tuple[str, Optional[str], Optional[int]]] = []
    seen: set[str] = set()

    # Repository links on this page have exactly /google/<repository> as path.
    for link in soup.select(f'a[href^="/{ORG}/"]'):
        href = link.get("href", "").split("?", 1)[0].rstrip("/")
        parts = href.strip("/").split("/")
        if len(parts) != 2 or parts[0].lower() != ORG:
            continue
        name = parts[1]
        if name.lower() in {"repositories", "projects", "packages", "people", "sponsors"}:
            continue
        if name in seen:
            continue

        card = _repository_card(link)
        language_tag = card.select_one('[itemprop="programmingLanguage"]')
        language = language_tag.get_text(" ", strip=True) if language_tag else None

        stars = None
        for star_link in card.select(f'a[href="/{ORG}/{name}/stargazers"]'):
            candidate = star_link.get("aria-label") or star_link.get("title") or star_link.get_text(" ", strip=True)
            stars = parse_count(candidate)
            if stars is not None:
                break
        # In some GitHub layouts the star count is in an abbreviated title on a
        # button/link with a slightly different href.
        if stars is None:
            for star_link in card.select(f'a[href^="/{ORG}/{name}/stargazers"]'):
                candidate = star_link.get("aria-label") or star_link.get("title") or star_link.get_text(" ", strip=True)
                stars = parse_count(candidate)
                if stars is not None:
                    break

        records.append((name, language or None, stars))
        seen.add(name)
    return records


def fetch_page(session: requests.Session, page: int) -> str:
    for attempt in range(MAX_RETRIES + 1):
        time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

        response = session.get(
            BASE_URL,
            params={"type": "all", "page": page, "per_page": PAGE_SIZE},
            timeout=30,
        )

        if response.status_code == 429:
            if attempt == MAX_RETRIES:
                response.raise_for_status()

            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after) if retry_after else 0
            except ValueError:
                wait_seconds = 0

            if wait_seconds <= 0:
                wait_seconds = min(60 * (2 ** attempt), 300)

            wait_seconds += random.uniform(0, 2)
            print(f"429制限: {wait_seconds:.1f}秒待って再試行します", flush=True)
            time.sleep(wait_seconds)
            continue

        response.raise_for_status()
        return response.text

    raise RuntimeError("ページ取得の再試行上限に達しました")


def main() -> None:
    import sqlite3

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; GoogleRepositoryClassExercise/1.0)",
        "Accept": "text/html",
    })

    records: list[tuple[str, Optional[str], Optional[int]]] = []
    page = 1
    while True:
        if page > 1:
            time.sleep(1)
        print(f"一覧ページを取得中: {page}", flush=True)
        html = fetch_page(session, page)
        page_records = parse_listing(html)
        if not page_records:
            break
        records.extend(page_records)
        page += 1

    # Keep one row per repository and refresh values when rerun.
    records = list({record[0]: record for record in records}.values())
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS repositories (
            name TEXT PRIMARY KEY,
            language TEXT,
            stars INTEGER
        )
    """)
    print(f"{len(records)}件を保存中...", flush=True)
    cursor.executemany("""
        INSERT OR REPLACE INTO repositories (name, language, stars)
        VALUES (?, ?, ?)
    """, records)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scrape_progress (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            next_page INTEGER NOT NULL,
            is_done INTEGER NOT NULL DEFAULT 0
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO scrape_progress (id, next_page, is_done) VALUES (1, 1, 0)")
    conn.commit()
    cursor.execute("SELECT next_page, is_done FROM scrape_progress WHERE id = 1")
    page, is_done = cursor.fetchone()
    if is_done:
        print("取得済みです。最初から取得する場合は repositories.db を削除してください。", flush=True)

    while not is_done:
        print(f"一覧ページを取得中: {page}", flush=True)
        html = fetch_page(session, page)
        page_records = parse_listing(html)
        if not page_records:
            cursor.execute("UPDATE scrape_progress SET is_done = 1 WHERE id = 1")
            conn.commit()
            break
        cursor.executemany("""
            INSERT OR REPLACE INTO repositories (name, language, stars)
            VALUES (?, ?, ?)
        """, page_records)
        cursor.execute("UPDATE scrape_progress SET next_page = ? WHERE id = 1", (page + 1,))
        conn.commit()
        print(f"{len(page_records)}件を保存しました（ページ {page}）", flush=True)
        page += 1

    # 課題要件: SELECT 文で保存データを表示
    cursor.execute("SELECT name, language, stars FROM repositories ORDER BY name")
    for row in cursor.fetchall():
        print(row)
    conn.close()


if __name__ == "__main__":
    main()