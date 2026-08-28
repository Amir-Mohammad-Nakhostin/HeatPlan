"""Structured-input workflow: deterministic validation and planning.

This module replaces natural-language planning as the PRIMARY path:

    STRUCTURED USER INPUT
        -> DETERMINISTIC VALIDATION   (no LLM anywhere in this file)
        -> TIME / HORIZON RESOLUTION  (destination-local, IANA timezone)
        -> LOCATION RESOLUTION        (app.location.LocationResolver)
        -> THERMAL DATA REQUEST       (one per unique required hour)
        -> THERMAL ANALYSIS           (existing exposure model)
        -> RESULT

What this module deliberately does NOT do:
  * no natural-language parsing of the request fields;
  * no FortyGuard submission before location, date, time window, duration,
    and the forecast-horizon gate have ALL passed (saves API credits);
  * no fabricated thermal data - every reported number comes from a grid
    fetched for its exact (location, date, hour).

Error codes surfaced to the API layer (spec §17):
  INVALID_LOCATION, LOCATION_OUTSIDE_SUPPORTED_AREA, INVALID_DATE,
  INVALID_DURATION, INVALID_TIME_WINDOW, FORECAST_HORIZON_EXCEEDED,
  THERMAL_INTERVAL_UNSUPPORTED, NO_FEASIBLE_ALTERNATIVE, FORTYGUARD_ERROR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .fortyguard_client import (
    MAX_FUTURE_HOURS,
    forecast_horizon,
    validate_us_coordinates,
)
from .location import LocationResolutionError, ResolvedLocation
from .models import StructuredPlanRequest
from .thermal import TimestampStatus
from . import planner as _planner  # exact-hour cache + scoring (preserved)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class StructuredInputError(Exception):
    """Deterministic, user-facing validation failure (HTTP 422).

    ``detail`` carries only user-level fields - never internal traces,
    API keys, signed URLs, or raw FortyGuard payloads.
    """

    def __init__(self, code: str, message: str, **extra):
        self.code = code
        self.extra = extra
        super().__init__(message)

    def to_detail(self) -> dict:
        return {"code": self.code, "message": str(self), **self.extra}


class ForecastHorizonExceededError(StructuredInputError):
    """The requested window needs hours beyond the +12h forecast horizon.

    Raised BEFORE any FortyGuard request is submitted (never partially
    submitted), and NEVER re-labeled as THERMAL_INTERVAL_UNSUPPORTED: the
    actual reason is horizon coverage, not an empty live response.
    """

    def __init__(self, *, window_start: datetime, window_end: datetime,
                 first_unavailable: datetime,
                 horizon_start: datetime, horizon_end: datetime):
        self.first_unavailable = first_unavailable
        super().__init__(
            "FORECAST_HORIZON_EXCEEDED",
            "The requested time window extends beyond the current "
            f"+{MAX_FUTURE_HOURS}h forecast horizon (usable until "
            f"{horizon_end.strftime('%H:%M')} local). First unavailable "
            f"requested hour: {first_unavailable.strftime('%H:%M')}.",
            requested_window={
                "start": window_start.isoformat(),
                "end": window_end.isoformat(),
            },
            horizon_start=horizon_start.isoformat(),
            horizon_end=horizon_end.isoformat(),
            first_unavailable_hour=first_unavailable.strftime("%Y-%m-%d %H:%M"),
        )


# ---------------------------------------------------------------------------
# Deterministic field parsing / validation
# ---------------------------------------------------------------------------
def _parse_date(value: str) -> datetime:
    """YYYY-MM-DD -> a date; malformed input is INVALID_DATE (422)."""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        raise StructuredInputError(
            "INVALID_DATE",
            "The date must be a valid calendar date in YYYY-MM-DD format.")


def _parse_hhmm(value: str, code: str = "INVALID_TIME_WINDOW") -> tuple[int, int]:
    try:
        h_s, m_s = value.strip().split(":")
        h, m = int(h_s), int(m_s)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except (ValueError, AttributeError):
        raise StructuredInputError(
            code, "Times must use 24-hour HH:MM format (e.g. 13:00).")


@dataclass
class ResolvedWindow:
    """The user's request resolved into destination-local datetimes."""

    location: ResolvedLocation
    date: str                      # YYYY-MM-DD (echo of user input)
    duration_minutes: int
    window_start: datetime         # destination-local, tz-aware
    window_end: datetime           # destination-local, tz-aware
    candidate_starts: list[datetime] = field(default_factory=list)

    @property
    def window_minutes(self) -> int:
        return int((self.window_end - self.window_start).total_seconds() // 60)


def generate_candidate_starts(window_start: datetime, window_end: datetime,
                              duration_minutes: int) -> list[datetime]:
    """Deterministic sliding window over the user's availability.

    duration=60, window 13:00->17:00  ->  13:00, 14:00, 15:00, 16:00.
    No candidate may exceed the user's window, and no LLM is involved.
    Starts step by whole hours from the window start.
    """
    starts: list[datetime] = []
    cursor = window_start
    step = timedelta(hours=1)
    span = timedelta(minutes=duration_minutes)
    while cursor + span <= window_end:
        starts.append(cursor)
        cursor = cursor + step
    return starts


def resolve_window(req: StructuredPlanRequest,
                   location: ResolvedLocation,
                   *, now: Optional[datetime] = None) -> ResolvedWindow:
    """Validate the structured request and resolve it to destination-local
    datetimes. All validation errors are raised BEFORE any thermal request.

    TIMEZONE (spec §5): date + available_start/end are wall-clock fields at
    the DESTINATION. They are stamped with the destination's IANA zone via
    zoneinfo - never the developer machine's zone, never a hard-coded UTC
    offset (DST-safe).
    """
    tz = location.tz

    # --- duration -----------------------------------------------------------
    duration = req.duration_minutes
    if duration is None or int(duration) <= 0:
        raise StructuredInputError(
            "INVALID_DURATION", "Duration must be greater than 0 minutes.")
    duration = int(duration)

    # --- date ---------------------------------------------------------------
    day = _parse_date(req.date)

    # --- time window ---------------------------------------------------------
    sh, sm = _parse_hhmm(req.available_start)
    eh, em = _parse_hhmm(req.available_end)
    window_start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
    window_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
    if window_end <= window_start:
        raise StructuredInputError(
            "INVALID_TIME_WINDOW",
            "The end of your time window must be after its start.")
    if duration > int((window_end - window_start).total_seconds() // 60):
        raise StructuredInputError(
            "INVALID_TIME_WINDOW",
            "The duration is longer than the available time window.")

    # --- location: existing U.S. polygon gate stays active -------------------
    # (Before ANY FortyGuard submission - spec §20.)
    try:
        validate_us_coordinates(location.latitude, location.longitude)
    except Exception as e:  # FortyGuardError: range or outside-US
        msg = str(e)
        code = ("INVALID_LOCATION" if "valid latitude" in msg
                else "LOCATION_OUTSIDE_SUPPORTED_AREA")
        raise StructuredInputError(code, msg)

    # --- candidates ----------------------------------------------------------
    starts = generate_candidate_starts(window_start, window_end, duration)
    if not starts:  # defensive; duration<=window guarantees >= 1
        raise StructuredInputError(
            "INVALID_TIME_WINDOW",
            "No candidate start fits inside the available time window.")

    return ResolvedWindow(
        location=location, date=req.date, duration_minutes=duration,
        window_start=window_start, window_end=window_end,
        candidate_starts=starts)


# ---------------------------------------------------------------------------
# Forecast-horizon gate (BEFORE any FortyGuard submission)
# ---------------------------------------------------------------------------
def check_forecast_horizon(window: ResolvedWindow,
                           *, now: Optional[datetime] = None) -> None:
    """Validate every required hour of every candidate against the rolling
    +12h forecast horizon. Raises ForecastHorizonExceededError identifying
    the FIRST unavailable requested hour. No partial submission: when this
    fails, zero thermal requests have been made.

    The horizon clock is the destination's local wall clock (the same clock
    the user's times were interpreted against); the rolling horizon itself
    is the existing ``forecast_horizon`` window. For destinations on other
    U.S. zones the instants are compared timezone-aware, so zone arithmetic
    is exact.
    """
    horizon_start, horizon_end = forecast_horizon(now)
    first_bad: Optional[datetime] = None
    for start in window.candidate_starts:
        for ts in _planner_required_hours(start, window.duration_minutes):
            if not (horizon_start <= ts <= horizon_end):
                if first_bad is None or ts < first_bad:
                    first_bad = ts
    if first_bad is not None:
        raise ForecastHorizonExceededError(
            window_start=window.window_start, window_end=window.window_end,
            first_unavailable=first_bad,
            horizon_start=horizon_start, horizon_end=horizon_end)


def _planner_required_hours(start: datetime, duration_minutes: int) -> list[datetime]:
    from .thermal import required_hourly_timestamps
    return required_hourly_timestamps(start, duration_minutes)


# ---------------------------------------------------------------------------
# Thermal fetch: one request per UNIQUE (location, date, hour)
# ---------------------------------------------------------------------------
def _required_hours_union(window: ResolvedWindow) -> list[datetime]:
    """Unique exact-hour timestamps needed by ANY candidate.

    Candidates 13->14, 14->15, 15->16, 16->17 with duration 60 need hours
    13, 14, 15, 16 - FOUR thermal requests total, never four per candidate.
    """
    seen: dict[tuple[str, int], datetime] = {}
    for start in window.candidate_starts:
        for ts in _planner_required_hours(start, window.duration_minutes):
            key = (ts.strftime("%Y-%m-%d"), ts.hour)
            seen.setdefault(key, ts.replace(minute=0, second=0, microsecond=0))
    return [seen[k] for k in sorted(seen)]


async def fetch_unique_hours(window: ResolvedWindow,
                             *, now: Optional[datetime] = None,
                             request_cache: Optional[dict] = None):
    """Fetch exactly the unique required hours (deduped, request-scoped).

    Returns (grids_by_hour, per_hour_checks). A requested hour that fails
    inside the horizon is UNAVAILABLE (per-timestamp gap - never borrowed
    from another hour/location and never fabricated). Hours outside the
    horizon are never requested (already gated by check_forecast_horizon).
    """
    from .fortyguard_client import ThermalDataUnavailableError

    hours = _required_hours_union(window)
    grids: dict = {}
    checks: list[dict] = []
    cache = request_cache if request_cache is not None else {}
    for ts in hours:
        key = _planner._request_cache_key(
            window.location.latitude, window.location.longitude, ts)
        try:
            if key in cache:
                grid = cache[key]
            else:
                grid = await _planner.get_heat_grid(
                    window.location.latitude, window.location.longitude,
                    ts.strftime("%Y-%m-%d"), ts.hour)
                cache[key] = grid
            grids[_planner._hour_key(ts)] = grid
            checks.append({"timestamp": ts.strftime("%H:%M"),
                           "status": TimestampStatus.USABLE.value,
                           "detail": f"{len(grid.temps_f)} tile samples"})
        except ThermalDataUnavailableError:
            checks.append({"timestamp": ts.strftime("%H:%M"),
                           "status": TimestampStatus.UNAVAILABLE.value,
                           "detail": "map_data.features == [] / n_cells == 0"})
    return grids, checks


# ---------------------------------------------------------------------------
# Candidate evaluation + deterministic ranking
# ---------------------------------------------------------------------------
@dataclass
class StructuredCandidate:
    start_time: str          # HH:MM destination-local
    end_time: str            # HH:MM destination-local
    start_dt: datetime
    duration_minutes: int
    thermal_status: str      # "ok" | "unavailable_hours"
    avg_temperature_f: Optional[float] = None
    exposure: Optional[float] = None   # estimated thermal exposure (burden·min)
    timeline: list[dict] = field(default_factory=list)
    # Deterministic, backend-derived explanation of this candidate's rank.
    reason: str = ""


async def evaluate_candidates(window: ResolvedWindow,
                              *, now: Optional[datetime] = None,
                              request_cache: Optional[dict] = None):
    """Evaluate every candidate deterministically.

    A candidate is feasible only when EVERY required hour is inside the
    horizon AND usable. Estimated thermal exposure uses the existing v1
    model (app.exposure) over the EXACT hour grids - the same semantics as
    the legacy planner's stationary path.
    """
    grids, hour_checks = await fetch_unique_hours(
        window, now=now, request_cache=request_cache)

    candidates: list[StructuredCandidate] = []
    for start in window.candidate_starts:
        end = start + timedelta(minutes=window.duration_minutes)
        hours = _planner_required_hours(start, window.duration_minutes)
        if not all(_planner._hour_key(h) in grids for h in hours):
            candidates.append(StructuredCandidate(
                start_time=start.strftime("%H:%M"), end_time=end.strftime("%H:%M"),
                start_dt=start, duration_minutes=window.duration_minutes,
                thermal_status="unavailable_hours"))
            continue
        expo, samples, avg = _planner._score_hourly(
            start, window.duration_minutes, grids,
            window.location.latitude, window.location.longitude)
        candidates.append(StructuredCandidate(
            start_time=start.strftime("%H:%M"), end_time=end.strftime("%H:%M"),
            start_dt=start, duration_minutes=window.duration_minutes,
            thermal_status="ok", avg_temperature_f=round(avg, 1),
            exposure=round(expo, 2),
            timeline=[{"offset_minutes": int(o), "temperature_f": round(t, 1)}
                      for o, t, _ in samples]))
    return candidates, hour_checks


def rank_candidates(candidates: list[StructuredCandidate]) -> list[StructuredCandidate]:
    """Deterministic ranking: lowest estimated thermal exposure wins;
    earliest start breaks ties. No LLM output is involved."""
    feasible = [c for c in candidates
                if c.thermal_status == "ok" and c.exposure is not None]
    return sorted(feasible, key=lambda c: (c.exposure, c.start_dt))


# ---------------------------------------------------------------------------
# Deterministic explanation (backend-derived facts only - never an LLM)
# ---------------------------------------------------------------------------
def _alt_reason(best: StructuredCandidate, alt: StructuredCandidate) -> str:
    """Explain an alternative relative to the best option using ONLY the
    deterministic values the planner computed (exposure, temperature)."""
    parts: list[str] = []
    if best.exposure is not None and alt.exposure is not None:
        if best.exposure > 0:
            pct = (alt.exposure - best.exposure) / best.exposure * 100.0
            if pct <= 1.0:
                parts.append("nearly the same estimated thermal exposure "
                             "as the best time")
            else:
                parts.append(f"estimated thermal exposure about {pct:.0f}% "
                             "higher than the best time")
        elif alt.exposure == best.exposure:
            parts.append("the same estimated thermal exposure as the best time")
    if best.avg_temperature_f is not None and alt.avg_temperature_f is not None:
        delta = alt.avg_temperature_f - best.avg_temperature_f
        if abs(delta) >= 0.5:
            parts.append(f"about {abs(delta):.0f} °F "
                         f"{'warmer' if delta > 0 else 'cooler'}")
    if not parts:
        return "Another available time in your window."
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def explain_ranking(ranked: list[StructuredCandidate],
                    *, total_candidates: int) -> dict[str, str]:
    """Deterministic start_time -> reason map for the ranked feasible set.

    Every statement is a direct verbalization of the deterministic ranking
    (lowest exposure, earliest-start tie-break) and the computed
    exposures/temperatures. No numbers are invented.
    """
    reasons: dict[str, str] = {}
    if not ranked:
        return reasons
    best = ranked[0]
    if len(ranked) == 1:
        reasons[best.start_time] = (
            "The only time that fits inside your window."
            if total_candidates <= 1 else
            "The only time in your window with usable live thermal data.")
    elif ranked[1].exposure == best.exposure:
        reasons[best.start_time] = (
            "Tied for the lowest estimated thermal exposure; it is the "
            "earliest of those options.")
    else:
        reasons[best.start_time] = (
            "Lowest estimated thermal exposure among the available times "
            "in your window.")
    for alt in ranked[1:]:
        reasons[alt.start_time] = _alt_reason(best, alt)
    return reasons


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run_structured_plan(req: StructuredPlanRequest,
                              *, now: Optional[datetime] = None) -> dict:
    """Full structured pipeline: validate -> resolve -> gate -> fetch -> rank.

    Raises StructuredInputError (422-level) for any deterministic failure;
    FortyGuardError propagates for live-API failures (502-level at the API
    layer).
    """
    from .location import location_resolver

    # 1. LOCATION RESOLUTION (deterministic; never fabricated).
    if req.location_text and req.location_text.strip():
        try:
            location = location_resolver.resolve(req.location_text)
        except LocationResolutionError as e:
            raise StructuredInputError(e.code, str(e))
    elif req.location is not None:
        # Explicit coordinates from the UI's location picker.
        try:
            location = location_resolver._from_coordinates(
                req.location.latitude, req.location.longitude,
                label=req.location.name or
                f"({req.location.latitude}, {req.location.longitude})")
        except LocationResolutionError as e:
            raise StructuredInputError(e.code, str(e))
    else:
        raise StructuredInputError(
            "INVALID_LOCATION",
            "Choose a location: a U.S. park, city, address, or coordinates.")

    # 2. DETERMINISTIC VALIDATION + TIME/HORIZON RESOLUTION.
    window = resolve_window(req, location, now=now)

    # 3. FORECAST-HORIZON GATE - before ANY FortyGuard request.
    check_forecast_horizon(window, now=now)

    # 4. THERMAL DATA REQUEST: unique required hours only (deduped).
    candidates, hour_checks = await evaluate_candidates(window, now=now)

    # 5. THERMAL ANALYSIS + deterministic ranking.
    ranked = rank_candidates(candidates)
    if not ranked:
        unavailable = sorted({c["timestamp"] for c in hour_checks
                              if c["status"] == TimestampStatus.UNAVAILABLE.value})
        hs, he = forecast_horizon(now)
        raise StructuredInputError(
            "NO_FEASIBLE_ALTERNATIVE",
            "No time inside your window has usable live thermal data"
            + (f" (unavailable hours: {', '.join(unavailable)})."
               if unavailable else ".")
            + " No missing thermal data is fabricated.",
            requested_window={
                "start": window.window_start.isoformat(),
                "end": window.window_end.isoformat(),
            },
            unavailable_hours=unavailable,
            horizon_start=hs.isoformat(), horizon_end=he.isoformat())

    reasons = explain_ranking(ranked, total_candidates=len(candidates))
    for c in candidates:
        c.reason = reasons.get(c.start_time, "")

    best = ranked[0]
    return {
        "best": best,
        "alternatives": ranked[1:],
        "candidates": candidates,
        "hour_checks": hour_checks,
        "window": window,
    }
