"""EUDAMED — the EU medical device database. Free, no key, no registration.

This is the source that covers the market openFDA cannot see. A European
manufacturer with no US presence has no 510(k) and never will; it does have
EUDAMED entries.

Two things shape how this detector behaves, both of them limitations of the
source rather than choices:

1. **EUDAMED publishes no registration date** on device records. There is no way
   to ask "what is new since March". So this is not a dated event — it is a
   standing fact: "this company has N devices on the EU market". It carries a
   confidence deliberately below the auto-scoring bar and always reaches you for
   review, because a standing fact is context, not a trigger.

2. **One company, one signal.** A manufacturer with 36 registered devices is one
   piece of evidence, not 36. The devices are aggregated into a single detection
   before the runner ever sees them.
"""

from __future__ import annotations

import datetime as dt
import re

from ..config import get_config
from .base import Detection, DetectorResult, fetch

# The public search UI, filtered to the manufacturer, so the link goes somewhere useful.
EUDAMED_UI = "https://ec.europa.eu/tools/eudamed/#/screen/search-device?submitted=true&deviceStatusCode=refdata.device-model-status.on-the-market&nameSearchType=CONTAINS&name="

_RISK_ORDER = ["class-i", "class-iia", "class-iib", "class-iii"]


class EUDAMEDDetector:
    name = "eudamed"

    def enabled(self) -> bool:
        return bool(
            get_config().detection.get("sources", {}).get("eudamed", {}).get("enabled", True)
        )

    @property
    def _cfg(self) -> dict:
        return get_config().detection.get("sources", {}).get("eudamed", {})

    def for_companies(self, names: list[str], since: dt.date) -> DetectorResult:
        result = DetectorResult()
        url = str(self._cfg.get("url", ""))
        limit = int(self._cfg.get("max_results_per_company", 50))
        type_key = str(self._cfg.get("type_key", "regulatory_submission"))
        base_conf = float(self._cfg.get("base_confidence", 0.65))

        for company in names:
            payload, error = fetch(
                self.name, url,
                params={"name": company, "page": 0, "pageSize": limit},
                # EUDAMED pages a 3-million-row table and is genuinely slow; the
                # default 30s times out on more runs than it completes.
                timeout=float(self._cfg.get("timeout_seconds", 90)),
            )
            result.queries_made += 1
            if error:
                result.errors.append(error)
                continue
            if not payload:
                continue

            detection = self._aggregate(payload, company, type_key, base_conf, limit)
            if detection:
                result.detections.append(detection)

        return result

    def discover(self, since: dt.date) -> DetectorResult:
        # Searching EUDAMED by device keyword would return manufacturers we have
        # never assessed, with no date to place them in time. That is a list, not
        # a signal, and lists are what this whole system exists to replace.
        return DetectorResult()

    # -- shaping ------------------------------------------------------------

    def _aggregate(
        self, payload: dict, company: str, type_key: str, base_conf: float,
        read_limit: int,
    ) -> Detection | None:
        rows = [r for r in payload.get("content", []) if isinstance(r, dict)]
        if not rows:
            return None
        # EUDAMED caps its page at 20 whatever pageSize we ask for, so "did we see
        # everything" has to be judged on what came back, not on what we requested.
        page_rows = len(rows)

        # EUDAMED's `name` filter is a contains-match across the whole record, so
        # confirm the manufacturer really is the one we asked about. On word
        # boundaries: a bare substring test counts "Plantech Medical" as a match
        # for "Antech", which is how one company arrived claiming 3,063 devices.
        wanted = re.compile(rf"\b{re.escape(company.strip())}\b", re.IGNORECASE)
        rows = [r for r in rows if wanted.search(r.get("manufacturerName") or "")]
        if not rows:
            return None

        # `totalElements` counts what EUDAMED's contains-search returned, which for
        # a short name like "Antech" sweeps in every unrelated "...antech..." on the
        # register. Only rows that survived the manufacturer re-check are counted;
        # anything beyond the page we read is reported as a floor, not a total.
        confirmed = len(rows)
        returned = int(payload.get("totalElements") or confirmed)
        partial = returned > page_rows

        manufacturer = rows[0].get("manufacturerName") or company
        on_market = sum(
            1 for r in rows
            if "on-the-market" in (r.get("deviceStatusType") or {}).get("code", "")
        )
        highest = _highest_risk_class(rows)
        trade_names = [r.get("tradeName") for r in rows if r.get("tradeName")][:5]

        count = f"at least {confirmed}" if partial else str(confirmed)
        title = (
            f"EUDAMED: {count} device registration"
            f"{'' if confirmed == 1 and not partial else 's'} in the EU"
        )
        if highest:
            title += f", highest risk {highest.replace('class-', 'class ').upper()}"

        return Detection(
            type_key=type_key,
            company_name=manufacturer,
            title=title,
            # EUDAMED gives no registration date. Today is when WE first saw it,
            # and the summary says so rather than implying the filing is fresh.
            detected_date=dt.datetime.now(dt.timezone.utc).date(),
            url=EUDAMED_UI + company.replace(" ", "%20"),
            source_name="EUDAMED",
            external_id=(rows[0].get("manufacturerSrn") or None),
            raw={
                "manufacturer": manufacturer,
                "manufacturer_srn": rows[0].get("manufacturerSrn"),
                "devices_confirmed": confirmed,
                "search_returned": returned,
                "page_size_read": read_limit,
                "count_is_a_floor": partial,
                "on_the_market": on_market,
                "highest_risk_class": highest,
                "example_trade_names": trade_names,
                "note": "EUDAMED publishes no registration date; this is a standing "
                        "registration, not a dated event.",
            },
            source_confidence=base_conf,
            # One record per company already — the aggregation happened above.
            unique_per_event=True,
        )


def _highest_risk_class(rows: list[dict]) -> str | None:
    best = -1
    for row in rows:
        code = (row.get("riskClass") or {}).get("code", "")
        for index, name in enumerate(_RISK_ORDER):
            if code.endswith(name) and index > best:
                best = index
    return _RISK_ORDER[best] if best >= 0 else None
