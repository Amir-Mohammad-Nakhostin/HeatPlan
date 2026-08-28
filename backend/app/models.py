"""Shared pydantic models for HeatPlan."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    text: str = Field(..., description="Natural language outdoor task")
    # Explicit coordinates ALWAYS take precedence over any location the
    # intent parser (LLM or fallback) extracts from the text.
    lat: Optional[float] = None
    lon: Optional[float] = None
    # YYYY-MM-DD, defaults to today (Phoenix). Planning is forecast-only:
    # the start must land inside the rolling Phoenix-local window
    # [now, now + 12h); past dates and starts beyond the window are
    # rejected with a distinct outside-horizon error - historical planning
    # is not supported and no data is fabricated for it.
    date: Optional[str] = None


class ParsedIntent(BaseModel):
    task: str = "outdoor activity"
    task_type: Literal["stationary", "route"] = "stationary"
    duration_minutes: int = 60
    start_time: str = "14:00"  # HH:MM local
    date: str = ""
    location_name: str = ""
    lat: float = 0.0
    lon: float = 0.0
    end_lat: Optional[float] = None
    end_lon: Optional[float] = None
    end_location_name: Optional[str] = None
    heat_priority: Literal["low", "medium", "high"] = "medium"
    parser: Literal["llm", "fallback"] = "fallback"
    warnings: list[str] = []


class TimelinePoint(BaseModel):
    offset_minutes: int
    temperature_f: float
    heat_burden: float
    lat: Optional[float] = None
    lon: Optional[float] = None


class Plan(BaseModel):
    id: Literal["A", "B", "C"]
    name: str
    strategy: str
    start_time: str
    duration_minutes: float
    distance_m: float
    # Estimated temperature-based thermal burden (burden-minutes), built
    # from raw tile temperatures; NOT an apparent-temperature or
    # physiological metric, and not medically validated.
    exposure: float
    exposure_reduction_pct: float = 0.0  # vs Plan A baseline
    time_penalty_minutes: float = 0.0    # vs Plan A baseline
    avg_temperature_f: float
    geometry: list[list[float]] = []  # [[lat, lon], ...]
    timeline: list[TimelinePoint] = []
    tradeoff: str = ""


class PlanResponse(BaseModel):
    intent: ParsedIntent
    plans: list[Plan]
    recommended_id: Literal["A", "B", "C"]
    scores: dict[str, float]
    explanation: str
    data_source: Literal["fortyguard_live", "mock"]
    location_name: str
    generated_at: str


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Structured-input architecture (primary workflow)
# ---------------------------------------------------------------------------
class StructuredLocation(BaseModel):
    """Explicit coordinates from the UI location picker.

    The frontend resolves place names via GET /api/locations/search and
    submits the resolved coordinates; the backend never parses a location
    out of a natural-language sentence.
    """
    name: str = ""
    latitude: float
    longitude: float


class StructuredPlanRequest(BaseModel):
    """POST /api/plan/structured request body (spec §18).

    date:              YYYY-MM-DD (destination-local calendar date)
    duration_minutes:  integer > 0
    available_start:   HH:MM destination-local (24-hour)
    available_end:     HH:MM destination-local (24-hour), after start
    location:          resolved coordinates (from the location picker), OR
    location_text:     free text resolved by the deterministic LocationResolver
    """
    date: str = Field(..., description="YYYY-MM-DD, destination-local")
    duration_minutes: int = Field(..., gt=0)
    available_start: str = Field(..., description="HH:MM destination-local")
    available_end: str = Field(..., description="HH:MM destination-local")
    location: Optional[StructuredLocation] = None
    location_text: Optional[str] = None


class StructuredCandidateOut(BaseModel):
    start_time: str
    end_time: str
    duration_minutes: int
    thermal_status: str
    avg_temperature_f: Optional[float] = None
    exposure: Optional[float] = None
    timeline: list[dict] = []
    # Deterministic, backend-derived explanation of this candidate's rank.
    # Built only from computed exposures/temperatures - never LLM-invented.
    reason: str = ""


class StructuredPlanResponse(BaseModel):
    """Structured result: one best time plus other available times."""
    best: StructuredCandidateOut
    alternatives: list[StructuredCandidateOut] = []
    candidates: list[StructuredCandidateOut] = []
    location_name: str
    latitude: float
    longitude: float
    timezone: str
    date: str
    duration_minutes: int
    available_start: str
    available_end: str
    data_source: Literal["fortyguard_live", "mock"]
    generated_at: str
    disclaimer: str = (
        "Estimated thermal exposure is decision support computed from "
        "FortyGuard environmental data - not medical advice, not a safety "
        "guarantee."
    )


class LocationSuggestion(BaseModel):
    display_name: str
    latitude: float
    longitude: float
    timezone: str


class ForecastWindowResponse(BaseModel):
    """GET /api/forecast/window: the rolling +12h forecast horizon expressed
    in the destination's local wall-clock time, plus an optional deterministic
    pre-flight check of a candidate time window. Lets the UI validate the
    horizon and resolve the location BEFORE any FortyGuard request."""
    timezone: str
    horizon_start: str  # ISO, destination-local (offset-bearing)
    horizon_end: str    # ISO, destination-local (offset-bearing)
    horizon_display: str  # e.g. "10:00 AM – 9:45 PM"
    location_name: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    ok: bool = True
    code: Optional[str] = None
    message: Optional[str] = None
    first_unavailable_hour: Optional[str] = None
