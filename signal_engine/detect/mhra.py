"""MHRA — the UK Public Access Registration Database (PARD). Free, no key.

Devices placed on the UK market are registered with the MHRA, including by
manufacturers based outside the UK who appoint a UK Responsible Person. That
makes this the one source here that sees a German or Singaporean company
selling into Britain.

Unlike EUDAMED, PARD records carry a registration date (`MAN_CREATED_DATE`), so
these behave as dated events and respect `--since` and `--backfill` properly.

The endpoint is the POST behind pard.mhra.gov.uk's public search page. It is not
a documented API, so it may change shape without notice — which is handled the
same way as any other source failure: reported, and the run continues.
"""

from __future__ import annotations

import datetime as dt

from ..config import get_config
from .base import Detection, DetectorResult, country_code, fetch, parse_date

PARD_UI = "https://pard.mhra.gov.uk/manufacturer-details/"


class MHRADetector:
    name = "mhra"

    def enabled(self) -> bool:
        return bool(
            get_config().detection.get("sources", {}).get("mhra", {}).get("enabled", True)
        )

    @property
    def _cfg(self) -> dict:
        return get_config().detection.get("sources", {}).get("mhra", {})

    def for_companies(self, names: list[str], since: dt.date) -> DetectorResult:
        result = DetectorResult()
        url = str(self._cfg.get("url", ""))
        limit = int(self._cfg.get("max_results_per_company", 25))
        type_key = str(self._cfg.get("type_key", "regulatory_submission"))
        base_conf = float(self._cfg.get("base_confidence", 0.90))

        for company in names:
            payload, error = fetch(
                self.name, url, json_body={"searchTerm": company}
            )
            result.queries_made += 1
            if error:
                result.errors.append(error)
                continue
            if not payload:
                continue

            rows = payload if isinstance(payload, list) else []
            if len(rows) > limit:
                result.warnings.append(
                    f"{company}: {len(rows)} MHRA registrations, "
                    f"only the first {limit} were read"
                )
                rows = rows[:limit]

            for row in rows:
                detection = self._to_detection(row, type_key, base_conf)
                if detection and detection.detected_date >= since:
                    result.detections.append(detection)

        return result

    def discover(self, since: dt.date) -> DetectorResult:
        # PARD's search takes a term, not a date range, so there is no "everything
        # registered since March" query to make. Discovery would mean guessing
        # search terms and calling whatever came back a prospect.
        return DetectorResult()

    # -- shaping ------------------------------------------------------------

    def _to_detection(
        self, row: dict, type_key: str, base_conf: float
    ) -> Detection | None:
        name = (row.get("MAN_ORGANISATION_NAME") or "").strip()
        if not name:
            return None

        country = (row.get("MAN_COUNTRY") or "").strip()
        relationship = (row.get("RELATIONSHIP") or "").strip()
        rep = (row.get("REP_NAME") or "").strip()
        org_id = row.get("MAN_ORGANISATION_ID")

        title = "MHRA: registered to place devices on the UK market"
        if country:
            title += f" (manufacturer in {country})"

        return Detection(
            type_key=type_key,
            company_name=name,
            title=title,
            detected_date=parse_date(row.get("MAN_CREATED_DATE")),
            url=f"{PARD_UI}{org_id}" if org_id else None,
            source_name="MHRA PARD",
            external_id=str(org_id) if org_id else None,
            raw={
                "organisation": name,
                "country": country,
                "relationship": relationship,
                "uk_responsible_person": rep,
                "registered": row.get("MAN_CREATED_DATE"),
                "last_updated": row.get("LAST_UPDATED_DATE"),
                "city": row.get("MAN_CITY"),
            },
            country=country_code(country),
            source_confidence=base_conf,
            unique_per_event=True,
        )
