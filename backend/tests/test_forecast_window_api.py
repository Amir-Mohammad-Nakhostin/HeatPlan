"""GET /api/forecast/window: deterministic pre-flight horizon checks.

This endpoint must never submit anything to FortyGuard; it only resolves the
location, expresses the rolling horizon in destination-local time, and runs
the same deterministic window validation as the planning endpoint.
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_window_by_coordinates_ok():
    r = client.get("/api/forecast/window",
                   params={"lat": 33.4521, "lon": -111.9470})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["timezone"] == "America/Phoenix"
    assert body["horizon_start"] and body["horizon_end"]
    assert "–" in body["horizon_display"]


def test_window_by_text_resolves_location():
    r = client.get("/api/forecast/window", params={"q": "papago park"})
    assert r.status_code == 200
    body = r.json()
    assert body["location_name"].startswith("Papago Park")
    assert abs(body["latitude"] - 33.4521) < 1e-3
    assert body["timezone"] == "America/Phoenix"


def test_window_unresolvable_text_is_422_invalid_location():
    r = client.get("/api/forecast/window", params={"q": "zzz-not-a-real-place-qqq"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_LOCATION"


def test_window_invalid_coordinates_rejected():
    r = client.get("/api/forecast/window", params={"lat": 999.0, "lon": 0.0})
    assert r.status_code == 422


def test_far_future_window_fails_horizon_preflight():
    r = client.get("/api/forecast/window", params={
        "lat": 33.4521, "lon": -111.9470, "date": "2099-01-01",
        "start": "13:00", "end": "17:00", "duration_minutes": 60})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "FORECAST_HORIZON_EXCEEDED"
    assert body["first_unavailable_hour"]


def test_inverted_window_fails_validation_preflight():
    r = client.get("/api/forecast/window", params={
        "lat": 33.4521, "lon": -111.9470, "date": "2026-08-28",
        "start": "17:00", "end": "13:00", "duration_minutes": 60})
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "INVALID_TIME_WINDOW"


def test_outside_us_window_fails_location_gate():
    # Toronto is outside the U.S. polygon. The deterministic location gate
    # may fire either at resolution time (422, structured detail) or at the
    # window preflight stage (200 with ok=false); both are valid refusals
    # and neither submits anything to FortyGuard.
    r = client.get("/api/forecast/window", params={
        "lat": 43.65, "lon": -79.38, "date": "2026-08-28",  # Toronto
        "start": "13:00", "end": "17:00", "duration_minutes": 60})
    if r.status_code == 422:
        assert r.json()["detail"]["code"] == "LOCATION_OUTSIDE_SUPPORTED_AREA"
    else:
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "LOCATION_OUTSIDE_SUPPORTED_AREA"
