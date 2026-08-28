"""FortyGuard Temperature API client.

Implements ONLY documented behavior from the official docs
(https://docs-api.fortyguard.com):
  POST {base}/v1/heatmap        -> {"data": {"activity_id": ...}}
  GET  {base}/v1/status/{id}    -> {"data": {"status": ..., "result": ...}}
  header: api-key

All analysis endpoints are asynchronous: submit -> activity_id -> bounded
polling until status Completed (terminal: Failed). Credentials stay
server-side and are never logged.

When no API key is configured (or FORTYGUARD_MOCK=true), the client runs in
MOCK mode and returns deterministic synthetic heat grids. Mock data is always
labeled via Settings.data_source and never presented as live data.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import settings
from .us_geo import coordinates_range_valid, is_in_us

log = logging.getLogger("heatplan.fortyguard")

# NOTE: the previous CONUS lat/lon bounding box (24..50 N, -125..-66 E) was
# geometrically wrong for US-only validation - it accepted Toronto, Ciudad
# Juárez, Windsor, Tijuana, Vancouver BC, and any other non-US location that
# happened to fall inside that rectangle. The real US-only check lives in
# app.us_geo.is_in_us and is a polygon membership test against a packaged
# (offline, deterministic) US polygon. Constants are kept only as public
# lat/lon numeric-range bounds and are not the U.S. gate.
US_LAT_MIN, US_LAT_MAX = -90.0, 90.0
US_LON_MIN, US_LON_MAX = -180.0, 180.0

MAX_FUTURE_HOURS = 12
MIN_DATE = "2019-01-01"  # current documented historical range start

PHOENIX_TZ = ZoneInfo("America/Phoenix")  # fixed UTC-7; Phoenix has NO daylight saving

# D3 - explicit, documented integration assumption. FortyGuard's official
# documentation does NOT state the timezone interpretation of start_time, and
# the documented payload carries no timezone field. HeatPlan's product
# semantics are Phoenix-local, so we always submit the Phoenix wall-clock hour
# of the requested timestamp (the AOI locale). Developer-machine timezone
# never matters: all datetimes are normalized to PHOENIX_TZ before the hour
# is extracted (see planner._start_local).
FORTYGUARD_STARTTIME_TZ = "local_to_aoi"  # start_time = AOI-locale (Phoenix) clock time


def now_phoenix() -> datetime:
    """Current local time in Phoenix - the app's reference forecast clock.

    HEATPLAN_NOW_OVERRIDE (ISO-8601, UTC) pins the clock for tests/demos so
    the rolling +N h horizon is reproducible; unset -> real wall clock.
    """
    override = os.environ.get("HEATPLAN_NOW_OVERRIDE", "").strip()
    if override:
        dt = datetime.fromisoformat(override)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(PHOENIX_TZ)
    return datetime.now(PHOENIX_TZ)


def forecast_horizon(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Rolling forecast window: now .. now + 12 h (Phoenix local time).

    This is the API's *availability limit*, NOT a bundle of twelve hourly
    heatmaps.  A single POST /v1/heatmap with filter_type=1 covers exactly
    one hour; each required hour still needs its own request
    (see app.thermal.resolve_thermal_interval).
    """
    now = now if now is not None else now_phoenix()
    return now, now + timedelta(hours=MAX_FUTURE_HOURS)


def timestamp_within_horizon(ts: datetime, h_start: datetime, h_end: datetime) -> bool:
    """Inclusive check: a whole-hour timestamp inside the rolling window."""
    return h_start <= ts <= h_end


class FortyGuardError(Exception):
    """User-facing-safe FortyGuard failure (never contains the API key)."""


class ThermalDataUnavailableError(FortyGuardError):
    """The requested timestamp is INSIDE the +12 h horizon, but FortyGuard
    returned no usable spatial tiles (map_data.features == [] / n_cells == 0).

    This is a per-timestamp data gap - NOT "the forecast window ended here".
    Callers must classify it as unavailable for that hour and must never
    fabricate a grid to fill it.
    """


