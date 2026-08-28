"""REAL FortyGuard API end-to-end test (spec §20 RULE 20).

Skipped automatically unless a real key exists in .env / environment.
Run explicitly with:  pytest tests/test_live_e2e.py -v
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

pytestmark = pytest.mark.skipif(
    not settings.fortyguard_api_key or settings.use_mock,
    reason="No FORTYGUARD_API_KEY configured - live e2e skipped.")


def test_live_stationary_plan_uses_real_fortyguard_data():
    client = TestClient(app)
    r = client.post("/api/plan", json={
        "text": "I need to work outdoors for 90 minutes starting at 2 PM."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data_source"] == "fortyguard_live"
    assert len(body["plans"]) == 3
    assert all(p["exposure"] > 0 for p in body["plans"])
    assert body["recommended_id"] in ("A", "B", "C")


def test_live_structured_plan_uses_real_fortyguard_data():
    """Live structured workflow (spec §23): resolve a real US location,
    compute destination-local now, choose a window inside +12h, submit one
    real heatmap per unique required hour, verify real results come back.

    The window is derived from the DESTINATION's local clock at runtime so
    the test stays inside the rolling horizon whenever it is run.
    """
    from app.location import location_resolver

    client = TestClient(app)

    # 1. Resolve a real US location (deterministic offline gazetteer).
    loc = location_resolver.resolve("Discovery Park")

    # 2. Destination-local now; choose a 2-hour window 1h..3h ahead so the
    #    required hours sit comfortably inside the rolling +12h horizon.
    now_local = loc.now_local()
    start_h = (now_local.hour + 1) % 24
    end_h = (now_local.hour + 3) % 24
    if end_h <= start_h:  # window would cross midnight: skip rather than fake
        pytest.skip("destination-local window would cross midnight")

    r = client.post("/api/plan/structured", json={
        "date": now_local.strftime("%Y-%m-%d"),
        "duration_minutes": 60,
        "available_start": f"{start_h:02d}:00",
        "available_end": f"{end_h:02d}:00",
        "location": {"name": loc.display_name,
                     "latitude": loc.latitude, "longitude": loc.longitude},
    })
    assert r.status_code == 200, r.text
    body = r.json()

    # 3. Real data end-to-end: live source, resolved location/timezone, and
    #    a best candidate computed from per-hour Completed activities with
    #    non-empty map_data.features and average_temperature values.
    assert body["data_source"] == "fortyguard_live"
    assert body["location_name"] == loc.display_name
    assert body["timezone"] == "America/Los_Angeles"
    assert body["best"]["thermal_status"] == "ok"
    assert body["best"]["avg_temperature_f"] is not None
    assert body["best"]["exposure"] is not None and body["best"]["exposure"] > 0
    assert len(body["best"]["timeline"]) >= 1
    assert "not medical advice" in body["disclaimer"]
