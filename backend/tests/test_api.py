"""Backend flow: complete request -> 3 plans -> recommendation (mock mode).

Runs with FORTYGUARD_MOCK=true so no network/credentials are needed; the
response must be labeled data_source=mock. A separate live test
(test_live_e2e.py) is skipped unless a real key is present in .env.
"""
import os

os.environ["FORTYGUARD_MOCK"] = "true"
# Deterministic clock: Phoenix 2026-08-26 10:00 -> rolling horizon 10:00-22:00.
# The demo "2 PM" / "3 PM" starts land inside the horizon regardless of when
# the suite is actually executed.
os.environ["HEATPLAN_NOW_OVERRIDE"] = "2026-08-26T17:00:00+00:00"
os.environ["HEATPLAN_FORECAST_HOURS"] = "12"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_config_labels_mock():
    r = client.get("/api/config")
    assert r.json()["data_source"] == "mock"


def test_full_plan_flow_stationary():
    r = client.post("/api/plan", json={
        "text": "I need to work outdoors for 90 minutes starting at 2 PM."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data_source"] == "mock"
    assert body["intent"]["duration_minutes"] == 90
    assert len(body["plans"]) == 3
    ids = {p["id"] for p in body["plans"]}
    assert ids == {"A", "B", "C"}
    assert body["recommended_id"] in ids
    # baseline invariants
    a = next(p for p in body["plans"] if p["id"] == "A")
    assert a["time_penalty_minutes"] == 0
    # exposures are real numbers, monotonic A >= C for stationary shifts
    c = next(p for p in body["plans"] if p["id"] == "C")
    assert c["exposure"] <= a["exposure"] + 1e-6
    assert c["exposure_reduction_pct"] >= 0
    # every plan carries a computed timeline
    for p in body["plans"]:
        assert p["exposure"] > 0
        assert len(p["timeline"]) >= 1
    assert body["explanation"]


def test_full_plan_flow_route():
    r = client.post("/api/plan", json={
        "text": "Walk from Phoenix to Tempe for 60 minutes at 3 pm, coolest please"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"]["task_type"] == "route"
    a = next(p for p in body["plans"] if p["id"] == "A")
    c = next(p for p in body["plans"] if p["id"] == "C")
    assert c["distance_m"] >= a["distance_m"]
    assert len(a["geometry"]) >= 2


def test_rejects_non_us_coordinates():
    r = client.post("/api/plan", json={"text": "Walk outside for 30 minutes",
                                       "lat": 48.8566, "lon": 2.3522})  # Paris
    assert r.status_code == 502
    assert "U.S." in r.json()["detail"]


def test_rejects_empty_text():
    r = client.post("/api/plan", json={"text": "hi"})
    assert r.status_code == 422


def test_malformed_clock_time_degrades_gracefully():
    """P1 regression: 'at 99:99' must NOT crash the planner (was HTTP 500
    via ValueError in _start_local); it falls back to 14:00 with a warning."""
    r = client.post("/api/plan", json={
        "text": "walk outside for 30 minutes at 99:99"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent"]["start_time"] == "14:00"
    assert any("not a valid clock time" in w for w in body["intent"]["warnings"])


def test_explicit_coordinates_win_over_text_location():
    """P1 regression: caller-supplied lat/lon override the gazetteer match
    from the text ('Miami') - the same rule the LLM mode must follow."""
    r = client.post("/api/plan", json={
        "text": "Walk outside in Miami for 30 minutes starting at 2 PM",
        "lat": 33.4484, "lon": -112.0740})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["intent"]["lat"], body["intent"]["lon"]) == (33.4484, -112.0740)
    assert body["location_name"] == "Selected location"


def test_same_input_same_recommendation():
    payload = {"text": "I need to work outdoors for 90 minutes starting at 2 PM."}
    r1 = client.post("/api/plan", json=payload).json()
    r2 = client.post("/api/plan", json=payload).json()
    assert r1["recommended_id"] == r2["recommended_id"]
    assert r1["scores"] == r2["scores"]
    assert [p["exposure"] for p in r1["plans"]] == [p["exposure"] for p in r2["plans"]]


def test_empty_hour_is_unavailable_not_forecast_end(monkeypatch):
    """E2E (C3): an empty hour INSIDE the horizon is a per-timestamp data gap,
    NOT 'the forecast window ended'. The user's requested interval (14:00 +
    90 min traverses [14:00, 15:00]) is itself unsupported, so the response
    names exactly the requested interval's UNAVAILABLE hour - alternative
    candidate timestamps are never mixed in."""
    from app import fortyguard_client as fg_mod
    from app.fortyguard_client import ThermalDataUnavailableError

    calls: list[int] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append(hour)
        if hour == 15:
            raise ThermalDataUnavailableError(
                "no usable spatial tiles (map_data.features == [] / n_cells == 0)")
        return fg_mod._mock_grid(lat, lon, hour)

    monkeypatch.setattr("app.planner.get_heat_grid", fake_grid)
    r = client.post("/api/plan", json={
        "text": "I need to work outdoors for 90 minutes starting at 2 PM."})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["code"] == "THERMAL_INTERVAL_UNSUPPORTED"
    assert "Requested thermal interval: 14:00, 15:00" in body["message"]
    assert "Unavailable live thermal data: 15:00" in body["message"]
    assert "Outside" not in body["message"]
    checks = {c["timestamp"]: c["status"]
              for c in body["requested_interval"]["checks"]}
    assert checks == {"14:00": "usable", "15:00": "unavailable"}
    assert body["diagnostics"]["alternative_candidates"] == 0
    # The empty hour WAS requested -> the gap is live data, not the horizon.
    assert 14 in calls and 15 in calls


def test_all_hours_unavailable_422(monkeypatch):
    """E2E: when NO required timestamp has usable spatial tiles the request
    fails with an explicit UNAVAILABLE message - distinct from horizon."""
    from app import fortyguard_client as fg_mod
    from app.fortyguard_client import ThermalDataUnavailableError

    calls: list[int] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append(hour)
        raise ThermalDataUnavailableError(
            "no usable spatial tiles (map_data.features == [] / n_cells == 0)")

    monkeypatch.setattr("app.planner.get_heat_grid", fake_grid)
    r = client.post("/api/plan", json={
        "text": "I need to work outdoors for 60 minutes starting at 2 PM."})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["code"] == "THERMAL_INTERVAL_UNSUPPORTED"
    assert "Requested thermal interval: 14:00" in body["message"]
    assert "Unavailable live thermal data: 14:00" in body["message"]
    assert "Outside" not in body["message"]
    assert any(c["detail"] and "n_cells == 0" in c["detail"]
               for c in body["requested_interval"]["checks"])
    assert 14 in calls  # the empty hour IS requested: it is inside the horizon


def test_outside_horizon_422_rejected_before_request(monkeypatch):
    """E2E: timestamps beyond the rolling +12 h horizon are rejected BEFORE
    any FortyGuard request - never conflated with an empty response."""
    from app import fortyguard_client as fg_mod

    calls: list[int] = []

    async def fake_grid(lat, lon, date, hour):
        calls.append(hour)
        return fg_mod._mock_grid(lat, lon, hour)

    monkeypatch.setattr("app.planner.get_heat_grid", fake_grid)
    # "10 PM" -> 22:00 start, 90 min -> required [22:00, 23:00]; 23:00 > 22:00
    r = client.post("/api/plan", json={
        "text": "I need to work outdoors for 90 minutes starting at 10 PM."})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["code"] == "THERMAL_INTERVAL_UNSUPPORTED"
    assert "Requested thermal interval: 22:00, 23:00" in body["message"]
    assert "Outside current forecast horizon: 23:00" in body["message"]
    assert "Unavailable" not in body["message"]  # distinct condition
    checks = {c["timestamp"]: c["status"]
              for c in body["requested_interval"]["checks"]}
    assert checks == {"22:00": "usable", "23:00": "outside_horizon"}
    assert calls == [22]  # outside hour was never requested
