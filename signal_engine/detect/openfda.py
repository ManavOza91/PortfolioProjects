"""openFDA — 510(k) clearances and device registrations.

Free, no key, no registration. https://open.fda.gov/apis/

A clearance is a strong regulatory signal for Product B: a diagnostic approaching
or entering commercial manufacture, which is when reagent stability and unit-dose
format decisions get made.
"""

from __future__ import annotations

import datetime as dt

from ..config import get_config
from .base import Detection, DetectorResult, fetch, parse_date

FDA_510K_UI = "https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID="


class OpenFDADetector:
    name = "openfda"

    def enabled(self) -> bool:
        return bool(
            get_config().detection.get("sources", {}).get("openfda", {}).get("enabled", True)
        )

    # -- config -------------------------------------------------------------

    @property
    def _cfg(self) -> dict:
        return get_config().detection.get("sources", {}).get("openfda", {})

    def _endpoints(self) -> list[dict]:
        return list(self._cfg.get("endpoints", []))

    # -- watchlist ----------------------------------------------------------

    def for_companies(self, names: list[str], since: dt.date) -> DetectorResult:
        result = DetectorResult()
        limit = int(self._cfg.get("max_results_per_company", 20))

        for endpoint in self._endpoints():
            url = endpoint.get("url", "")
            if "510k" not in url:
                # Only the 510(k) endpoint carries applicant + decision_date in the
                # shape we need. Others are declared in config for future use.
                continue
            type_key = endpoint.get("type_key", "regulatory_submission")
            label = endpoint.get("name", "510(k)")

            for company in names:
                escaped = company.replace('"', "")
                query = (
                    f'applicant:"{escaped}"'
                    f" AND decision_date:[{since.isoformat()} TO {_today().isoformat()}]"
                )
                payload, error = fetch(
                    self.name, url, params={"search": query, "limit": limit}
                )
                result.queries_made += 1
                if error:
                    result.errors.append(error)
                    continue
                if not payload:
                    continue

                total = (
                    payload.get("meta", {}).get("results", {}).get("total", 0)
                )
                if total > limit:
                    # Silently keeping the first 50 of 90 clearances would look like
                    # a complete history when it is not. Say so.
                    result.warnings.append(
                        f"{company}: {total} {label} records in this period, "
                        f"only the first {limit} were read "
                        f"(raise detection.sources.openfda.max_results_per_company)"
                    )

                for row in payload.get("results", []):
                    detection = self._to_detection(row, type_key, label)
                    if detection:
                        result.detections.append(detection)

        return result

    # -- discovery ----------------------------------------------------------

    def discover(self, since: dt.date) -> DetectorResult:
        result = DetectorResult()
        limit = int(self._cfg.get("max_results_per_company", 20))
        keywords = self._cfg.get("discovery_keywords", []) or []

        for endpoint in self._endpoints():
            url = endpoint.get("url", "")
            if "510k" not in url:
                continue
            type_key = endpoint.get("type_key", "regulatory_submission")
            label = endpoint.get("name", "510(k)")

            for keyword in keywords:
                query = (
                    f'device_name:"{keyword}"'
                    f" AND decision_date:[{since.isoformat()} TO {_today().isoformat()}]"
                )
                payload, error = fetch(
                    self.name, url, params={"search": query, "limit": limit}
                )
                result.queries_made += 1
                if error:
                    result.errors.append(error)
                    continue
                if not payload:
                    continue
                for row in payload.get("results", []):
                    detection = self._to_detection(row, type_key, label)
                    if detection:
                        result.detections.append(detection)

        return result

    # -- shaping ------------------------------------------------------------

    def _to_detection(self, row: dict, type_key: str, label: str) -> Detection | None:
        applicant = (row.get("applicant") or "").strip()
        if not applicant:
            return None

        k_number = (row.get("k_number") or "").strip()
        device = (row.get("device_name") or "device").strip()
        decision = (row.get("decision_description") or "").strip()

        title = f"{label} {k_number}: {device}".strip()
        if decision and decision.lower() not in title.lower():
            title = f"{title} — {decision}"

        return Detection(
            type_key=type_key,
            company_name=applicant,
            title=title,
            detected_date=parse_date(row.get("decision_date")),
            url=f"{FDA_510K_UI}{k_number}" if k_number else None,
            source_name="openFDA 510(k)",
            external_id=k_number or None,
            raw={
                "k_number": k_number,
                "device_name": device,
                "applicant": applicant,
                "decision_date": row.get("decision_date"),
                "decision_description": decision,
                "country_code": row.get("country_code"),
                "state": row.get("state"),
            },
            # A clearance is a matter of public record — the source itself is certain.
            # Whether it is OUR company is decided by name matching in the runner.
            source_confidence=0.98,
        )


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()