def has_usable_spatial_data(result: dict) -> bool:
    """True only when the live response carries usable spatial tiles.

    map_data.features == []  -> False (confirmed: no data for this hour)
    n_cells == 0             -> False (same, explicit signal)
    no map_data at all       -> unknown shape: parsing delegated to
                                _extract_grid, which fails loudly if the
                                payload is unparseable.
    """
    if not isinstance(result, dict) or not result:
        return False
    map_data = result.get("map_data")
    if isinstance(map_data, dict) and "features" in map_data:
        if not (map_data.get("features") or []):
            return False  # empty FeatureCollection: no spatial result to use
        return True
    return True  # other documented shapes are validated by _extract_grid


@dataclass
class HeatGrid:
    """Normalized thermal grid: parallel arrays of lat/lon/temperature_f."""

    lats: list[float]
    lons: list[float]
    temps_f: list[float]
    source: str  # "fortyguard_live" | "mock"

    def nearest_temp(self, lat: float, lon: float) -> float:
        best_i, best_d = 0, float("inf")
        for i in range(len(self.lats)):
            d = (self.lats[i] - lat) ** 2 + (self.lons[i] - lon) ** 2
            if d < best_d:
                best_d, best_i = d, i
        return self.temps_f[best_i]

    def points(self) -> list[tuple[float, float, float]]:
        return list(zip(self.lats, self.lons, self.temps_f))


def validate_us_coordinates(lat: float, lon: float) -> None:
    """Reject any coordinate that is not inside the United States.

    Two-stage gate (both must pass):

      1. Latitude / longitude are numerically in range
         (lat ∈ [-90, 90], lon ∈ [-180, 180]).  Obviously invalid inputs are
         rejected here with a distinct message before any polygon lookup.
      2. The coordinate falls inside the packaged U.S. polygon
         (``app.us_geo.is_in_us`` - see that module for the definition of
         "United States").  A rectangular envelope is NOT sufficient: it
         accepts Toronto, Ciudad Juárez, Tijuana, Windsor, Vancouver BC,
         and other non-US locations inside a CONUS bounding box.

    The check is fully offline and deterministic: no runtime internet
    request, no external geocoding API, no dependency on the developer's
    machine location.
    """
    if not coordinates_range_valid(lat, lon):
        raise FortyGuardError(
            "Coordinates are not a valid latitude/longitude pair "
            f"({lat}, {lon}). Latitude must be in [-90, 90] and longitude "
            "in [-180, 180]."
        )
    if not is_in_us(lat, lon):
        raise FortyGuardError(
            "FortyGuard coverage is U.S.-only. "
            f"Coordinates ({lat:.4f}, {lon:.4f}) are outside the supported area. "
            "The demo defaults to Phoenix, AZ."
        )


