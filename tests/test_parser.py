"""Parser schema and request shape.

The live API call cannot be exercised without a key, so the client is stubbed. What
these tests do cover is the part most likely to be wrong: the schema handed to the
model, and the shape of the request built around it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from signal_engine.config import get_taxonomy
from signal_engine.llm import client as client_module
from signal_engine.llm.parser import (
    build_entry_system_prompt,
    clamp_confidence,
    coerce_date,
    entry_model,
    insight_model,
    parse_entry,
    parse_manager_insight,
)


class FakeResponse:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.model = "stub"
        self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


@pytest.fixture()
def stub(monkeypatch):
    """Install a fake Anthropic client and pretend a key is present."""

    def _install(response):
        from signal_engine.config import get_config

        fake = FakeClient(response)
        monkeypatch.setattr(client_module, "get_client", lambda: fake)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        # Parsing is opt-in and off by default in config.yaml — these tests are
        # exercising the path as it behaves when a user has turned it on.
        monkeypatch.setitem(get_config().llm, "enabled", True)
        return fake

    return _install


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_type_key_is_constrained_to_the_live_taxonomy():
    """The model is given an enum, so it cannot invent a signal type."""
    schema = entry_model().model_json_schema()
    enum = schema["$defs"]["ParsedSignal"]["properties"]["type_key"]["enum"]
    assert set(enum) == {t.key for t in get_taxonomy().types}


def test_the_schema_follows_the_taxonomy_not_the_code(monkeypatch):
    """Swap the taxonomy and the parser's schema changes with it — no code edit."""
    from signal_engine import config as config_module
    from signal_engine.config import SignalTypeSpec, Taxonomy

    fake = Taxonomy(
        version=1,
        product_fit_labels={},
        types=[
            SignalTypeSpec("widget_shortage", "Widget shortage", "A", 20, "manual", True, "x"),
            SignalTypeSpec("line_downtime", "Line downtime", "B", 15, "auto", False, "y"),
        ],
    )
    monkeypatch.setattr(config_module, "get_taxonomy", lambda: fake)
    monkeypatch.setattr("signal_engine.llm.parser.get_taxonomy", lambda: fake)

    schema = entry_model().model_json_schema()
    enum = schema["$defs"]["ParsedSignal"]["properties"]["type_key"]["enum"]
    assert set(enum) == {"widget_shortage", "line_downtime"}


def test_an_unknown_signal_type_is_rejected():
    model = entry_model()
    payload = {
        "company_name": "X", "company_domain": None, "company_country": None,
        "company_segment": None, "contact_full_name": None, "contact_job_title": None,
        "contact_email": None, "contact_linkedin_url": None, "contact_persona": None,
        "nothing_scoreable": False, "reasoning": "",
        "signals": [{
            "type_key": "definitely_not_in_the_taxonomy", "product_fit": "B",
            "confidence": 0.9, "evidence": "e", "summary": "s",
            "detected_date": None, "timeline": None,
        }],
    }
    with pytest.raises(Exception):
        model.model_validate(payload)


def test_an_empty_signal_list_is_valid():
    """Nothing scoreable is a correct answer, and the schema must permit it."""
    model = entry_model()
    inst = model.model_validate({
        "company_name": "X", "company_domain": None, "company_country": None,
        "company_segment": None, "contact_full_name": None, "contact_job_title": None,
        "contact_email": None, "contact_linkedin_url": None, "contact_persona": None,
        "signals": [], "nothing_scoreable": True, "reasoning": "Nothing to score.",
    })
    assert inst.signals == []
    assert inst.nothing_scoreable is True


def test_insight_schema_allows_a_null_signal_type():
    inst = insight_model().model_validate({
        "segment": "Nordic POC respiratory developers", "geography": "Nordics",
        "signal_type": None, "weight_adjustment": 6.0,
        "summary": "Scaling fast.", "expiry_days": 90,
        "confidence": 0.8, "nothing_scoreable": False,
    })
    assert inst.signal_type is None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def test_the_system_prompt_carries_every_taxonomy_key_and_is_cacheable():
    blocks = build_entry_system_prompt()
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    text = blocks[0]["text"]
    for spec in get_taxonomy().types:
        assert spec.key in text
        assert spec.label in text


