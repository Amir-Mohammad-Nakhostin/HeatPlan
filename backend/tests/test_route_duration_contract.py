"""Explicit regression tests for the route-duration contract.

See ``backend/docs/route_duration_contract.md`` for the prose definition.
This file pins the behavior of the four canonical quantities:

    A. USER REQUESTED DURATION  - intent.duration_minutes
    B. ACTUAL ROUTE DURATION    - _path_m(path) / WALK_SPEED_M_S / 60
    C. THERMAL COVERAGE         - required_hourly_timestamps(start, B)
    D. EXPOSURE                 - exposure() over samples along B

with the following cases from the spec:

    1. requested duration > actual route duration
    2. requested duration < actual route duration
    3. requested duration == actual route duration
    4. route crossing an hour boundary
    5. route requiring multiple hourly thermal grids
    6. route crossing midnight / date rollover (architecture check)
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import fortyguard_client as fg
from app import planner
from app.fortyguard_client import HeatGrid, ThermalDataUnavailableError
from app.models import ParsedIntent
from app.planner import (
    WALK_SPEED_M_S,
    _path_m,
    _route_candidates,
    _sample_path,
)
from app.thermal import required_hourly_timestamps


PHX = ZoneInfo("America/Phoenix")
NOW_10 = datetime(2026, 8, 26, 10, 0, tzinfo=PHX)   # horizon 10:00 -> 22:00
START = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)


def _grid(temp_f: float = 100.0, *, points=None) -> HeatGrid:
    if points is None:
        points = [(33.4484, -112.0740)]
    return HeatGrid(
        lats=[p[0] for p in points],
        lons=[p[1] for p in points],
        temps_f=[temp_f] * len(points),
        source="fortyguard_live",
    )


def _route_intent(*, duration_min: int, start_time="14:00",
                  date="2026-08-26",
                  a=(33.4000, -112.0000),
                  b=(33.4200, -112.0000)) -> ParsedIntent:
    return ParsedIntent(
        task="walking", task_type="route", duration_minutes=duration_min,
        start_time=start_time, date=date, location_name="Phoenix, AZ",
        lat=a[0], lon=a[1], end_lat=b[0], end_lon=b[1],
        end_location_name="End", heat_priority="medium")


# ---------------------------------------------------------------------------
# Contract 1: requested > actual (A > B)
# ---------------------------------------------------------------------------
def test_requested_duration_gt_actual_route_duration(monkeypatch):
    """User asks for 240 minutes; the direct 2.2 km walk is ~26 min. The
    candidate is scored on its ACTUAL 26 min duration - never stretched to
    match the requested 240 min - and requires only hour 14 in coverage."""
    async def fake_grid(lat, lon, date, hour):
        # only hour 14 is served; that's enough for a 26-min walk
        if hour == 14:
            return _grid(90.0)
        raise ThermalDataUnavailableError("empty")
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    intent = _route_intent(duration_min=240)  # much longer than the walk
    cands, requested, reports, _summary = asyncio.run(
        _route_candidates(intent, now=NOW_10))
    # Every produced candidate's ACTUAL duration is what it was scored on.
    for c in cands:
        # actual duration comes from path distance / walk speed - not 240
        dur = _path_m(c.geometry) / WALK_SPEED_M_S / 60.0
        assert abs(c.duration_minutes - dur) < 1.0
        # 240 min is NOT what we scored
        assert c.duration_minutes < 240
    # requested-interval resolution still uses the requested 240 min
    # (spans hours 14..17). That is INTENT-scoped; independent of candidates.
    assert requested.required[0].hour == 14
    assert requested.required[-1].hour >= 17


# ---------------------------------------------------------------------------
# Contract 2: requested < actual (A < B)
# ---------------------------------------------------------------------------
def test_requested_duration_lt_actual_route_duration(monkeypatch):
    """User asks for 30 min but the walk-speed constant is slowed so the
    2.2 km direct route ACTUALLY takes ~73 min - crossing hour 14 -> 15.
    Hour 15 is UNAVAILABLE, so the candidate must be INFEASIBLE (excluded
    and reported), NOT silently scored on the requested-30-min coverage
    window (which alone would only ever require hour 14)."""
    async def fake_grid(lat, lon, date, hour):
        if hour == 14:
            return _grid(90.0, points=[
                (33.4000, -112.0000), (33.4200, -112.0000),
            ])
        raise ThermalDataUnavailableError("empty")
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    # Force ACTUAL walk duration > 60 min on the direct 2.2 km chord.
    monkeypatch.setattr(planner, "WALK_SPEED_M_S", 0.5)  # ~73 min

    intent = _route_intent(duration_min=30)  # A = 30 min
    with pytest.raises(planner.IntervalUnsupportedError) as ei:
        asyncio.run(_route_candidates(intent, now=NOW_10))

    # The requested-interval (A) alone spans only hour 14 - which is USABLE.
    # The failure comes from validating the ACTUAL route duration (B), which
    # requires hour 15 (UNAVAILABLE). This proves the pipeline gates on B,
    # never on A alone.
    err = ei.value
    assert err.requested_result.valid, \
        "requested-30-min interval is supported by hour 14; failure MUST " \
        "come from actual-route-duration coverage, not from A"
    # Every candidate report shows the actual-duration coverage that failed.
    assert err.candidate_reports, \
        "candidate reports must be present so the failure attribution is " \
        "traceable to the ACTUAL route duration, not the requested duration"
    assert any(any(chk.status.value == "unavailable" for chk in r.result.checks)
               for r in err.candidate_reports)


# ---------------------------------------------------------------------------
# Contract 3: requested == actual (A == B)
# ---------------------------------------------------------------------------
def test_requested_equals_actual_route_duration_smoke(monkeypatch):
    """When the requested duration and the actual walk duration happen to
    be equal (or numerically close) the two concepts remain conceptually
    distinct: the candidate's scored duration still comes from geometry,
    not the intent field. This test pins that the pipeline does not
    special-case A == B."""
    async def fake_grid(lat, lon, date, hour):
        if hour in (14, 15):
            return _grid(90.0, points=[
                (33.4000, -112.0000), (33.4200, -112.0000),
                (33.4100, -111.9800), (33.4300, -111.9700),
            ])
        raise ThermalDataUnavailableError("empty")
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    # Direct 2.2 km at 1.4 m/s ≈ 26 min. Match the intent to that.
    intent = _route_intent(duration_min=26)
    cands, _requested, _reports, _summary = asyncio.run(
        _route_candidates(intent, now=NOW_10))
    assert cands
    for c in cands:
        dur = _path_m(c.geometry) / WALK_SPEED_M_S / 60.0
        # duration_minutes is derived from geometry, not copied from intent
        assert abs(c.duration_minutes - dur) < 1.0


# ---------------------------------------------------------------------------
# Contract 4: route crossing an hour boundary
# ---------------------------------------------------------------------------
def test_route_crossing_hour_boundary_requires_both_hours():
    """If a route's actual duration spans hour 14 -> hour 15, its thermal
    coverage must include BOTH hours. required_hourly_timestamps is the
    single source of truth for that list."""
    ts = required_hourly_timestamps(START, 90)
    assert [t.hour for t in ts] == [14, 15]

    # And the sampler uses each sample's own hour.
    from app.planner import _sample_path
    g14 = _grid(90.0, points=[(33.4000, -112.0000), (33.4200, -112.0000)])
    g15 = _grid(110.0, points=[(33.4000, -112.0000), (33.4200, -112.0000)])
    grids = {START: g14, START + timedelta(hours=1): g15}

    # Slow walk to force spanning both hours.
    from app import planner as pl
    old_speed = pl.WALK_SPEED_M_S
    try:
        pl.WALK_SPEED_M_S = 0.5   # ~2.2 km at 0.5 m/s ≈ 73 min
        samples = _sample_path([[33.4000, -112.0000], [33.4200, -112.0000]],
                               START, 73.0, grids)
    finally:
        pl.WALK_SPEED_M_S = old_speed
    temps = {t for _, t, _ in samples}
    # Both hours' own grids appear in the samples.
    assert 90.0 in temps and 110.0 in temps


# ---------------------------------------------------------------------------
# Contract 5: route requiring multiple hourly thermal grids
# ---------------------------------------------------------------------------
def test_route_requiring_three_hourly_grids_all_hours_covered():
    """A ~150-minute walk starting at 14:00 needs hours 14, 15, 16."""
    ts = required_hourly_timestamps(START, 150)
    assert [t.hour for t in ts] == [14, 15, 16]


# ---------------------------------------------------------------------------
# Contract 6: route crossing midnight / date rollover
# ---------------------------------------------------------------------------
def test_route_crossing_midnight_rolls_date_correctly():
    """A 90-minute walk starting at 23:30 must require the previous day's
    23:00 hour AND the next day's 00:00 hour, with the date correctly
    rolled forward."""
    start = datetime(2026, 8, 26, 23, 30, tzinfo=PHX)
    ts = required_hourly_timestamps(start, 90)
    assert [(t.day, t.hour) for t in ts] == [(26, 23), (27, 0)]
    # Timezone stays Phoenix (fixed UTC-7).
    for t in ts:
        assert t.tzinfo is PHX
        assert t.utcoffset().total_seconds() == -7 * 3600
