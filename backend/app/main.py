"""HeatPlan FastAPI application.

Endpoints:
  GET  /api/health
  GET  /api/config             -> default city + data source label
  POST /api/plan/structured    -> STRUCTURED request (date, duration, time
                                  window, location) -> best time + alternatives.
                                  PRIMARY endpoint; fully deterministic, no LLM.
  GET  /api/locations/search   -> deterministic location suggestions
                                  (offline gazetteer; no fake geocoder)
  POST /api/plan               -> legacy natural-language endpoint, retained
                                  for backward compatibility only; the
                                  frontend no longer uses it.

Serves the built React frontend from frontend/dist when present.
The FortyGuard API key is used ONLY here on the server side.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .explain import explain
from .fortyguard_client import (
    MAX_FUTURE_HOURS,
    FortyGuardError,
    forecast_horizon,
    validate_us_coordinates,
)
from .intent import parse_intent
from typing import Optional

from .location import LocationResolutionError, location_resolver
from .models import (
    ForecastWindowResponse,
    LocationSuggestion,
    PlanRequest,
    PlanResponse,
    StructuredCandidateOut,
    StructuredLocation,
    StructuredPlanRequest,
    StructuredPlanResponse,
)
from .planner import IntervalUnsupportedError, generate_plans
from .structured import (
    StructuredInputError,
    check_forecast_horizon,
    resolve_window,
    run_structured_plan,
)
from .us_geo import coordinates_range_valid

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="HeatPlan", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "data_source": settings.data_source}


@app.get("/api/config")
def config() -> dict:
    return {
        "default_city": settings.default_city_name,
        "default_lat": settings.default_city_lat,
        "default_lon": settings.default_city_lon,
        "data_source": settings.data_source,
        "coverage": "United States only (FortyGuard API)",
        "forecast_horizon_hours": MAX_FUTURE_HOURS,
    }


@app.post("/api/plan/structured", response_model=StructuredPlanResponse)
async def plan_structured(req: StructuredPlanRequest) -> StructuredPlanResponse:
    """Primary planning endpoint: deterministic structured input.

    Validation order (nothing reaches FortyGuard before all gates pass):
      location resolution -> US polygon gate -> date/duration/window
      validation -> forecast-horizon gate -> unique-hour thermal fetch
      -> exposure model -> deterministic ranking.
    """
    try:
        result = await run_structured_plan(req)
    except StructuredInputError as e:
        raise HTTPException(status_code=422, detail=e.to_detail())
    except FortyGuardError as e:
        raise HTTPException(status_code=502, detail={
            "code": "FORTYGUARD_ERROR", "message": str(e)})
    except Exception as e:  # noqa: BLE001 - never leak stack traces to users
        logging.getLogger("heatplan").exception("structured plan failed")
        raise HTTPException(status_code=500,
                            detail={"code": "INTERNAL",
                                    "message": f"Planning failed: {type(e).__name__}"})

    window = result["window"]

    def _out(c) -> StructuredCandidateOut:
        return StructuredCandidateOut(
            start_time=c.start_time, end_time=c.end_time,
            duration_minutes=c.duration_minutes,
            thermal_status=c.thermal_status,
            avg_temperature_f=c.avg_temperature_f,
            exposure=c.exposure, timeline=c.timeline, reason=c.reason)

    return StructuredPlanResponse(
        best=_out(result["best"]),
        alternatives=[_out(c) for c in result["alternatives"]],
        candidates=[_out(c) for c in result["candidates"]],
        location_name=window.location.display_name,
        latitude=window.location.latitude,
        longitude=window.location.longitude,
        timezone=window.location.timezone,
        date=window.date,
        duration_minutes=window.duration_minutes,
        available_start=req.available_start,
        available_end=req.available_end,
        data_source=settings.data_source,  # type: ignore[arg-type]
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/api/locations/search", response_model=list[LocationSuggestion])
def location_search(q: str = "") -> list[LocationSuggestion]:
    """Deterministic offline location suggestions for the search field.

    No live geocoder is configured in this build; suggestions come from the
    curated U.S. gazetteer (app.location) and raw 'lat, lon' input.
    """
    return [
        LocationSuggestion(display_name=r.display_name, latitude=r.latitude,
                           longitude=r.longitude, timezone=r.timezone)
        for r in location_resolver.search(q)
    ]


def _fmt_local(dt) -> str:
    """Destination-local wall clock as '9:45 PM' (no leading zero)."""
    return dt.strftime("%I:%M %p").lstrip("0")


@app.get("/api/forecast/window", response_model=ForecastWindowResponse)
def forecast_window(q: str = "", lat: Optional[float] = None,
                    lon: Optional[float] = None, date: str = "",
                    start: str = "", end: str = "",
                    duration_minutes: int = 0) -> ForecastWindowResponse:
    """Pre-flight forecast-horizon check (no FortyGuard request is ever made).

    Resolves the location (text or coordinates) through the deterministic
    LocationResolver, expresses the rolling +12h horizon in the
    DESTINATION's local wall-clock time, and - when a full candidate window
    is supplied - runs the same deterministic validation the planning
    endpoint uses. The UI uses this to fail fast (and to show the available
    forecast span) before any thermal request is submitted.
    """
    try:
        if q.strip():
            location = location_resolver.resolve(q)
        elif lat is not None and lon is not None:
            if not coordinates_range_valid(lat, lon):
                raise LocationResolutionError(
                    "Coordinates are not a valid latitude/longitude pair.")
            location = location_resolver._from_coordinates(
                lat, lon, label="selected location")
        else:
            raise LocationResolutionError(
                "Provide a location (text or coordinates).")
    except LocationResolutionError as e:
        raise HTTPException(status_code=422,
                            detail={"code": e.code, "message": str(e)})

    hs, he = forecast_horizon()
    hs_l, he_l = hs.astimezone(location.tz), he.astimezone(location.tz)
    resp = ForecastWindowResponse(
        timezone=location.timezone,
        horizon_start=hs_l.isoformat(),
        horizon_end=he_l.isoformat(),
        horizon_display=f"{_fmt_local(hs_l)} – {_fmt_local(he_l)}",
        location_name=location.display_name,
        latitude=location.latitude,
        longitude=location.longitude,
    )
    if date and start and end and duration_minutes > 0:
        req = StructuredPlanRequest(
            date=date, duration_minutes=duration_minutes,
            available_start=start, available_end=end,
            location=StructuredLocation(name="", latitude=location.latitude,
                                        longitude=location.longitude))
        try:
            window = resolve_window(req, location)
            check_forecast_horizon(window)
        except StructuredInputError as e:
            resp.ok = False
            resp.code = e.code
            resp.message = str(e)
            resp.first_unavailable_hour = e.extra.get("first_unavailable_hour")
    return resp


@app.post("/api/plan", response_model=PlanResponse)
async def plan(req: PlanRequest) -> PlanResponse:
    text = (req.text or "").strip()
    if len(text) < 5:
        raise HTTPException(status_code=422, detail="Please describe your outdoor task.")
    try:
        intent = await parse_intent(text, req.date, req.lat, req.lon)
        validate_us_coordinates(intent.lat, intent.lon)
        plans, scores, recommended = await generate_plans(intent)
    except IntervalUnsupportedError as e:
        # Distinct 422 with a STRUCTURED, user-level error body (Phase 7).
        #   message            - concise user-level explanation, built from
        #                        the REQUESTED interval alone (C3): never a
        #                        dump of internal alternative-candidate
        #                        timestamps.
        #   requested_interval - the requested interval's own hours with
        #                        per-hour status (OUTSIDE_HORIZON and
        #                        UNAVAILABLE stay distinct).
        #   diagnostics        - compact search-space summary only; raw
        #                        candidate internals stay server-side.
        #   code               - THERMAL_INTERVAL_UNSUPPORTED or
        #                        NO_FEASIBLE_ALTERNATIVE.
        raise HTTPException(status_code=422, detail={
            "code": e.code,
            "message": str(e),
            "requested_interval": e.requested_interval_public,
            "diagnostics": e.diagnostics_public,
        })
    except FortyGuardError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001 - never leak stack traces to users
        logging.getLogger("heatplan").exception("plan failed")
        raise HTTPException(status_code=500,
                            detail=f"Planning failed: {type(e).__name__}")
    return PlanResponse(
        intent=intent,
        plans=plans,
        recommended_id=recommended,  # type: ignore[arg-type]
        scores=scores,
        explanation=explain(intent, plans, recommended, scores),
        data_source=settings.data_source,  # type: ignore[arg-type]
        location_name=intent.location_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


# ---- Static frontend (production single-service deploy) ----
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        target = DIST / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(DIST / "index.html")
