"""P1 regression tests: intent-parsing consistency between LLM and fallback.

The same API request MUST resolve to the same coordinates and date whether
or not an LLM provider is enabled:

  * explicit PlanRequest lat/lon take precedence in BOTH modes - an LLM may
    classify language but may never override caller-supplied coordinates;
  * the request `date` is honored in BOTH modes;
  * malformed clock times never produce an invalid HH:MM that crashes the
    planner (previously "at 99:99" -> start_time "23:99" -> HTTP 500).
"""
import asyncio

import pytest

from app import intent as intent_mod
from app.intent import fallback_parse, parse_intent


def test_fallback_honors_explicit_coordinates():
    i = fallback_parse("walk in Miami for 30 minutes", "2026-08-26",
                       33.4484, -112.0740)
    assert (i.lat, i.lon) == (33.4484, -112.0740)
    assert i.location_name == "Selected location"


def test_llm_mode_preserves_explicit_coordinates(monkeypatch):
    """LLM extracts 'Miami' from the text, but the caller pinned Phoenix
    coordinates: the response must use the caller's coordinates."""
    monkeypatch.setattr(intent_mod.settings, "llm_provider", "openai")
    monkeypatch.setattr(intent_mod.settings, "openai_api_key", "test-key")

    async def fake_llm(text):
        return {"task": "walking", "duration_minutes": 30,
                "start_time": "15:00", "heat_priority": "medium",
                "location": "Miami"}

    monkeypatch.setattr(intent_mod, "llm_parse", fake_llm)
    i = asyncio.run(parse_intent("walk in Miami for 30 minutes",
                                 "2026-08-26", 33.4484, -112.0740))
    assert i.parser == "llm"
    assert (i.lat, i.lon) == (33.4484, -112.0740)  # NOT Miami's gazetteer coords
    assert i.location_name == "Selected location"


def test_same_request_same_location_regardless_of_llm(monkeypatch):
    """Identical input -> identical coordinates with the LLM on or off."""
    async def fake_llm(text):
        return {"task": "walking", "duration_minutes": 30,
                "start_time": "15:00", "heat_priority": "medium",
                "location": "Tucson"}

    monkeypatch.setattr(intent_mod, "llm_parse", fake_llm)
    monkeypatch.setattr(intent_mod.settings, "llm_provider", "")
    monkeypatch.setattr(intent_mod.settings, "openai_api_key", "")
    fallback = asyncio.run(parse_intent("walk for 30 minutes",
                                        "2026-08-26", 33.5, -112.1))
    monkeypatch.setattr(intent_mod.settings, "llm_provider", "openai")
    monkeypatch.setattr(intent_mod.settings, "openai_api_key", "test-key")
    llm = asyncio.run(parse_intent("walk for 30 minutes",
                                   "2026-08-26", 33.5, -112.1))
    assert (fallback.lat, fallback.lon) == (llm.lat, llm.lon) == (33.5, -112.1)


def test_llm_mode_preserves_request_date(monkeypatch):
    """The `date` field must survive LLM parsing exactly as in fallback."""
    async def fake_llm(text):
        return {"task": "walking", "duration_minutes": 30}

    monkeypatch.setattr(intent_mod, "llm_parse", fake_llm)
    monkeypatch.setattr(intent_mod.settings, "llm_provider", "openai")
    monkeypatch.setattr(intent_mod.settings, "openai_api_key", "test-key")
    i = asyncio.run(parse_intent("walk for 30 minutes", "2026-08-27", None, None))
    assert i.date == "2026-08-27"
    assert fallback_parse("walk for 30 minutes", "2026-08-27", None, None).date \
        == "2026-08-27"


@pytest.mark.parametrize("bad", ["99:99", "25:00", "12:75", "9:99"])
def test_fallback_rejects_malformed_clock_time(bad):
    """Malformed times fall back to 14:00 WITH a warning - never a clamped
    or out-of-range HH:MM that would crash planner._start_local."""
    i = fallback_parse(f"walk outside for 30 minutes at {bad}",
                       "2026-08-26", None, None)
    assert i.start_time == "14:00"
    assert any("not a valid clock time" in w for w in i.warnings)


def test_llm_rejects_out_of_range_minutes(monkeypatch):
    """An LLM returning '14:75' must not be accepted verbatim."""
    async def fake_llm(text):
        return {"task": "walking", "duration_minutes": 30,
                "start_time": "14:75"}

    monkeypatch.setattr(intent_mod, "llm_parse", fake_llm)
    monkeypatch.setattr(intent_mod.settings, "llm_provider", "openai")
    monkeypatch.setattr(intent_mod.settings, "openai_api_key", "test-key")
    i = asyncio.run(parse_intent("walk for 30 minutes", "2026-08-26", None, None))
    assert i.start_time == "14:00"  # ParsedIntent default, LLM value rejected
