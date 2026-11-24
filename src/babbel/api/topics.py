"""Topic fetcher for proactive conversations (spec FR-14, D13).

Reads config/topic_feeds.yaml (interest -> RSS feeds), and for every active person stores up to
three fresh items per matching interest as TopicItem rows. Babbel offers at most one per call.
Only the headline and a short summary are kept (D13: check each source's terms of use).

Run once an hour or once a day:  babbel-topics
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import yaml
from sqlmodel import Session, select

from babbel.api.db import Senior, TopicItem, get_engine
from babbel.config import get_settings

log = logging.getLogger("babbel.topics")
MAX_AGE = timedelta(days=3)


@dataclass(frozen=True)
class FeedItem:
    title: str
    summary: str
    url: str
    published_at: datetime | None


def _clean(value: str | None, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)  # RSS
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))  # Atom
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_feed(xml_text: str) -> list[FeedItem]:
    """Parse RSS 2.0 or Atom into items, newest first."""
    root = ET.fromstring(xml_text)
    atom = "{http://www.w3.org/2005/Atom}"
    items: list[FeedItem] = []
    for node in root.iter("item"):
        items.append(FeedItem(
            title=_clean(node.findtext("title"), 140),
            summary=_clean(node.findtext("description"), 240),
            url=(node.findtext("link") or "").strip(),
            published_at=_date(node.findtext("pubDate")),
        ))
    for node in root.iter(f"{atom}entry"):
        link = node.find(f"{atom}link")
        items.append(FeedItem(
            title=_clean(node.findtext(f"{atom}title"), 140),
            summary=_clean(node.findtext(f"{atom}summary") or node.findtext(f"{atom}content"), 240),
            url=(link.get("href", "") if link is not None else "").strip(),
            published_at=_date(node.findtext(f"{atom}updated") or node.findtext(f"{atom}published")),
        ))
    items = [i for i in items if i.title]
    items.sort(key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items


def load_feeds(path: str | Path) -> dict[str, list[str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k).lower(): list(v or []) for k, v in (data.get("interests") or {}).items()}


def matching_interests(person_interests: list[str], feeds: dict[str, list[str]]) -> list[str]:
    """An interest matches a feed key when either contains the other ("Club Brugge" ~ "voetbal"
    does not match; add aliases in the YAML instead)."""
    out = []
    for interest in person_interests:
        low = interest.lower().strip()
        for key in feeds:
            if low and (key in low or low in key) and key not in out:
                out.append(key)
    return out


def store_items(session: Session, senior_id: int, interest: str, items: list[FeedItem],
                now: datetime, per_interest: int = 3) -> int:
    added = 0
    for item in items:
        if added >= per_interest:
            break
        if item.published_at and now - item.published_at > MAX_AGE:
            continue
        if item.url and session.exec(
            select(TopicItem).where(TopicItem.senior_id == senior_id, TopicItem.url == item.url)
        ).first():
            continue
        session.add(TopicItem(senior_id=senior_id, interest=interest, title=item.title,
                              summary=item.summary, source="rss", url=item.url,
                              published_at=item.published_at))
        added += 1
    return added


def run(feeds_path: str | None = None, fetch=None) -> int:
    """Fetch feeds once for all active people. `fetch(url) -> str` can be injected for tests."""
    feeds = load_feeds(feeds_path or get_settings().topic_feeds_file)
    cache: dict[str, list[FeedItem]] = {}

    def get(url: str) -> list[FeedItem]:
        if url not in cache:
            try:
                body = fetch(url) if fetch else httpx.get(url, timeout=10, follow_redirects=True).text
                cache[url] = parse_feed(body)
            except Exception:
                log.exception("feed failed: %s", url)
                cache[url] = []
        return cache[url]

    now = datetime.now(timezone.utc)
    total = 0
    with Session(get_engine()) as session:
        for senior in session.exec(select(Senior).where(Senior.status == "active")).all():
            interests = [i for i in senior.interests.split(",") if i.strip()]
            for key in matching_interests(interests, feeds):
                items = [item for url in feeds[key] for item in get(url)]
                total += store_items(session, senior.id, key, items, now)
        session.commit()
    log.info("stored %d topic items", total)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run()


if __name__ == "__main__":
    main()
