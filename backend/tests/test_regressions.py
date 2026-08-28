"""Regression suite for the C2/C3/C4/C5, L1/L2/L3, D3 repair round.

All HTTP is mocked (or the grid fetcher is monkeypatched) - no network, no
credentials, no .env needed. Covers, per the spec's minimum list:

  1.  candidate-attributed error diagnostics
  2.  requested interval not polluted by alternative candidates
  3.  alternative search remains functional (supported request -> plans)
  4.  outside-horizon candidates are not unnecessarily generated
  5.  UNAVAILABLE != OUTSIDE_HORIZON
  6.  _start_local timezone normalization
  7.  average_temperature parsing (live Celsius)
  8.  ambiguous temperature field handling
  9.  polling deadline
  10. polling observability
  11. API key never appears in logs
  12. request-scoped exact-hour cache
  13. midnight rollover
  14. date rollover
  15. past requested time
  16. horizon boundary
  17. exact-hour identity
  (route actual-duration coverage is covered by test_route_sampling.py)
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Deterministic clock: Phoenix 2026-08-26 10:00 -> rolling horizon 10:00-22:00.
# These tests must not depend on the execution-time wall clock; the override
# is pinned HERE (identical to test_api.py) so this module is self-sufficient
# regardless of test-module import order.
os.environ.setdefault("HEATPLAN_NOW_OVERRIDE", "2026-08-26T17:00:00+00:00")

import httpx
import pytest

from app import fortyguard_client as fg
from app import planner
from app.fortyguard_client import (
    FORTYGUARD_STARTTIME_TZ,
    PHOENIX_TZ,
    HeatGrid,
    ThermalDataUnavailableError,
)
from app.models import ParsedIntent
from app.planner import (
    CandidateReport,
    IntervalUnsupportedError,
    SearchSummary,
    _candidate_shifts,
    _stationary_candidates,
    _start_local,
    generate_plans,
)
from app.thermal import (
    TimestampStatus,
    required_hourly_timestamps,
    resolve_thermal_interval,
)

PHX = ZoneInfo("America/Phoenix")
NOW10 = datetime(2026, 8, 26, 10, 0, tzinfo=PHX)          # horizon 10:00 -> 22:00


def _grid(temp_f: float = 100.0) -> HeatGrid:
    return HeatGrid(lats=[33.4484], lons=[-112.0740], temps_f=[temp_f],
                    source="fortyguard_live")


def _intent(start_time="14:00", date="2026-08-26", duration: int = 60,
            lat: float = 33.4484, lon: float = -112.0740,
            task_type: str = "stationary") -> ParsedIntent:
    return ParsedIntent(
        task="test", task_type=task_type, duration_minutes=duration,
        start_time=start_time, date=date, location_name="Phoenix, AZ",
        lat=lat, lon=lon, heat_priority="medium")


def _serve(fetch, start: datetime, duration: int, now=NOW10):
    return asyncio.run(resolve_thermal_interval(
        33.4484, -112.0740, start, duration, fetch, now=now))


def _fetch_serving(served: set[int]):
    """Fetch serving a grid only for hours in `served`; others -> unavailable."""
    async def fetch(lat, lon, ts):
        if ts.hour in served:
            return _grid()
        raise ThermalDataUnavailableError(
            f"{ts:%H:%M} -> map_data.features == [] / n_cells == 0")
    return fetch


# ---------------------------------------------------------------------------
# C3 - candidate-attributed diagnostics / message pollution
# ---------------------------------------------------------------------------
def test_error_message_uses_requested_interval_only():
    requested = _serve(_fetch_serving({14}), datetime(2026, 8, 26, 14, 0, tzinfo=PHX), 90)
    assert requested.has_unavailable and not requested.has_outside_horizon
    # Alternative candidate far outside the horizon (23:00 -> [23:00, 00:00]
    # both OUTSIDE_HORIZON) must NEVER leak into the user-facing message.
    alt = CandidateReport(
        start_time="23:00", shift=9,
        result=_serve(_fetch_serving({23}), datetime(2026, 8, 26, 23, 0, tzinfo=PHX), 90))
    err = IntervalUnsupportedError(
        requested_result=requested,
        candidate_reports=[alt],
        search_summary=SearchSummary(requested_start=requested.required[0],
                                     requested_duration_minutes=90))
    msg = str(err)
    assert "Requested thermal interval: 14:00, 15:00" in msg
    assert "Unavailable live thermal data: 15:00" in msg
    assert "23:00" not in msg and "00:00" not in msg
    assert "Outside current forecast horizon" not in msg      # requested has none
    assert "No missing thermal data is fabricated" in msg
    assert err.code == "THERMAL_INTERVAL_UNSUPPORTED"
    # candidate reports stay attached for logs/debug/tests
    assert len(err.candidate_reports) == 1
    assert err.candidate_reports[0].shift == 9
    assert err.candidate_reports[0].result is alt.result


def test_requested_outside_horizon_stays_distinct_from_unavailable():
    requested = _serve(_fetch_serving({22}), datetime(2026, 8, 26, 22, 0, tzinfo=PHX), 90)
    err = IntervalUnsupportedError(requested_result=requested)
    msg = str(err)
    assert "Requested thermal interval: 22:00, 23:00" in msg
    assert "Outside current forecast horizon: 23:00" in msg
    assert "Unavailable" not in msg
    by_ts = {c.timestamp.strftime("%H:%M"): c.status for c in requested.checks}
    assert by_ts["22:00"] is TimestampStatus.USABLE
    assert by_ts["23:00"] is TimestampStatus.OUTSIDE_HORIZON


def test_no_feasible_alternative_when_requested_is_supported():
    requested = _serve(_fetch_serving({14, 15}), datetime(2026, 8, 26, 14, 0, tzinfo=PHX), 90)
    assert requested.valid
    err = IntervalUnsupportedError(requested_result=requested,
                                   alternative_none_found=True)
    assert err.code == "NO_FEASIBLE_ALTERNATIVE"
    assert "Live thermal data supports the requested interval" in str(err)
    assert "no alternative plan could be validated" in str(err)
    pub = err.requested_interval_public
    assert pub["required"] == ["14:00", "15:00"]
    assert all(c["status"] == "usable" for c in pub["checks"])


def test_requested_interval_public_shape():
    requested = _serve(_fetch_serving({14}), datetime(2026, 8, 26, 14, 0, tzinfo=PHX), 90)
    err = IntervalUnsupportedError(requested_result=requested)
    pub = err.requested_interval_public
    assert [c["timestamp"] for c in pub["checks"]] == ["14:00", "15:00"]
    assert [c["status"] for c in pub["checks"]] == ["usable", "unavailable"]
    assert pub["horizon_end"]  # rolling horizon exposed for explainability
    diag = err.diagnostics_public
    assert diag["alternative_candidates"] == 0
    assert diag["search_summary"] == {"candidate_shifts": [], "feasible_shifts": []}


# ---------------------------------------------------------------------------
# C4 - candidate search bounded by the real horizon
# ---------------------------------------------------------------------------
def test_candidate_shifts_bounded_by_horizon_not_hardcoded():
    base = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)
    # horizon 10:00 -> 22:00; 90 min -> last_start = 22:00+1h-90m = 21:30
    shifts = _candidate_shifts(base, 90, now=NOW10)
    assert shifts == list(range(0, 8))          # 14:00..21:00, derived
    assert len(shifts) < 12                     # never a blind 12
    assert 8 not in shifts                      # 22:00 start would end at 23:00
    assert max(shifts) == 7                     # 21:00 -> [21:00, 22:00] boundary ok


def test_candidate_count_varies_with_duration():
    base = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)
    s90 = _candidate_shifts(base, 90, now=NOW10)
    s60 = _candidate_shifts(base, 60, now=NOW10)
    assert len(s60) != len(s90)                 # horizon-derived, not constant
    assert len(s90) == 8 and len(s60) == 9


def test_alternative_search_still_functional(monkeypatch):
    """A supported request still yields 3 deterministic plans (demo case)."""
    async def fake_grid(lat, lon, date, hour):
        return fg._mock_grid(lat, lon, hour)
    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    plans, scores, rec = asyncio.run(
        generate_plans(_intent(start_time="14:00", duration=90)))
    assert len(plans) == 3
    assert {p.id for p in plans} == {"A", "B", "C"}
    assert rec in scores


# ---------------------------------------------------------------------------
# C2 / D3 - timezone normalization
# ---------------------------------------------------------------------------
def test_start_local_naive_date_is_phoenix_local():
    dt = _start_local(_intent(start_time="09:30", date="2026-08-26"))
    assert dt.tzinfo is PHX
    assert (dt.hour, dt.minute) == (9, 30)
    assert dt.utcoffset().total_seconds() == -7 * 3600
    assert dt == datetime(2026, 8, 26, 9, 30, tzinfo=PHX)


def test_start_local_aware_datetime_converts_then_applies_clock_fields():
    # 2026-08-26T14:00Z == 07:00 Phoenix; the intended local clock fields
    # (intent.start_time = 09:30) are applied AFTER astimezone(PHOENIX_TZ).
    dt = _start_local(_intent(start_time="09:30", date="2026-08-26T14:00:00+00:00"))
    assert (dt.hour, dt.minute) == (9, 30)
    assert dt.tzinfo is PHX


def test_start_local_independent_of_machine_timezone(monkeypatch):
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    import time as _time
    _time.tzset()
    try:
        dt = _start_local(_intent(start_time="14:00", date="2026-08-26"))
        assert dt.hour == 14 and dt.tzinfo is PHX
        assert dt.utcoffset().total_seconds() == -7 * 3600
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        _time.tzset()


def test_submit_payload_is_exact_phoenix_date_time(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content
        return httpx.Response(200, json={"data": {"activity_id": "act-tz"}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fg._submit_heatmap(client, 33.44, -112.07, "2026-08-26", 14)

    asyncio.run(run())
    body = json.loads(captured["json"])
    dt = body["date_time"]
    assert dt["start_date"] == "2026-08-26"
    assert dt["start_time"] == "14:00"
    assert dt["filter_type"] == 1
    # documented integration assumption stays explicit
    assert FORTYGUARD_STARTTIME_TZ == "local_to_aoi"


# ---------------------------------------------------------------------------
# C5 - temperature field contract
# ---------------------------------------------------------------------------
def _tile(lon1, lat1, lon2, lat2, props):
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [[
            [lon1, lat1], [lon2, lat1], [lon2, lat2], [lon1, lat2], [lon1, lat1],
        ]]},
    }


def _live_payload(field: str, value: float) -> dict:
    return {"map_data": {"type": "FeatureCollection", "features": [
        _tile(-112.071, 33.439, -112.069, 33.441, {field: value}),
        _tile(-112.061, 33.449, -112.059, 33.451, {field: value - 1.0}),
    ]}}


def test_extract_grid_documented_average_temperature_is_celsius():
    grid = fg._extract_grid(_live_payload("average_temperature", 40.5556))
    assert grid.source == "fortyguard_live"
    assert len(grid.temps_f) == 2
    assert abs(grid.nearest_temp(33.44, -112.07) - 105.0) < 0.01   # C -> F


def test_extract_grid_ambiguous_temperature_never_celsius_on_live_path():
    # "temperature" alone is NOT the documented field: it must not silently
    # become a Celsius source; no usable tiles -> unavailable, never 105 F.
    with pytest.raises(ThermalDataUnavailableError):
        fg._extract_grid(_live_payload("temperature", 21.0))


def test_legacy_fahrenheit_fixture_still_parsed_offline():
    # Legacy/offline shape: top-level FeatureCollection (no map_data); the
    # generic field is a legacy FAHRENHEIT value, kept as-is, never C->F.
    legacy = {"type": "FeatureCollection", "features": [
        _tile(-112.071, 33.439, -112.069, 33.441, {"temperature": 105.0}),
    ]}
    grid = fg._extract_grid(legacy)
    assert grid.temps_f == [105.0]


# ---------------------------------------------------------------------------
# L1 / L2 - polling deadline & observability
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_polling_stops_when_deadline_expired(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": {"status": "Processing"}})

    monkeypatch.setattr(fg.settings, "poll_interval_s", 0.05)
    monkeypatch.setattr(fg.settings, "poll_timeout_s", 0.02)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(fg.FortyGuardError, match="timed out"):
            await fg._poll_status(client, "act-deadline")
    # The deadline is checked BEFORE every iteration's HTTP request: after it
    # expires no new polling request is issued (at most the first one).
    assert calls["n"] <= 1


@pytest.mark.anyio
async def test_http_timeout_capped_to_remaining(monkeypatch):
    recorded: list = []

    class FakeClient:
        async def get(self, url, headers=None, timeout=None):
            recorded.append(timeout)
            return httpx.Response(200, json={"data": {"status": "Processing"}})

    monkeypatch.setattr(fg.settings, "poll_timeout_s", 0.08)
    monkeypatch.setattr(fg.settings, "poll_interval_s", 0.02)
    with pytest.raises(fg.FortyGuardError, match="timed out"):
        await fg._poll_status(FakeClient(), "act-cap")
    assert recorded and all(0 < t <= 30 for t in recorded)


def _completed_result() -> dict:
    return _live_payload("average_temperature", 40.5556)


def test_polling_logs_observability_and_never_the_api_key(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="heatplan.fortyguard")
    script = [
        (200, {"data": {"activity_id": "act-obs"}}),
        (200, {"data": {"status": "Processing"}}),
        (200, {"data": {"status": "Completed", "result": _completed_result()}}),
    ]
    transport, _ = _fake_transport(script)
    monkeypatch.setattr(fg.settings, "poll_interval_s", 0.01)
    monkeypatch.setattr(fg.settings, "fortyguard_api_key", "super-secret-key-123")

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            aid = await fg._submit_heatmap(client, 33.44, -112.07, "2026-08-26", 14)
            await fg._poll_status(client, aid)

    asyncio.run(run())
    records = [r.getMessage() for r in caplog.records
               if r.name == "heatplan.fortyguard"]
    assert any("activity_id=act-obs" in r for r in records)
    assert any("submitted_date=2026-08-26" in r and "submitted_time=14:00" in r
               for r in records)
    completed = [r for r in records if "terminal=completed" in r]
    assert completed
    assert "poll=2" in completed[0] and "status=completed" in completed[0]
    assert "elapsed_s=" in completed[0]
    # L2 hard rule: the API key never appears in any log line.
    assert all("super-secret-key-123" not in r for r in records)
    assert all("api-key" not in r.lower() or "activity_id" in r for r in records)


def _fake_transport(script: list):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        code, body = script[i]
        return httpx.Response(code, json=body)

    return httpx.MockTransport(handler), calls


# ---------------------------------------------------------------------------
# L3 - request-scoped exact-hour thermal cache
# ---------------------------------------------------------------------------
def test_request_scoped_cache_reuses_exact_hour(monkeypatch):
    calls: list[tuple[str, int]] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append((date, hour))
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    cands, requested, reports, summary = asyncio.run(
        _stationary_candidates(_intent(start_time="14:00", duration=90), now=NOW10))
    # hours 14..22 are required by SOME candidate; each (date, hour) may only
    # be submitted ONCE per request - the request-scoped cache dedupes.
    assert sorted({h for _, h in calls}) == list(range(14, 23))
    assert len(calls) == len({c for c in calls})       # no duplicate submission
    assert requested.valid and cands


# ---------------------------------------------------------------------------
# TIME SEMANTICS - rollover, boundary, past, exact-hour identity
# ---------------------------------------------------------------------------
def test_midnight_rollover():
    now = datetime(2026, 8, 26, 22, 0, tzinfo=PHX)     # horizon 22:00 -> (27)10:00
    served = {(8, 26, 22), (8, 26, 23), (8, 27, 0)}

    async def fetch(lat, lon, ts):
        if (ts.month, ts.day, ts.hour) in served:
            return _grid()
        raise ThermalDataUnavailableError("empty")

    start = datetime(2026, 8, 26, 22, 0, tzinfo=PHX)
    res = _serve(fetch, start, 180, now=now)
    assert [t.strftime("%H:%M") for t in res.required] == ["22:00", "23:00", "00:00"]
    assert all(c.status is TimestampStatus.USABLE for c in res.checks)
    assert required_hourly_timestamps(start, 180)[-1].day == 27   # date rolled


def test_date_rollover():
    now = datetime(2026, 8, 26, 20, 0, tzinfo=PHX)     # horizon 20:00 -> (27)08:00
    served = {(8, 26, 23), (8, 27, 0)}

    async def fetch(lat, lon, ts):
        if (ts.month, ts.day, ts.hour) in served:
            return _grid()
        raise ThermalDataUnavailableError("empty")

    start = datetime(2026, 8, 26, 23, 30, tzinfo=PHX)  # 23:30 + 90m -> 01:00
    res = _serve(fetch, start, 90, now=now)
    assert [(t.month, t.day, t.hour) for t in res.required] == [(8, 26, 23), (8, 27, 0)]
    assert res.valid


def test_past_requested_time_is_outside_horizon():
    calls: list[int] = []

    async def fetch(lat, lon, ts):
        calls.append(ts.hour)
        return _grid()

    start = datetime(2026, 8, 26, 9, 0, tzinfo=PHX)    # before horizon 10:00
    res = _serve(fetch, start, 60, now=NOW10)
    by_ts = {c.timestamp.strftime("%H:%M"): c.status for c in res.checks}
    assert by_ts["09:00"] is TimestampStatus.OUTSIDE_HORIZON
    assert calls == []                                 # never requested


def test_horizon_boundary_inclusive_for_last_hour():
    served_ok: list[int] = []

    async def fetch(lat, lon, ts):
        served_ok.append(ts.hour)
        return _grid()

    start = datetime(2026, 8, 26, 22, 0, tzinfo=PHX)   # == horizon_end, allowed
    res = _serve(fetch, start, 60, now=NOW10)
    assert [c.status for c in res.checks] == [TimestampStatus.USABLE]
    assert served_ok == [22]


def test_exact_hour_identity_in_cache_keys():
    h14 = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)
    h15 = datetime(2026, 8, 26, 15, 0, tzinfo=PHX)
    h14_next = datetime(2026, 8, 27, 14, 0, tzinfo=PHX)
    k14 = planner._request_cache_key(33.4484, -112.0740, h14)
    k15 = planner._request_cache_key(33.4484, -112.0740, h15)
    k14b = planner._request_cache_key(33.4484, -112.0740, h14_next)
    assert len({k14, k15, k14b}) == 3                  # hour and date identity
    # required-hour semantics (half-open interval, endpoint not auto-added)
    assert [t.hour for t in required_hourly_timestamps(h14, 120)] == [14, 15]
    assert [t.hour for t in required_hourly_timestamps(h14, 61)] == [14, 15]


# ---------------------------------------------------------------------------
# Full-flow: API-level semantics (requested vs alternatives)
# ---------------------------------------------------------------------------
def test_full_flow_requested_interval_error_no_pollution(monkeypatch):
    calls: list[int] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append(hour)
        if hour == 15:
            raise ThermalDataUnavailableError("empty")
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    with pytest.raises(IntervalUnsupportedError) as ei:
        asyncio.run(generate_plans(_intent(start_time="14:00", duration=90)))
    e = ei.value
    assert e.code == "THERMAL_INTERVAL_UNSUPPORTED"
    msg = str(e)
    assert "Requested thermal interval: 14:00, 15:00" in msg
    assert "Unavailable live thermal data: 15:00" in msg
    assert "Outside" not in msg
    # Alternatives were NOT searched once the requested interval failed.
    assert e.candidate_reports == []
    assert 14 in calls and 15 in calls


def test_full_flow_supported_request_but_no_alternative(monkeypatch):
    async def fake_grid(lat, lon, date, hour):
        if hour >= 16:
            raise ThermalDataUnavailableError("empty")
        return fg._mock_grid(lat, lon, hour)

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    with pytest.raises(IntervalUnsupportedError) as ei:
        asyncio.run(generate_plans(_intent(start_time="14:00", duration=90)))
    e = ei.value
    assert e.code == "NO_FEASIBLE_ALTERNATIVE"
    assert "supports the requested interval" in str(e)
    assert "no alternative plan could be validated" in str(e)
    # the requested interval itself is fine; alternatives (hour >= 16) failed
    assert e.requested_result.valid
    assert len(e.candidate_reports) > 0
