"""Anthropic client wrapper.

Fail-soft by design: if the key is missing or the API is unreachable, callers get a
structured "unavailable" result instead of an exception. A human observation is the
highest-converting signal this system has — it must never be lost because a network
call failed. The raw text is always stored; parsing can be retried later.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from ..config import get_config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """Raised only when fail_soft is off."""


@dataclass
class ParseOutcome:
    """Result of one structured-output call."""

    ok: bool
    data: BaseModel | None = None
    error: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def unavailable(self) -> bool:
        return not self.ok


def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


_client: Any = None


def get_client() -> Any:
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(timeout=float(get_config().llm.get("timeout_seconds", 120)))
    return _client


def reset_client() -> None:
    global _client
    _client = None


def parse_structured(
    *,
    system: str | list[dict[str, Any]],
    user: str,
    output_format: type[T],
    model: str,
    max_tokens: int | None = None,
    effort: str | None = None,
) -> ParseOutcome:
    """One schema-validated call. Never raises unless fail_soft is disabled."""
    cfg = get_config()
    fail_soft = bool(cfg.llm.get("fail_soft", True))

    if not cfg.llm.get("enabled", True):
        return _fail("Language model features are disabled in config.yaml", fail_soft)
    if not api_key_present():
        return _fail(
            "ANTHROPIC_API_KEY is not set. Add it to a .env file next to config.yaml. "
            "Your note has been saved and can be parsed later.",
            fail_soft,
        )

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or int(cfg.llm.get("max_tokens", 4000)),
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_format": output_format,
    }
    if effort:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": effort}

    try:
        response = get_client().messages.parse(**kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        log.warning("LLM call failed: %s", exc)
        return _fail(f"{type(exc).__name__}: {exc}", fail_soft)

    if getattr(response, "stop_reason", None) == "refusal":
        return _fail("The model declined to process this text.", fail_soft)

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        return _fail("The model returned no parsable output.", fail_soft)

    usage = getattr(response, "usage", None)
    return ParseOutcome(
        ok=True,
        data=parsed,
        model=getattr(response, "model", model),
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


def _fail(message: str, fail_soft: bool) -> ParseOutcome:
    if not fail_soft:
        raise LLMUnavailable(message)
    return ParseOutcome(ok=False, error=message)
