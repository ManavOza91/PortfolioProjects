"""Language-model layer: the parser now, the coach in a later phase."""

from .client import ParseOutcome, api_key_present, parse_structured  # noqa: F401
from .parser import (  # noqa: F401
    clamp_confidence,
    coerce_date,
    parse_entry,
    parse_manager_insight,
)
