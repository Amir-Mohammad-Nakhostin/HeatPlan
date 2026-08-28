"""Regression tests for cache-key location identity.

Cache keys previously rounded latitude / longitude to 3 decimal places
(~110 m), which meant two materially different requested locations could
collapse to the same cache key and the thermal grid fetched for one could
be served for the other. This is a data-integrity risk; there is no
documented FortyGuard contract that treats 3-decimal snapping as safe
thermal identity.

The current cache identity is:

    (location at ~11 cm precision, date, hour, data source)

Regressions covered here (as required by the spec):

  A. same exact location + same date + same hour  -> cache HIT
  B. same location + different hour               -> cache MISS
  C. different location + same date + same hour   -> cache MISS
                                                   (must NOT reuse the
                                                    first location's grid)
  D. repeated candidate requests for the same exact location/date/hour
     within one plan request -> no duplicate FortyGuard submission
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import fortyguard_client as fg
from app import planner
from app.fortyguard_client import HeatGrid
from app.models import ParsedIntent
from app.planner import _stationary_candidates, _request_cache_key


PHX = ZoneInfo("America/Phoenix")
NOW_10 = datetime(2026, 8, 26, 10, 0, tzinfo=PHX)   # horizon 10:00 -> 22:00


# ---------------------------------------------------------------------------
# _request_cache_key: raw identity rules
# ---------------------------------------------------------------------------
def test_request_cache_key_same_exact_location_same_hour_is_a_hit():
    """A: same coord, date, hour -> identical key."""
    ts = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)
    k1 = _request_cache_key(33.4484, -112.0740, ts)
    k2 = _request_cache_key(33.4484, -112.0740, ts)
    assert k1 == k2


def test_request_cache_key_same_location_different_hour_is_a_miss():
    """B: different hour -> different key."""
    a = _request_cache_key(33.4484, -112.0740,
                           datetime(2026, 8, 26, 14, 0, tzinfo=PHX))
    b = _request_cache_key(33.4484, -112.0740,
                           datetime(2026, 8, 26, 15, 0, tzinfo=PHX))
    assert a != b


def test_request_cache_key_close_but_distinct_locations_do_not_alias():
    """C (unit level): two coordinates ~22 m / ~11 m apart (well below the
    old 3-decimal ~110 m aliasing radius) must produce DIFFERENT keys.
    The previous behavior collapsed both onto one key and could silently
    reuse a grid across distinct locations."""
    ts = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)
    # ~22 m in latitude at Phoenix - previously collapsed by round(_, 3)
    a = _request_cache_key(33.4444, -112.0774, ts)
    b = _request_cache_key(33.4446, -112.0776, ts)
    assert a != b, "distinct locations must not alias to one cache key"

    # ~11 m in latitude - still distinct
    c = _request_cache_key(33.44440, -112.07740, ts)
    d = _request_cache_key(33.44451, -112.07751, ts)
    assert c != d


def test_request_cache_key_ignores_representation_noise_at_11cm():
    """Positive control: two coords that differ only in float representation
    noise (below ~11 cm) still hash to the SAME key; the fix does not defeat
    IEEE-754 tolerance for the exact-same-location case."""
    ts = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)
    a = _request_cache_key(33.4484, -112.0740, ts)
    b = _request_cache_key(33.4484 + 1e-9, -112.0740 - 1e-9, ts)
    assert a == b


def test_fortyguard_client_cache_key_close_but_distinct_locations_do_not_alias():
    """Same aliasing risk existed in ``fortyguard_client._cache_key`` - the
    client-level submission cache must also keep distinct coords distinct."""
    k1 = fg._cache_key(33.4444, -112.0774, "2026-08-26", 14)
    k2 = fg._cache_key(33.4446, -112.0776, "2026-08-26", 14)
    assert k1 != k2


# ---------------------------------------------------------------------------
# Integration: candidate loop never reuses a grid across distinct locations
# ---------------------------------------------------------------------------
def _intent(*, lat: float, lon: float, start_time="14:00",
            date="2026-08-26", duration=90) -> ParsedIntent:
    return ParsedIntent(
        task="test", task_type="stationary", duration_minutes=duration,
        start_time=start_time, date=date, location_name="Phoenix, AZ",
        lat=lat, lon=lon, heat_priority="medium")


def test_no_duplicate_fortyguard_submission_for_same_exact_hour(monkeypatch):
    """D: within ONE /api/plan request, several candidates that traverse the
    same exact (location, date, hour) must produce ONE FortyGuard
    submission - not one per candidate. Request-scoped caching still works
    after the aliasing fix."""
    submissions: list[tuple[float, float, str, int]] = []

    async def fake_grid(lat, lon, date, hour):
        submissions.append((lat, lon, date, hour))
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    cands, requested, _reports, _summary = asyncio.run(
        _stationary_candidates(
            _intent(lat=33.4484, lon=-112.0740, start_time="14:00",
                    duration=90),
            now=NOW_10,
        )
    )
    # Every unique (date, hour) may only be submitted ONCE per plan request.
    unique = {(d, h) for _lat, _lon, d, h in submissions}
    assert len(submissions) == len(unique)
    assert requested.valid
    assert cands


def test_different_locations_do_not_share_a_grid(monkeypatch):
    """C (integration): sending two /api/plan requests for two different
    exact locations must NOT reuse the first location's grid for the
    second location - even though the two coordinates round to the same
    3-decimal key. The old behavior would return the grid built for
    location A when serving location B; the new behavior submits BOTH.
    """
    seen: list[tuple[float, float, str, int]] = []

    async def fake_grid(lat, lon, date, hour):
        seen.append((float(lat), float(lon), date, hour))
        # Encode the origin location into the returned grid so a wrong reuse
        # would be observable.
        return HeatGrid(
            lats=[float(lat)], lons=[float(lon)],
            temps_f=[80.0 + float(lat)], source="fortyguard_live"
        )

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    # Location A and location B differ by ~22 m in latitude / longitude -
    # the previous 3-decimal cache key collapsed them into one bucket.
    loc_a = (33.44440, -112.07740)
    loc_b = (33.44460, -112.07760)

    asyncio.run(_stationary_candidates(
        _intent(lat=loc_a[0], lon=loc_a[1], start_time="14:00", duration=60),
        now=NOW_10))
    asyncio.run(_stationary_candidates(
        _intent(lat=loc_b[0], lon=loc_b[1], start_time="14:00", duration=60),
        now=NOW_10))

    # For every (date, hour) covered by both requests we must observe
    # BOTH exact locations - never only location A reused for both.
    hour_locations: dict[tuple[str, int], set[tuple[float, float]]] = {}
    for lat, lon, date, hour in seen:
        hour_locations.setdefault((date, hour), set()).add((lat, lon))

    # At least one hour is required by BOTH requests (same start / duration)
    shared_hours = [k for k, v in hour_locations.items() if len(v) >= 2]
    assert shared_hours, (
        "expected at least one hour submitted for BOTH locations - "
        "if only one location appears the client cache silently reused a "
        "grid across distinct requested locations (aliasing regression)"
    )
    for k in shared_hours:
        assert loc_a in hour_locations[k]
        assert loc_b in hour_locations[k]
