"""Three-layer horizon architecture tests - user's reference example.

Phoenix now = 10:00  ->  horizon 10:00 .. 22:00
user: start 14:00, duration 60 min  ->  required [14:00] (the activity
occupies [14:00, 15:00); the endpoint is NOT an extra required hour).
If a required hour returns features == [] / n_cells == 0 -> UNAVAILABLE,
never "the forecast window ended".
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.fortyguard_client import (
    HeatGrid,
    ThermalDataUnavailableError,
    _extract_grid,
    forecast_horizon,
    has_usable_spatial_data,
    timestamp_within_horizon,
)
from app.thermal import (
    TimestampStatus,
    candidate_feasible,
    required_hourly_timestamps,
    resolve_thermal_interval,
)

PHX = ZoneInfo("America/Phoenix")
NOW_10 = datetime(2026, 8, 26, 10, 0, tzinfo=PHX)  # Phoenix now = 10:00


def _grid() -> HeatGrid:
    return HeatGrid(lats=[33.4484, 33.4490], lons=[-112.0740, -112.0730],
                    temps_f=[101.0, 103.0], source="fortyguard_live")


def _fetch_with(served: set[int]):
    """fetch(kind, ..., ts): serves a grid only for hours in `served`;
    any other hour raises the unavailable signal (empty spatial result)."""
    calls: list[int] = []

    async def fetch(lat, lon, ts):
        calls.append(ts.hour)
        if ts.hour in served:
            return _grid()
        raise ThermalDataUnavailableError(
            f"{ts:%H:%M} -> map_data.features == [] / n_cells == 0")

    return fetch, calls


def _start(hour: int) -> datetime:
    return datetime(2026, 8, 26, hour, 0, tzinfo=PHX)


def test_horizon_is_rolling_phoenix_window():
    start, end = forecast_horizon(NOW_10)
    assert (start.hour, end.hour) == (10, 22)
    assert timestamp_within_horizon(_start(22), start, end)   # boundary included
    assert not timestamp_within_horizon(_start(23), start, end)


def test_horizon_rolls_with_the_phoenix_clock():
    """The horizon is a rolling +12h window, never a fixed daily window."""
    s, e = forecast_horizon(datetime(2026, 8, 26, 4, 0, tzinfo=PHX))
    assert (s.hour, e.hour) == (4, 16)                       # 04:00 -> 16:00
    s, e = forecast_horizon(datetime(2026, 8, 26, 16, 0, tzinfo=PHX))
    assert (s.day, s.hour) == (26, 16)
    assert (e.day, e.hour) == (27, 4)                        # 16:00 -> 04:00 next day
    # Phoenix has NO DST: the offset is fixed UTC-7 all year (zoneinfo,
    # never a naive offset guess).
    assert s.utcoffset().total_seconds() == -7 * 3600
    winter = datetime(2026, 1, 15, 16, 0, tzinfo=PHX)
    assert winter.utcoffset().total_seconds() == -7 * 3600


def test_required_hourly_timestamps_match_activity_duration():
    """P0-1 spec: required hours are the hourly intervals actually traversed.

    30 min @ 14:00 -> [14]           60 min @ 14:00 -> [14]
    90 min @ 14:00 -> [14, 15]      120 min @ 14:00 -> [14, 15]
   150 min @ 14:00 -> [14, 15, 16]
    """
    assert [t.hour for t in required_hourly_timestamps(_start(14), 30)] == [14]
    assert [t.hour for t in required_hourly_timestamps(_start(14), 60)] == [14]
    assert [t.hour for t in required_hourly_timestamps(_start(14), 90)] == [14, 15]
    assert [t.hour for t in required_hourly_timestamps(_start(14), 120)] == [14, 15]
    assert [t.hour for t in required_hourly_timestamps(_start(14), 150)] == [14, 15, 16]


def test_endpoint_on_hour_boundary_is_not_added():
    """P0-1 spec: 14:00->15:00 requires 14:00 only; 14:00->16:00 requires
    14:00 and 15:00 - the exact endpoint hour is never auto-added."""
    assert [t.hour for t in required_hourly_timestamps(_start(14), 60)] == [14]
    assert [t.hour for t in required_hourly_timestamps(_start(14), 120)] == [14, 15]
    # One minute past the boundary DOES enter the next hour.
    assert [t.hour for t in required_hourly_timestamps(_start(14), 61)] == [14, 15]
    assert [t.hour for t in required_hourly_timestamps(_start(14), 121)] == [14, 15, 16]
    # Mid-hour starts still require their own starting hour.
    half = datetime(2026, 8, 26, 14, 30, tzinfo=PHX)
    assert [t.hour for t in required_hourly_timestamps(half, 30)] == [14]
    assert [t.hour for t in required_hourly_timestamps(half, 40)] == [14, 15]


def test_example_interval_all_usable_is_valid():
    fetch, calls = _fetch_with({14, 15})
    res = asyncio.run(resolve_thermal_interval(
        33.4484, -112.0740, _start(14), 60, fetch, now=NOW_10))
    assert res.valid is True
    assert candidate_feasible(res) is True
    assert all(c.status is TimestampStatus.USABLE for c in res.checks)
    assert sorted(calls) == [14]   # 60 min @ 14:00 traverses ONLY the 14:00 hour


def test_empty_hour_is_unavailable_not_horizon_end():
    fetch, calls = _fetch_with({14})   # 15:00 -> features==[]/n_cells==0
    res = asyncio.run(resolve_thermal_interval(
        33.4484, -112.0740, _start(14), 90, fetch, now=NOW_10))
    assert res.valid is False
    assert res.has_unavailable is True
    by_hour = {c.timestamp.hour: c for c in res.checks}
    assert by_hour[14].status is TimestampStatus.USABLE
    assert by_hour[15].status is TimestampStatus.UNAVAILABLE  # in-horizon gap
    assert by_hour[15].status is not TimestampStatus.OUTSIDE_HORIZON
    assert "no usable spatial result" in by_hour[15].detail
    assert candidate_feasible(res) is False   # planner must not receive it
    assert sorted(calls) == [14, 15]          # both requested; the gap is live


def test_outside_horizon_is_rejected_before_any_request():
    fetch, calls = _fetch_with({22})         # 23:00 is beyond the window
    res = asyncio.run(resolve_thermal_interval(
        33.4484, -112.0740, _start(22), 90, fetch, now=NOW_10))  # -> [22, 23]
    assert res.valid is False
    by_hour = {c.timestamp.hour: c for c in res.checks}
    assert by_hour[22].status is TimestampStatus.USABLE   # boundary hour fine
    assert by_hour[23].status is TimestampStatus.OUTSIDE_HORIZON
    assert sorted(calls) == [22]             # outside hour was never requested


def test_extract_grid_maps_empty_spatial_result_to_unavailable():
    backend = Path(__file__).resolve().parents[1]
    empty = json.loads((backend / "fortyguard_raw_15_result.json").read_text())
    assert has_usable_spatial_data(empty) is False
    with pytest.raises(ThermalDataUnavailableError):
        _extract_grid(empty)
    full = json.loads((backend / "fortyguard_raw_result.json").read_text())
    assert has_usable_spatial_data(full) is True
    grid = _extract_grid(full)
    assert len(grid.temps_f) == 99
