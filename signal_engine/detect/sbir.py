"""SBIR / STTR award data — sbir.gov's free public API.

A grant is a QUALIFIER, not a trigger: money exists and a specific programme is
funded. It carries a low weight for exactly that reason and will not surface a
company on its own.

⚠️ This API is frequently unavailable at source — it returns
`{"Code":"TooManyRequestsError","Message":"The SBIR Public API is not available
at this time."}` even for a single request, independent of anything we do. That is
handled as a normal, reportable outcome: the run continues and the other sources
still produce results. This detector's request shape follows sbir.gov's documented
parameters but has NOT been verified against a live response.
"""

from __future__ import annotations

import datetime as dt

from ..config import get_config
from .base import Detection, DetectorResult, fetch, parse_date


class SBIRDetector:
    name = "sbir"

    def enabled(self) -> bool:
        return bool(
            get_config().detection.get("sources", {}).get("sbir", {}).get("enabled", True)
        )

    @property
    def _cfg(self) -> dict:
        return get_config().detection.get("sources", {}).get("sbir", {})

    def _url(self) -> str:
        return str(self._cfg.get("url", "https://api.www.sbir.gov/public/api/awards"))

    # -- watchlist ----------------------------------------------------------

    def for_companies(self, names: list[str], since: dt.date) -> DetectorResult:
        result = DetectorResult()
        rows = int(self._cfg.get("max_results_per_company", 20))
        type_key = str(self._cfg.get("type_key", "grant_award"))

        for company in names:
            payload, error = fetch(
                self.name, self._url(), params={"firm": company, "rows": rows}
            )
            result.queries_made += 1
            if error:
                result.errors.append(error)
                # The API is down for everyone, not just this company — stop early
                # rather than making 52 identical failing requests.
                break
            for row in _rows(payload):
                detection = self._to_detection(row, type_key)
                if detection and detection.detected_date >= since:
                    result.detections.append(detection)

        return result

    # -- discovery ----------------------------------------------------------

    def discover(self, since: dt.date) -> DetectorResult:
        result = DetectorResult()
        rows = int(self._cfg.get("max_results_per_company", 20))
        type_key = str(self._cfg.get("type_key", "grant_award"))

        for keyword in self._cfg.get("discovery_keywords", []) or []:
            payload, error = fetch(
                self.name, self._url(), params={"keyword": keyword, "rows": rows}
            )
            result.queries_made += 1
            if error:
                result.errors.append(error)
                break
            for row in _rows(payload):
                detection = self._to_detection(row, type_key)
                if detection and detection.detected_date >= since:
                    result.detections.append(detection)

        return result

    # -- shaping ------------------------------------------------------------

    def _to_detection(self, row: dict, type_key: str) -> Detection | None:
        firm = (row.get("firm") or "").strip()
        if not firm:
            return None

        title = (row.get("award_title") or "SBIR award").strip()
        agency = (row.get("agency") or "").strip()
        phase = (row.get("phase") or "").strip()
        amount = row.get("award_amount")

        bits = [b for b in (agency, phase) if b]
        headline = f"{title}"
        if bits:
            headline += f" ({' '.join(bits)})"
        if amount:
            headline += f" — {amount}"

        return Detection(
            type_key=type_key,
            company_name=firm,
            title=headline,
            detected_date=parse_date(
                row.get("award_start_date") or row.get("proposal_award_date")
            ),
            url=(row.get("award_link") or row.get("company_url") or "").strip() or None,
            source_name="SBIR.gov",
            external_id=str(row.get("award_id") or row.get("contract") or "") or None,
            raw={k: row.get(k) for k in ("firm", "award_title", "agency", "phase",
                                         "award_amount", "award_start_date", "branch")},
            source_confidence=0.95,
        )


def _rows(payload) -> list[dict]:
    """sbir.gov has returned both a bare list and a wrapped object over time."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "awards"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []
