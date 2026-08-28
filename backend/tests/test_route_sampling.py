"""P0 regression tests: route sampling geometry, duration-synchronized
thermal validation, and the removal of wrong-hour grid fallbacks.

P0-2  A -> waypoint -> B must be sampled along the waypoint geometry, never
      along the straight A -> B chord.
P0-3  A candidate whose ACTUAL route duration exceeds the requested duration
      must be validated against the thermal timeline of the ACTUAL duration;
      if that coverage is missing the candidate is INFEASIBLE - exposure is
      never computed from partial thermal coverage.
P0-4  A thermal sample at hour H uses the grid of hour H or the candidate is
      infeasible. The start-hour / previous-hour grid is never substituted.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.exposure import exposure
from app.fortyguard_client import HeatGrid, ThermalDataUnavailableError
from app.models import ParsedIntent
from app import planner
from app.planner import (
    IntervalUnsupportedError,
    _grid_for_hour,
    _path_m,
    _sample_path,
    _route_candidates,
)

PHX = ZoneInfo("America/Phoenix")
START = datetime(2026, 8, 26, 14, 0, tzinfo=PHX)  # 14:00 Phoenix
NOW_10 = datetime(2026, 8, 26, 10, 0, tzinfo=PHX)  # horizon 10:00 -> 22:00

# Short local geometry for the pure sampling tests.
A = [33.4000, -112.0000]
B = [33.4200, -112.0000]
C = [33.4100, -111.9900]          # waypoint, ~1 km east of the chord


class FieldGrid(HeatGrid):
    """Analytic thermal field: temperature is a deterministic function of
    (lat, lon), so every sample point has an exact expected value and tests
    do not depend on nearest-neighbour tie-breaking of sparse fixtures."""

    def __init__(self, fn):
        super().__init__(lats=[0.0], lons=[0.0], temps_f=[100.0],
                         source="fortyguard_live")
        self._fn = fn

    def nearest_temp(self, lat: float, lon: float) -> float:
        return self._fn(lat, lon)


def _chord_vs_waypoint_field(lat: float, lon: float) -> float:
    """50 F in a narrow band around the MIDDLE of the direct chord (the
    endpoints A/B lie outside it), 115 F near the waypoint longitude
    (-111.990), 100 F elsewhere."""
    if abs(lon - (-112.000)) < 0.0003 and 33.405 < lat < 33.415:
        return 50.0
    if abs(lon - (-111.990)) < 0.0005:
        return 115.0
    return 100.0


def _two_waypoint_field(lat: float, lon: float) -> float:
    """50 F in the MIDDLE of the chord; the east corridor (waypoints at lon
    -111.995) is 70 F in its southern half and 80 F in its northern half."""
    if abs(lon - (-112.000)) < 0.0003 and 33.405 < lat < 33.415:
        return 50.0
    if abs(lon - (-111.995)) < 0.0005:
        return 70.0 if lat < 33.410 else 80.0
    return 100.0


def _marker_grid(temps_at: dict[tuple[float, float], float]) -> HeatGrid:
    """Sparse grid of exact points (used where nearest-neighbour is enough)."""
    return HeatGrid(lats=[p[0] for p in temps_at],
                    lons=[p[1] for p in temps_at],
                    temps_f=list(temps_at.values()),
                    source="fortyguard_live")


# --------------------------------------------------------------------------
# P0-2: waypoint geometry is actually traversed
# --------------------------------------------------------------------------
def test_waypoint_route_samples_pass_through_waypoint_not_chord():
    """A -> C -> B must sample through C. The chord carries a 50 F band the
    detour never touches (except infinitesimally near A/B); the waypoint
    carries a 115 F band the chord never touches. If the sampler collapsed
    A -> C -> B onto A -> B the detour would be all ~50 F."""
    grids = {START: FieldGrid(_chord_vs_waypoint_field)}
    direct = _sample_path([A, B], START, 30.0, grids)
    detour = _sample_path([A, C, B], START, 45.0, grids)

    direct_temps = [t for _, t, _ in direct]
    detour_temps = [t for _, t, _ in detour]

    # Direct route: sampled along the chord -> passes through the 50 F band
    # and never touches the waypoint's 115 F band.
    assert 50.0 in direct_temps
    assert 115.0 not in direct_temps
    # Detour: samples actually pass through the waypoint's 115 F band...
    assert 115.0 in detour_temps
    # ...and never sample the chord's cool band (the detour is not on it).
    assert 50.0 not in detour_temps
    # Same endpoints, different sampling geometry -> different exposure.
    assert exposure([(t, d) for _, t, d in detour]) != exposure(
        [(t, d) for _, t, d in direct])


def test_two_waypoints_traversed_in_order():
    """A -> C1 -> C2 -> B: the middle segment C1 -> C2 runs due north along
    lon -111.995, crossing the field's 70 F -> 80 F boundary at lat 33.410.
    Both waypoint corridor temperatures must appear; the chord's 50 F band
    must not."""
    c1 = [33.4050, -111.9950]
    c2 = [33.4150, -111.9950]
    grids = {START: FieldGrid(_two_waypoint_field)}
    samples = _sample_path([A, c1, c2, B], START, 60.0, grids)
    temps = [t for _, t, _ in samples]
    assert 70.0 in temps and 80.0 in temps   # both corridor halves sampled
    assert 50.0 not in temps                 # chord band never sampled
    # Southern (70 F) samples precede northern (80 F) samples: order kept.
    assert max(i for i, (_, t, _) in enumerate(samples) if t == 70.0) < \
           min(i for i, (_, t, _) in enumerate(samples) if t == 80.0)


def test_total_distance_and_duration_from_every_segment():
    """Distance/duration of the waypoint route come from A->C plus C->B,
    which must exceed the straight A->B chord."""
    direct_d = _path_m([A, B])
    detour_d = _path_m([A, C, B])
    assert detour_d > direct_d
    expected = planner._haversine_m(*A, *C) + planner._haversine_m(*C, *B)
    assert abs(detour_d - expected) < 1e-6
    grids = {START: FieldGrid(_chord_vs_waypoint_field)}
    duration = detour_d / planner.WALK_SPEED_M_S / 60.0
    detour = _sample_path([A, C, B], START, duration, grids)
    # Sample durations cover the COMPLETE route duration, no more, no less.
    assert abs(sum(d for _, _, d in detour) - duration) < 1e-6


# --------------------------------------------------------------------------
# P0-4: no wrong-hour grid fallback
# --------------------------------------------------------------------------
def test_grid_for_hour_never_substitutes_another_hour():
    h14 = START
    h15 = datetime(2026, 8, 26, 15, 0, tzinfo=PHX)
    grids = {h14: _marker_grid({(A[0], A[1]): 90.0})}  # only hour 14 exists
    assert _grid_for_hour(grids, h14).nearest_temp(*A) == 90.0
    with pytest.raises(KeyError):
        _grid_for_hour(grids, h15)  # missing hour: error, never h14's grid


def test_sample_path_missing_later_hour_raises_instead_of_reusing_start_hour():
    """A walk starting 14:00 whose ACTUAL duration crosses into 15:00 must
    fail (candidate infeasible) when the 15:00 grid is missing - the 14:00
    grid is never reused for a 15:00 sample."""
    far = [33.5000, -112.0000]  # ~11 km north -> ~132 min at walk speed
    assert _path_m([A, far]) / planner.WALK_SPEED_M_S / 60.0 > 60.0
    grids = {START: _marker_grid({(A[0], A[1]): 90.0, (far[0], far[1]): 91.0})}
    with pytest.raises(KeyError):
        _sample_path([A, far], START, 132.0, grids)


# --------------------------------------------------------------------------
# P0-3: route duration synchronized with thermal validation
# --------------------------------------------------------------------------
# Spec-scenario geometry: the direct route is a ~26 min walk (fits the
# requested 60 minutes inside hour 14); the detours are ~65-75 min walks
# whose ACTUAL durations cross into hour 15 (cf. the spec's "requested 60,
# candidate 95 min" example).
RA = [33.4000, -112.0000]
RB = [33.4200, -112.0000]                 # ~2.2 km direct
COOL1 = [33.4100, -111.9800]              # detour waypoint (~52 min route)
COOL2 = [33.4300, -111.9700]              # detour waypoint (~69 min route)


def _route_intent(duration_min: int) -> ParsedIntent:
    return ParsedIntent(
        task="walking", task_type="route", duration_minutes=duration_min,
        start_time="14:00", date="2026-08-26", location_name="Phoenix, AZ",
        lat=RA[0], lon=RA[1], end_lat=RB[0], end_lon=RB[1],
        end_location_name="Test End", heat_priority="high")


def _route_field_grid(hour_temp: float) -> HeatGrid:
    """Grid with real points near the start (so _coolest_point_near builds
    genuine detours) plus far fillers for along-route sampling."""
    return HeatGrid(
        lats=[RA[0], RB[0], COOL1[0], COOL2[0], 33.0, 34.0],
        lons=[RA[1], RB[1], COOL1[1], COOL2[1], -112.5, -112.5],
        temps_f=[hour_temp, hour_temp, 85.0, 80.0, hour_temp, hour_temp],
        source="fortyguard_live")


def test_long_candidate_requires_thermal_coverage_for_its_actual_duration(monkeypatch):
    """Requested 60 min, but the detours' ACTUAL walk durations (~65-75 min)
    cross into 15:00. With hour 15 unavailable, those candidates must be
    INFEASIBLE: they are never validated against only the requested 60
    minutes and then scored for their full duration."""
    requested_hours: list[int] = []

    async def fake_grid(lat, lon, date, hour):
        requested_hours.append(hour)
        if hour >= 15:
            raise ThermalDataUnavailableError(
                "no usable spatial tiles (map_data.features == [] / n_cells == 0)")
        return _route_field_grid(90.0 + hour)  # distinct temp per hour

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    intent = _route_intent(60)  # user asks for 60 minutes

    # Sanity: the detours really do cross into 15:00 (start 14:00).
    detour_min = _path_m([RA, COOL2, RB]) / planner.WALK_SPEED_M_S / 60.0
    assert detour_min > 60.0

    cands, _requested, reports, _summary = asyncio.run(
        _route_candidates(intent, now=NOW_10))

    # Only candidates whose ACTUAL duration stays inside hour 14 survive;
    # the ~69-minute "Coolest" detour crosses into 15:00 and was excluded,
    # never scored on hour-14-only coverage.
    assert [c.name for c in cands] == ["Fastest", "Balanced"]
    assert all(c.duration_minutes < 60.0 for c in cands)
    # The exclusions were REPORTED (infeasible), never silently scored.
    assert reports, "infeasible candidates must be reported"
    assert any(any(c.status.value == "unavailable" for c in r.result.checks)
               for r in reports)
    # The planner requested hour 15 for those candidates: it validated their
    # ACTUAL duration, not merely the requested 60 minutes (which alone
    # would only ever require hour 14).
    assert 15 in requested_hours


def test_all_candidates_too_long_raise_interval_unsupported(monkeypatch):
    """Every candidate's actual duration exceeds the usable coverage ->
    IntervalUnsupportedError (no fabricated exposure, no shortened routes)."""

    async def fake_grid(lat, lon, date, hour):
        if hour >= 15:
            raise ThermalDataUnavailableError("empty")
        return _route_field_grid(90.0)

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    # Force even the direct route beyond one hour of walking.
    monkeypatch.setattr(planner, "WALK_SPEED_M_S", 0.01)
    intent = _route_intent(60)
    with pytest.raises(IntervalUnsupportedError):
        asyncio.run(_route_candidates(intent, now=NOW_10))


def test_candidate_covering_multiple_hours_scores_each_hour_with_its_own_grid(monkeypatch):
    """Positive control for P0-3/P0-4 together: a candidate whose ACTUAL
    duration spans 14:00 and 15:00 is validated for BOTH hours and scored
    with each sample's own hour grid (14:00 = 90 F, 15:00 = 110 F)."""
    grids_built = {
        14: _marker_grid({(A[0], A[1]): 90.0, (B[0], B[1]): 90.0}),
        15: _marker_grid({(A[0], A[1]): 110.0, (B[0], B[1]): 110.0}),
    }

    async def fake_grid(lat, lon, date, hour):
        if hour in grids_built:
            return grids_built[hour]
        raise ThermalDataUnavailableError("empty")

    monkeypatch.setattr(planner, "get_heat_grid", fake_grid)
    # Slow walking so the direct route takes ~95 minutes (spans 14:00-16:00).
    monkeypatch.setattr(planner, "WALK_SPEED_M_S", 0.39)
    intent = ParsedIntent(
        task="walking", task_type="route", duration_minutes=60,
        start_time="14:00", date="2026-08-26", location_name="Phoenix, AZ",
        lat=A[0], lon=A[1], end_lat=B[0], end_lon=B[1],
        end_location_name="Test End", heat_priority="high")

    cands, _requested, _reports, _summary = asyncio.run(
        _route_candidates(intent, now=NOW_10))
    direct = next(c for c in cands if c.name == "Fastest")
    assert direct.duration_minutes > 90.0  # actual duration, not requested 60
    temps = {t for _, t, _ in direct.samples}
    assert 90.0 in temps and 110.0 in temps  # both hours' own grids used
    offsets = [o for o, _, _ in direct.samples]
    assert any(o < 60 for o in offsets) and any(o >= 60 for o in offsets)
