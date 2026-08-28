"""Structured-input architecture regression tests (spec §21 A-F).

All thermal fetching is mocked or run with FORTYGUARD_MOCK=true: no network,
no credentials, no .env. The legacy text endpoint and its regression suite
remain untouched; these tests pin the NEW deterministic structured path.

Covered:
  A. Structured request validation (valid, zero/negative duration,
     duration > window, end before start, same start/end)
  B. Date handling (valid, invalid, destination-timezone correctness)
  C. Forecast horizon (fully inside, exactly at boundary, partially outside,
     completely outside) - gated BEFORE any FortyGuard request
  D. Candidate generation (60 min / 120 min in a 4h window, duration ==
     window, duration > window)
  F. Thermal request deduplication (unique hours only; different location /
     hour / request scope never share a result)
"""
import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# NOTE: the clock is pinned PER TEST by the autouse fixture below (never at
# module import time) so this module cannot leak its clock into other test
# modules - pytest imports all modules before running any test.

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    """Deterministic clock: Phoenix 2026-08-27 10:00 -> horizon 10:00..22:00.

    monkeypatch restores the previous environment after every test, so the
    legacy suites keep their own pinned clock untouched.
    """
    monkeypatch.setenv("FORTYGUARD_MOCK", "true")
    monkeypatch.setenv("HEATPLAN_NOW_OVERRIDE", "2026-08-27T17:00:00+00:00")
    # The settings object captured FORTYGUARD_MOCK at import; force mock mode
    # for this suite regardless of import order.
    from app.config import settings
    monkeypatch.setattr(settings, "fortyguard_mock", "true")
    yield

from app import fortyguard_client as fg  # noqa: E402
from app import planner  # noqa: E402
from app import structured  # noqa: E402
from app.location import location_resolver  # noqa: E402
from app.models import StructuredLocation, StructuredPlanRequest  # noqa: E402
from app.structured import (  # noqa: E402
    ForecastHorizonExceededError,
    StructuredInputError,
    check_forecast_horizon,
    generate_candidate_starts,
    resolve_window,
    run_structured_plan,
)

PHX = ZoneInfo("America/Phoenix")


def _req(**kw) -> StructuredPlanRequest:
    base = dict(
        date="2026-08-27",
        duration_minutes=60,
        available_start="13:00",
        available_end="17:00",
        location=StructuredLocation(
            name="Phoenix, AZ", latitude=33.4484, longitude=-112.0740),
    )
    base.update(kw)
    return StructuredPlanRequest(**base)


def _window(req: StructuredPlanRequest):
    return resolve_window(req, location_resolver.resolve("Phoenix"))


# ---------------------------------------------------------------------------
# A. Structured request validation
# ---------------------------------------------------------------------------
def test_valid_request_resolves():
    w = _window(_req())
    assert w.duration_minutes == 60
    assert w.window_start == datetime(2026, 8, 27, 13, 0, tzinfo=PHX)
    assert w.window_end == datetime(2026, 8, 27, 17, 0, tzinfo=PHX)
    assert len(w.candidate_starts) == 4


def test_duration_greater_than_window_rejected():
    with pytest.raises(StructuredInputError) as ei:
        _window(_req(duration_minutes=300))
    assert ei.value.code == "INVALID_TIME_WINDOW"


def test_end_before_start_rejected():
    with pytest.raises(StructuredInputError) as ei:
        _window(_req(available_start="17:00", available_end="13:00"))
    assert ei.value.code == "INVALID_TIME_WINDOW"


def test_same_start_and_end_rejected():
    with pytest.raises(StructuredInputError) as ei:
        _window(_req(available_start="13:00", available_end="13:00"))
    assert ei.value.code == "INVALID_TIME_WINDOW"


def test_negative_duration_rejected_by_schema():
    with pytest.raises(Exception):
        _req(duration_minutes=-5)  # pydantic gt=0


def test_zero_duration_rejected_by_schema():
    with pytest.raises(Exception):
        _req(duration_minutes=0)


def test_invalid_duration_type_rejected():
    with pytest.raises(Exception):
        _req(duration_minutes="sixty")


# ---------------------------------------------------------------------------
# B. Date handling + destination timezone correctness
# ---------------------------------------------------------------------------
def test_valid_future_date_accepted():
    w = _window(_req(date="2026-08-28"))
    assert w.date == "2026-08-28"
    assert w.window_start.day == 28


def test_invalid_date_rejected():
    for bad in ("2026-13-40", "not-a-date", "08/27/2026", "2026-8-7x"):
        with pytest.raises(StructuredInputError) as ei:
            _window(_req(date=bad))
        assert ei.value.code == "INVALID_DATE"