def test_the_system_prompt_is_stable_across_calls():
    """Today's date must NOT be in the system prompt — it would silently break
    prompt caching on every single call."""
    first = build_entry_system_prompt()[0]["text"]
    second = build_entry_system_prompt()[0]["text"]
    assert first == second
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    assert today not in first


def test_the_prompt_forbids_category_exclusions():
    text = build_entry_system_prompt()[0]["text"].lower()
    assert "no category exclusions" in text
    assert "veterinary" in text


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


def test_the_request_is_built_with_the_configured_model_and_schema(stub):
    from signal_engine.config import get_config

    fake = stub(FakeResponse(parsed=object()))
    parse_entry("Some note", company_hint="Northwind")

    call = fake.messages.calls[0]
    cfg = get_config()
    assert call["model"] == cfg.llm["parser_model"]
    assert call["output_format"] is entry_model()
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"][0]["role"] == "user"
    if cfg.llm.get("parser_effort"):
        assert call["thinking"] == {"type": "adaptive"}
        assert call["output_config"]["effort"] == cfg.llm["parser_effort"]


def test_the_date_and_hints_travel_in_the_user_turn(stub):
    fake = stub(FakeResponse(parsed=object()))
    parse_entry("A note", company_hint="Northwind", today=dt.date(2026, 3, 4))

    user = fake.messages.calls[0]["messages"][0]["content"]
    assert "2026-03-04" in user
    assert "Northwind" in user
    assert "A note" in user


def test_manager_insight_uses_its_own_schema(stub):
    fake = stub(FakeResponse(parsed=object()))
    parse_manager_insight("Nordic POC developers are scaling fast.")
    assert fake.messages.calls[0]["output_format"] is insight_model()


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_an_api_exception_fails_soft(stub):
    stub(ConnectionError("network unreachable"))
    outcome = parse_entry("A note")
    assert outcome.ok is False
    assert "network unreachable" in outcome.error
    assert outcome.data is None


def test_a_refusal_fails_soft(stub):
    stub(FakeResponse(parsed=object(), stop_reason="refusal"))
    outcome = parse_entry("A note")
    assert outcome.ok is False
    assert "declined" in outcome.error


def test_a_missing_key_fails_soft_with_a_useful_message(monkeypatch):
    from signal_engine.config import get_config

    monkeypatch.setitem(get_config().llm, "enabled", True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    outcome = parse_entry("A note")
    assert outcome.ok is False
    assert "ANTHROPIC_API_KEY" in outcome.error
    assert "saved" in outcome.error.lower()


def test_parsing_is_off_by_default_even_with_a_key_present(monkeypatch):
    """A leftover key must not silently re-enable a paid code path.

    This is the exact failure it prevents: an expired or out-of-credit key sitting
    in .env used to bring the note box back and start making billed calls.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leftover-key")
    outcome = parse_entry("A note")
    assert outcome.ok is False
    assert "disabled" in outcome.error


def test_fail_soft_can_be_turned_off(stub, monkeypatch):
    from signal_engine.config import get_config
    from signal_engine.llm.client import LLMUnavailable

    stub(ConnectionError("boom"))
    monkeypatch.setitem(get_config().llm, "fail_soft", False)
    with pytest.raises(LLMUnavailable):
        parse_entry("A note")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected", [(1.5, 1.0), (-2, 0.0), (0.73, 0.73), ("nonsense", 0.0), (None, 0.0)]
)
def test_confidence_is_clamped(raw, expected):
    assert clamp_confidence(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw", ["2026-03-04", "04/03/2026", "2026/03/04", "2026-03-04T09:00:00Z"]
)
def test_dates_parse_in_several_formats(raw):
    assert coerce_date(raw) == dt.date(2026, 3, 4)


@pytest.mark.parametrize("raw", [None, "", "not a date", 12345])
def test_unparsable_dates_fall_back_to_the_default(raw):
    default = dt.date(2026, 1, 1)
    assert coerce_date(raw, default=default) == default
