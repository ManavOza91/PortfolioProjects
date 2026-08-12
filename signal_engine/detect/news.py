"""Google News RSS, one query per watchlist company.

Public RSS feed only. We read headlines and links; we never fetch or scrape the
articles themselves, which keeps this inside "no scraping of sites that prohibit it".

Headlines are weak evidence — a company name in a headline is not a buying signal.
So a hit only becomes a signal if the headline also matches a taxonomy keyword, and
even then it carries a confidence below the auto-scoring bar. News reaches you as a
review item, which is the correct weight for a press mention.
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

from ..config import get_config
from ..ingest import normalise_name
from .base import Detection, DetectorResult, classify_by_keyword, fetch, parse_date


class NewsDetector:
    name = "news"

    def enabled(self) -> bool:
        return bool(
            get_config().detection.get("sources", {}).get("news", {}).get("enabled", True)
        )

    @property
    def _cfg(self) -> dict:
        return get_config().detection.get("sources", {}).get("news", {})

    def for_companies(self, names: list[str], since: dt.date) -> DetectorResult:
        result = DetectorResult()
        max_items = int(self._cfg.get("max_items_per_company", 10))
        base_conf = float(self._cfg.get("base_confidence", 0.55))
        lang = str(self._cfg.get("language", "en-US"))
        country = str(self._cfg.get("country", "US"))
        url = str(self._cfg.get("url", "https://news.google.com/rss/search"))

        for company in names:
            params = {
                "q": f'"{company}"',
                "hl": lang,
                "gl": country,
                "ceid": f"{country}:{lang.split('-')[0]}",
            }
            body, error = fetch(self.name, url, params=params, as_json=False)
            result.queries_made += 1
            if error:
                result.errors.append(error)
                continue
            if not body:
                continue

            for item in _items(body)[:max_items]:
                detection = self._to_detection(item, company, since, base_conf)
                if detection:
                    result.detections.append(detection)

        return result

    def discover(self, since: dt.date) -> DetectorResult:
        # News discovery would mean parsing a company name out of a headline, which
        # is guesswork without the parser. Deliberately not implemented: it would
        # manufacture companies from prose.
        return DetectorResult()

    def _to_detection(
        self, item: dict, company: str, since: dt.date, base_conf: float
    ) -> Detection | None:
        title = (item.get("title") or "").strip()
        if not title:
            return None

        published = parse_date(item.get("pubDate"))
        if published < since:
            return None

        # The company must actually be named in the headline. Google's RSS is
        # generous about relevance and will return loosely related stories.
        if normalise_name(company) not in normalise_name(title):
            return None

        type_key, keyword = classify_by_keyword(title)
        if not type_key:
            # A mention with no taxonomy keyword in it is not a signal. Dropping it
            # is the whole point — it is what stops a news feed becoming noise.
            return None

        return Detection(
            type_key=type_key,
            company_name=company,
            title=title,
            detected_date=published,
            url=(item.get("link") or "").strip() or None,
            source_name=f"Google News ({item.get('source') or 'press'})",
            external_id=(item.get("guid") or item.get("link") or "").strip() or None,
            raw={"headline": title, "matched_keyword": keyword,
                 "published": item.get("pubDate"), "outlet": item.get("source")},
            source_confidence=base_conf,
        )


def _items(xml_text: str) -> list[dict]:
    """Parse an RSS 2.0 body. Malformed feeds yield nothing rather than raising."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    out: list[dict] = []
    for node in root.iterfind(".//item"):
        entry: dict[str, str] = {}
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag in {"title", "link", "pubDate", "guid", "source", "description"}:
                entry[tag] = (child.text or "").strip()
        if entry:
            out.append(entry)
    return out