def test_window_uses_destination_timezone_not_machine_tz(monkeypatch):
    """The same structured request must resolve identically regardless of
    the developer machine's timezone (C2 semantics preserved)."""
    import time as _time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    _time.tzset()
    try:
        w = _window(_req())
        assert w.window_start.tzinfo is PHX
        assert w.window_start.utcoffset().total_seconds() == -7 * 3600
        assert (w.window_start.hour, w.window_start.minute) == (13, 0)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        _time.tzset()


def test_destination_timezone_applies_per_location():
    """Discovery Park (Sacramento) resolves to Pacific time; the same wall
    clock fields mean a different UTC instant than Phoenix's would."""
    loc = location_resolver.resolve("Discovery Park")
    w = resolve_window(_req(), loc)
    assert w.window_start.tzinfo == ZoneInfo("America/Los_Angeles")
    assert w.window_start.hour == 13


# ---------------------------------------------------------------------------
# C. Forecast horizon (gated before ANY FortyGuard request)
# ---------------------------------------------------------------------------
def test_window_fully_inside_horizon_passes():
    # horizon 10:00..22:00; window 13:00-17:00, 60 min -> hours 13..16.
    check_forecast_horizon(_window(_req()))  # must not raise


def test_window_exactly_at_boundary_passes():
    # boundary is INCLUSIVE (thermal.py documented rule): a 22:00 start with
    # 60 min requires only hour 22 == horizon_end -> allowed.
    w = _window(_req(available_start="22:00", available_end="23:00"))
    check_forecast_horizon(w)


def test_window_partially_outside_horizon_rejected_before_any_request(monkeypatch):
    """22:30 + 60 min traverses hours 22 AND 23; 23:00 is beyond the
    horizon. The FIRST unavailable hour is identified and NOTHING is
    submitted to FortyGuard."""
    calls: list = []

    async def fake_grid(lat, lon, date, hour):
        calls.append(hour)
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(structured._planner, "get_heat_grid", fake_grid)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    w = _window(_req(available_start="22:30", available_end="23:30"))
    with pytest.raises(ForecastHorizonExceededError) as ei:
        check_forecast_horizon(w)
    assert ei.value.code == "FORECAST_HORIZON_EXCEEDED"
    assert ei.value.first_unavailable.hour == 23
    assert "22:00" in ei.value.extra["horizon_end"]
    assert calls == []  # no partial submission


def test_window_completely_outside_horizon_rejected():
    w = _window(_req(date="2026-08-28", available_start="13:00",
                     available_end="17:00"))  # tomorrow -> beyond +12h
    with pytest.raises(ForecastHorizonExceededError):
        check_forecast_horizon(w)


def test_horizon_error_is_not_relabeled_thermal_interval_unsupported():
    """Spec §6: FORECAST_HORIZON_EXCEEDED must NOT become
    THERMAL_INTERVAL_UNSUPPORTED - the actual reason is horizon coverage."""
    w = _window(_req(date="2026-08-28", available_start="13:00",
                     available_end="17:00"))
    with pytest.raises(ForecastHorizonExceededError) as ei:
        check_forecast_horizon(w)
    assert ei.value.code != "THERMAL_INTERVAL_UNSUPPORTED"


# ---------------------------------------------------------------------------
# D. Candidate generation (deterministic sliding window)
# ---------------------------------------------------------------------------
def test_candidates_60min_in_4h_window():
    w = _window(_req(duration_minutes=60))
    assert [c.strftime("%H:%M") for c in w.candidate_starts] == [
        "13:00", "14:00", "15:00", "16:00"]


def test_candidates_120min_in_4h_window():
    w = _window(_req(duration_minutes=120))
    assert [c.strftime("%H:%M") for c in w.candidate_starts] == [
        "13:00", "14:00", "15:00"]


def test_duration_equal_to_window_yields_one_candidate():
    w = _window(_req(duration_minutes=240))
    assert [c.strftime("%H:%M") for c in w.candidate_starts] == ["13:00"]


def test_no_candidate_exceeds_the_user_window():
    w = _window(_req(duration_minutes=90, available_start="13:00",
                     available_end="16:00"))
    for s in w.candidate_starts:
        assert s + __import__("datetime").timedelta(minutes=90) <= w.window_end
    assert [c.strftime("%H:%M") for c in w.candidate_starts] == ["13:00", "14:00"]


def test_generate_candidate_starts_pure():
    start = datetime(2026, 8, 27, 13, 0, tzinfo=PHX)
    end = datetime(2026, 8, 27, 17, 0, tzinfo=PHX)
    assert generate_candidate_starts(start, end, 60)[-1].hour == 16
    assert generate_candidate_starts(start, end, 300) == []