def _aoi_polygon(lat: float, lon: float, half_size_deg: float = 0.006) -> dict:
    """Small square AOI (~1.3 km^2 at Phoenix latitude), well under the
    documented ~130 km^2 / 50 mi^2 maximum."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [lon - half_size_deg, lat - half_size_deg],
                            [lon + half_size_deg, lat - half_size_deg],
                            [lon + half_size_deg, lat + half_size_deg],
                            [lon - half_size_deg, lat + half_size_deg],
                            [lon - half_size_deg, lat - half_size_deg],
                        ]
                    ],
                },
            }
        ],
    }


class _TTLCache:
    def __init__(self, ttl_s: int):
        self.ttl = ttl_s
        self._store: dict[str, tuple[float, HeatGrid]] = {}

    def get(self, key: str) -> HeatGrid | None:
        hit = self._store.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        return None

    def set(self, key: str, value: HeatGrid) -> None:
        self._store[key] = (time.time(), value)


_cache = _TTLCache(settings.cache_ttl_s)


# Coordinates are normalized to a fixed decimal precision so that
# floating-point representation quirks (e.g. 33.4484 vs 33.44840000001) map
# to the same cache key, WITHOUT collapsing materially different requested
# locations onto each other. Six decimal places is ~11 cm at Phoenix latitude
# - far below any AOI, tile, or activity-scale semantic distance HeatPlan
# reasons about, and yet still unique per requested location.
#
# Why NOT 3 decimal places (the previous behavior): 3 decimals rounds to
# ~110 m and would collapse two clearly different user-requested locations
# (e.g. 33.4444, -112.0774 and 33.4446, -112.0776) into a single cache key,
# so a thermal grid fetched for one requested location could be silently
# served for a materially different location. FortyGuard's AOI/tile identity
# is per-submission (the AOI polygon is derived from the exact requested
# coordinate); no documented FortyGuard contract states that 3-decimal
# rounding is a safe thermal-identity substitution. Removing the aliasing.
_CACHE_COORD_DECIMALS = 6


def _cache_key(lat: float, lon: float, date: str, hour: int) -> str:
    return (
        f"heatmap:"
        f"{round(float(lat), _CACHE_COORD_DECIMALS):.{_CACHE_COORD_DECIMALS}f}:"
        f"{round(float(lon), _CACHE_COORD_DECIMALS):.{_CACHE_COORD_DECIMALS}f}:"
        f"{date}:{hour}"
    )


# --------------------------------------------------------------------------
# Live client
# --------------------------------------------------------------------------
#
# P1 DECISION RECORD - filter_type=2 (Range of Hours) was AUDITED and is
# deliberately NOT used in this MVP. Rationale:
#
#   1. HeatPlan's correctness rests on exact-hour identity: every thermal
#      sample at hour H must come from the grid fetched for hour H, and a
#      missing hour must make a candidate infeasible. The verified live
#      single-hour response (backend/fortyguard_raw_result.json) contains
#      NO per-tile temporal field: each tile carries tile_id +
#      average/min/max temperature only, and stats_data is a single
#      aggregate over the request. With one activity per hour, the hour is
#      unambiguous (it is the request parameter). In a range response the
#      tiles for different hours cannot be re-attributed to their exact
#      hour from the documented payload alone.
#   2. Empty-hour semantics would be destroyed: with per-hour activities,
#      features == [] / n_cells == 0 maps to UNAVAILABLE for exactly that
#      hour. A range request cannot distinguish "hour H empty" from
#      "hour H absent" / "whole range empty" without a per-hour
#      breakdown the documented response schema does not provide.
#   3. Cache semantics are per (location, date, hour); a range response
#      could not be decomposed into exact-hour cache entries safely.
#
# Correctness beats request-count reduction: filter_type=1 (one activity
# per required hour, and ONLY the required hours) remains the strategy.
# filter_type=2 can be revisited only if a captured range response proves
# that per-tile hourly identity and per-hour emptiness are preserved.
async def _submit_heatmap(client: httpx.AsyncClient, lat: float, lon: float,
                          date: str, hour: int) -> str:
    payload = {
        "polygon_aoi": _aoi_polygon(lat, lon),
        "date_time": {
            "start_date": date,
            "start_time": f"{hour:02d}:00",
            # filter_type=1 requests exactly ONE hour; each required hourly
            # timestamp gets its own submission (see app.thermal). Range
            # mode (filter_type=2) is intentionally NOT used - see the
            # decision record above _submit_heatmap.
            "filter_type": 1,
        },
        "granularity": 100,
    }
    try:
        resp = await client.post(
            f"{settings.fortyguard_base_url}/v1/heatmap",
            headers={"api-key": settings.fortyguard_api_key,
                     "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise FortyGuardError(f"FortyGuard request failed to reach the API: {type(e).__name__}")
    if resp.status_code in (400, 422):
        raise FortyGuardError(f"FortyGuard rejected the request (HTTP {resp.status_code}).")
    if resp.status_code == 401:
        raise FortyGuardError("FortyGuard authentication failed (invalid API key).")
    if resp.status_code == 403:
        raise FortyGuardError("FortyGuard plan does not allow this request (HTTP 403).")
    if resp.status_code == 429:
        raise FortyGuardError("FortyGuard rate limit exceeded; try again shortly.")
    if resp.status_code != 200:
        raise FortyGuardError(f"FortyGuard returned HTTP {resp.status_code}.")
    try:
        activity_id = resp.json()["data"]["activity_id"]
    except (ValueError, KeyError, TypeError):
        raise FortyGuardError("FortyGuard returned a malformed submission response.")
    log.info(
        "FortyGuard activity submitted: activity_id=%s "
        "submitted_date=%s submitted_time=%02d:00",
        activity_id, date, hour,
    )
    return activity_id


async def _poll_status(client: httpx.AsyncClient, activity_id: str) -> dict:
    """Bounded backoff polling per the official quickstart (5 s, ~120 rounds).

    L1 - at the START of every iteration:
      1. recompute the remaining time and stop immediately when the deadline
         has expired (no final in-flight HTTP request is started after it);
      2. cap the HTTP timeout to the remaining time so a slow status request
         cannot overshoot the global deadline.

    L2 - every iteration logs activity_id, poll count, status and elapsed
    seconds; the terminal state (completed / failed / timeout / error) is
    logged explicitly. The API key is only ever sent in the api-key header
    and is never logged.
    """
    deadline = time.monotonic() + settings.poll_timeout_s
    interval = settings.poll_interval_s
    start = time.monotonic()
    poll_count = 0
    status = "Submitted"
    while True:
        poll_count += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "activity_id=%s poll=%d status=%s elapsed_s=%.1f terminal=timeout",
                activity_id, poll_count, status, time.monotonic() - start,
            )
            raise FortyGuardError(
                f"FortyGuard task timed out (activity {activity_id}).")
        # L1: never let a single HTTP request outlive the global deadline.
        timeout = min(30.0, max(1.0, remaining))
        try:
            resp = await client.get(
                f"{settings.fortyguard_base_url}/v1/status/{activity_id}",
                headers={"api-key": settings.fortyguard_api_key},
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            log.warning(
                "activity_id=%s poll=%d status=http_error elapsed_s=%.1f "
                "terminal=error",
                activity_id, poll_count, time.monotonic() - start,
            )
            raise FortyGuardError(
                f"FortyGuard status check failed: {type(e).__name__}")
        if resp.status_code == 404:
            # Documented: may be temporarily unavailable right after submission.
            status = "not_found(404)"
        elif resp.status_code != 200:
            raise FortyGuardError(
                f"FortyGuard status returned HTTP {resp.status_code}.")
        else:
            try:
                data = resp.json()["data"]
            except (ValueError, KeyError, TypeError):
                raise FortyGuardError(
                    "FortyGuard returned a malformed status response.")
            status = str(data.get("status", "")).lower()
            if status in ("completed", "succeeded"):
                log.info(
                    "activity_id=%s poll=%d status=%s elapsed_s=%.1f "
                    "terminal=completed",
                    activity_id, poll_count, status, time.monotonic() - start,
                )
                return data.get("result") or {}
            if status in ("failed", "error"):
                log.warning(
                    "activity_id=%s poll=%d status=%s elapsed_s=%.1f "
                    "terminal=failed",
                    activity_id, poll_count, status, time.monotonic() - start,
                )
                raise FortyGuardError(
                    f"FortyGuard task failed (activity {activity_id}).")
        log.info(
            "activity_id=%s poll=%d status=%s elapsed_s=%.1f",
            activity_id, poll_count, status, time.monotonic() - start,
        )
        if time.monotonic() > deadline:
            log.warning(
                "activity_id=%s poll=%d status=%s elapsed_s=%.1f terminal=timeout",
                activity_id, poll_count, status, time.monotonic() - start,
            )
            raise FortyGuardError(
                f"FortyGuard task timed out (activity {activity_id}).")
        await asyncio.sleep(interval)
        interval = min(interval * 1.25, 15.0)  # gentle backoff, don't hammer


def _extract_grid(result: dict) -> HeatGrid:
    """Normalize FortyGuard's documented heatmap result into point samples.

    DOCUMENTED LIVE SHAPE (C5): the live heatmap is a GeoJSON
    FeatureCollection under ``result.map_data`` and the per-tile temperature
    field is ``properties.average_temperature`` in degrees CELSIUS (captured
    live payloads carry exactly tile_id + average/min/max temperature).
    HeatPlan converts it to Fahrenheit internally because the exposure UI
    and v1 burden model are expressed in °F.

    ONLY the documented ``average_temperature`` field is treated as a live
    Celsius source. An ambiguous generic field ("temperature", "temp", ...)
    is NEVER silently assumed Celsius on the live path: without
    ``average_temperature`` those tiles are ignored and an all-empty result
    raises ThermalDataUnavailableError rather than implying a Celsius value
    that was never documented.

    LEGACY/OFFLINE COMPATIBILITY (explicitly isolated): the older MVP
    point/geojson test shapes (top-level FeatureCollection,
    ``result["geojson"]``, ``result["points"]``) are accepted for offline
    fixtures only, and their generic temperature fields are interpreted as
    ALREADY-FAHRENHEIT legacy values - never as Celsius. Nothing else is
    invented.
    """
    if not isinstance(result, dict) or not result:
        raise FortyGuardError("FortyGuard returned an empty heatmap result.")

    lats: list[float] = []
    lons: list[float] = []
    temps_f: list[float] = []

    # Documented live tile field (degrees CELSIUS) - the ONLY accepted live
    # temperature source (C5).
    _LIVE_TILE_FIELD = "average_temperature"
    # Legacy/offline fixture fields (already degrees FAHRENHEIT), explicitly
    # isolated from the live path and never interpreted as Celsius.
    _LEGACY_TILE_FIELDS = (
        "temperature", "temp", "temperature_f", "Temperature",
        "Temperature_C", "Average_Temperature", "avg_temperature", "tcm",
        "temperature_c", "temp_c",
    )

    def _finite(value) -> float | None:
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        return None

    def _live_temperature_c(props: dict) -> float | None:
        """Documented live field only; generic keys never imply Celsius."""
        return _finite(props.get(_LIVE_TILE_FIELD))

    def _legacy_temperature_f(props: dict) -> float | None:
        """Legacy/offline compatibility fields, already °F."""
        for key in _LEGACY_TILE_FIELDS:
            value = _finite(props.get(key))
            if value is not None:
                return value
        return None

    def _centroid(coords) -> tuple[float, float] | None:
        """Simple polygon centroid for small heatmap tiles.

        GeoJSON coordinates are [lon, lat]. For the tiny AOIs used by HeatPlan,
        an arithmetic ring centroid is sufficient and avoids adding geometry
        dependencies. Returns (lat, lon).
        """
        try:
            ring = coords[0] if coords and isinstance(coords[0], list) else coords
            pts = [(float(x), float(y)) for x, y, *_ in ring]
            if len(pts) < 3:
                return None
            # Remove duplicated closing vertex for the arithmetic centroid.
            if pts[0] == pts[-1]:
                pts = pts[:-1]
            if not pts:
                return None
            lon = sum(x for x, _ in pts) / len(pts)
            lat = sum(y for _, y in pts) / len(pts)
            return lat, lon
        except (TypeError, ValueError):
            return None

    def _append_live_tile(lat: float, lon: float, temp_c: float) -> None:
        # FortyGuard TCM is Celsius; internal HeatPlan exposure is Fahrenheit.
        temps_f.append(temp_c * 9.0 / 5.0 + 32.0)
        lats.append(lat)
        lons.append(lon)

    def _walk_geojson(gj: dict, *, live_celsius: bool) -> None:
        for feat in gj.get("features", []) or []:
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry") or {}
            props = feat.get("properties") or {}
            value = (_live_temperature_c(props) if live_celsius
                     else _legacy_temperature_f(props))
            if value is None:
                continue
            kind = str(geom.get("type", ""))
            if kind == "Point":
                try:
                    lon, lat = geom["coordinates"][:2]
                except (KeyError, TypeError, ValueError):
                    continue
                if live_celsius:
                    _append_live_tile(float(lat), float(lon), value)
                else:
                    lats.append(float(lat)); lons.append(float(lon)); temps_f.append(value)
            elif kind == "Polygon":
                center = _centroid(geom.get("coordinates"))
                if center:
                    lat, lon = center
                    if live_celsius:
                        _append_live_tile(lat, lon, value)
                    else:
                        lats.append(lat); lons.append(lon); temps_f.append(value)
            elif kind == "MultiPolygon":
                for polygon in geom.get("coordinates", []) or []:
                    center = _centroid(polygon)
                    if center:
                        lat, lon = center
                        if live_celsius:
                            _append_live_tile(lat, lon, value)
                        else:
                            lats.append(lat); lons.append(lon); temps_f.append(value)

    # Current documented shape: result.map_data = GeoJSON FeatureCollection;
    # result.stats_data contains aggregate statistics, but tile values are
    # required for spatial planning and therefore are preferred.
    map_data = result.get("map_data")
    if isinstance(map_data, dict) and map_data.get("type") == "FeatureCollection":
        _walk_geojson(map_data, live_celsius=True)
    elif result.get("type") == "FeatureCollection":
        _walk_geojson(result, live_celsius=False)
    elif isinstance(result.get("geojson"), dict):
        _walk_geojson(result["geojson"], live_celsius=False)
    elif isinstance(result.get("points"), list):
        for p in result["points"]:
            try:
                lat = float(p["lat"]); lon = float(p["lon"])
                value = p.get("temperature", p.get("temperature_f"))
                if value is not None:
                    lats.append(lat); lons.append(lon); temps_f.append(float(value))
            except (KeyError, TypeError, ValueError):
                continue

    if not temps_f:
        # A completed task with no tile temperatures (map_data.features == []
        # or n_cells == 0) means this timestamp currently has NO usable
        # spatial result.  That is a per-timestamp gap, not the end of the
        # 12 h forecast window, and it is never patched with fake data.
        raise ThermalDataUnavailableError(
            "FortyGuard completed the heatmap but returned no usable spatial "
            "tiles for this timestamp (map_data.features == [] / n_cells == 0)."
        )
    return HeatGrid(lats=lats, lons=lons, temps_f=temps_f, source="fortyguard_live")


async def _fetch_download(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url, timeout=60)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise FortyGuardError("FortyGuard result download was not valid JSON/GeoJSON.")


# --------------------------------------------------------------------------
# Mock client (development only; always labeled)
# --------------------------------------------------------------------------
def _mock_grid(lat: float, lon: float, hour: int) -> HeatGrid:
    """Deterministic synthetic Phoenix-like thermal grid for development.

    Diurnal curve peaks near 15:00; spatial micro-variation comes from a
    hash of the coordinates so results are reproducible and differ across
    space (which the planner needs). This is NOT real FortyGuard data and is
    labeled as mock everywhere it surfaces."""
    lats: list[float] = []
    lons: list[float] = []
    temps: list[float] = []
    diurnal = 14.0 * math.sin((hour - 9) / 24.0 * 2 * math.pi)  # peak ~15h
    base = 90.0 + diurnal  # Phoenix summer baseline
    step = 0.0015
    for i in range(-4, 5):
        for j in range(-4, 5):
            la = lat + i * step
            lo = lon + j * step
            h = int(hashlib.sha256(f"{la:.5f},{lo:.5f}".encode()).hexdigest(), 16)
            micro = (h % 1000) / 1000.0 * 6.0 - 3.0  # ±3°F micro-climate
            lats.append(la); lons.append(lo); temps.append(round(base + micro, 2))
    return HeatGrid(lats=lats, lons=lons, temps_f=temps, source="mock")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
async def get_heat_grid(lat: float, lon: float, date: str, hour: int) -> HeatGrid:
    """Return a normalized heat grid for ONE hour (filter_type=1).

    The +12 h horizon is an availability limit, not a bundle of hours: the
    caller (app.thermal) requests exactly the hourly timestamps it needs and
    treats empty spatial results (ThermalDataUnavailableError) as per-
    timestamp unavailability - never as "the forecast window ended".
    """
    validate_us_coordinates(lat, lon)
    key = _cache_key(lat, lon, date, hour)
    cached = _cache.get(key)
    if cached:
        return cached
    if settings.use_mock:
        grid = _mock_grid(lat, lon, hour)
        _cache.set(key, grid)
        return grid

    async with httpx.AsyncClient() as client:
        activity_id = await _submit_heatmap(client, lat, lon, date, hour)
        result = await _poll_status(client, activity_id)
        link = result.get("download_link") if isinstance(result, dict) else None
        if link and not result.get("map_data") and not result.get("features") and not result.get("points"):
            result = await _fetch_download(client, link)
        grid = _extract_grid(result)
    _cache.set(key, grid)
    return grid