# ---------------------------------------------------------------------------
# F. Thermal request deduplication
# ---------------------------------------------------------------------------
def test_unique_hours_fetched_once_per_request(monkeypatch):
    """Candidates 13->14..16->17 (60 min) need hours 13,14,15,16 exactly
    ONCE each - 4 thermal fetches, never 4 per candidate."""
    calls: list[tuple[str, int]] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append((date, hour))
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(structured._planner, "get_heat_grid", fake_grid)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    result = asyncio.run(run_structured_plan(_req(duration_minutes=60)))
    assert len(calls) == 4
    assert sorted(h for _, h in calls) == [13, 14, 15, 16]
    assert len(calls) == len(set(calls))  # no duplicate submission
    assert result["best"] is not None


def test_different_location_is_a_different_request(monkeypatch):
    calls: list[tuple[float, float, int]] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append((lat, lon, hour))
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(structured._planner, "get_heat_grid", fake_grid)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    asyncio.run(run_structured_plan(_req()))  # Phoenix
    asyncio.run(run_structured_plan(_req(location=StructuredLocation(
        name="Discovery Park", latitude=38.6126, longitude=-121.5188))))
    locs = {(round(la, 4), round(lo, 4)) for la, lo, _ in calls}
    assert (33.4484, -112.074) in locs and (38.6126, -121.5188) in locs


def test_request_scoped_cache_does_not_leak_between_requests(monkeypatch):
    """Each run_structured_plan call gets its own request cache; a second
    request re-fetches (the shared request-scoped cache is per-request)."""
    calls: list[int] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append(hour)
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(structured._planner, "get_heat_grid", fake_grid)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    asyncio.run(run_structured_plan(_req()))
    first = len(calls)
    asyncio.run(run_structured_plan(_req()))
    assert len(calls) == 2 * first  # request scope: no cross-request reuse


def test_unavailable_hour_makes_candidate_infeasible_not_fabricated(monkeypatch):
    """Hour 14 returns features == [] (UNAVAILABLE). Candidates needing 14
    are infeasible; no data is borrowed from hour 13 or 15."""
    from app.fortyguard_client import ThermalDataUnavailableError

    async def fake_grid(lat, lon, date, hour):
        if hour == 14:
            raise ThermalDataUnavailableError("features == []")
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(structured._planner, "get_heat_grid", fake_grid)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    result = asyncio.run(run_structured_plan(_req(duration_minutes=120)))
    # 120-min candidates: 13->15 (needs 13,14) and 14->16 (needs 14,15) are
    # infeasible; only 15->17 (needs 15,16) survives.
    ok = [c for c in result["candidates"] if c.thermal_status == "ok"]
    assert [c.start_time for c in ok] == ["15:00"]
    assert result["best"].start_time == "15:00"
    unavailable = [c["timestamp"] for c in result["hour_checks"]
                   if c["status"] == "unavailable"]
    assert unavailable == ["14:00"]


def test_all_hours_unavailable_yields_no_feasible_alternative(monkeypatch):
    from app.fortyguard_client import ThermalDataUnavailableError

    async def fake_grid(lat, lon, date, hour):
        raise ThermalDataUnavailableError("features == []")

    monkeypatch.setattr(structured._planner, "get_heat_grid", fake_grid)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)

    with pytest.raises(StructuredInputError) as ei:
        asyncio.run(run_structured_plan(_req()))
    assert ei.value.code == "NO_FEASIBLE_ALTERNATIVE"
    assert "No missing thermal data is fabricated" in str(ei.value)


# ---------------------------------------------------------------------------
# Ranking is deterministic (lowest exposure, earliest tie-break)
# ---------------------------------------------------------------------------
def test_ranking_deterministic_lowest_exposure_wins():
    cands = [
        structured.StructuredCandidate("13:00", "14:00",
                                       datetime(2026, 8, 27, 13, tzinfo=PHX),
                                       60, "ok", 100.0, 500.0),
        structured.StructuredCandidate("14:00", "15:00",
                                       datetime(2026, 8, 27, 14, tzinfo=PHX),
                                       60, "ok", 95.0, 400.0),
        structured.StructuredCandidate("15:00", "16:00",
                                       datetime(2026, 8, 27, 15, tzinfo=PHX),
                                       60, "ok", 95.0, 400.0),
    ]
    ranked = structured.rank_candidates(cands)
    assert ranked[0].start_time == "14:00"  # lowest exposure, earliest tie
    assert [c.start_time for c in ranked] == ["14:00", "15:00", "13:00"]


def test_infeasible_candidates_never_ranked():
    cands = [
        structured.StructuredCandidate("13:00", "14:00",
                                       datetime(2026, 8, 27, 13, tzinfo=PHX),
                                       60, "unavailable_hours"),
        structured.StructuredCandidate("14:00", "15:00",
                                       datetime(2026, 8, 27, 14, tzinfo=PHX),
                                       60, "ok", 95.0, 400.0),
    ]
    ranked = structured.rank_candidates(cands)
    assert len(ranked) == 1 and ranked[0].start_time == "14:00"
