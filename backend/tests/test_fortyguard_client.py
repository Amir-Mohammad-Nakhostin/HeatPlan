"""FortyGuard client: polling lifecycle, failure, timeout, malformed input.

All HTTP is mocked with a fake transport - no network, no credentials.
"""
import os

os.environ["FORTYGUARD_MOCK"] = "false"
os.environ.setdefault("FORTYGUARD_API_KEY", "test-key")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app import fortyguard_client as fg  # noqa: E402


def _transport(script: list):
    """script: list of (status_code, json_body) served in order."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        code, body = script[i]
        return httpx.Response(code, json=body)

    return httpx.MockTransport(handler), calls


@pytest.mark.anyio
async def test_submit_and_poll_success(monkeypatch):
    # C5: the documented live per-tile field is properties.average_temperature
    # in degrees CELSIUS - the fixture must use it (a generic "temperature"
    # field is NOT a live Celsius source).
    result = {
        "map_data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"average_temperature": 40.5556},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-112.071, 33.439], [-112.069, 33.439],
                            [-112.069, 33.441], [-112.071, 33.441],
                            [-112.071, 33.439]
                        ]],
                    },
                },
                {
                    "type": "Feature",
                    "properties": {"average_temperature": 39.4444},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [-112.061, 33.449], [-112.059, 33.449],
                            [-112.059, 33.451], [-112.061, 33.451],
                            [-112.061, 33.449]
                        ]],
                    },
                },
            ],
        },
    }
    transport, _ = _transport([
        (200, {"data": {"activity_id": "act-1"}}),
        (200, {"data": {"status": "Processing"}}),
        (200, {"data": {"status": "Completed", "result": result}}),
    ])
    monkeypatch.setattr(fg.settings, "poll_interval_s", 0.01)
    async with httpx.AsyncClient(transport=transport) as client:
        activity = await fg._submit_heatmap(client, 33.44, -112.07, "2026-08-26", 14)
        assert activity == "act-1"
        result = await fg._poll_status(client, activity)
        grid = fg._extract_grid(result)
        assert grid.source == "fortyguard_live"
        assert len(grid.temps_f) == 2
        assert abs(grid.nearest_temp(33.44, -112.07) - 105.0) < 0.01


@pytest.mark.anyio
async def test_failed_activity_is_terminal(monkeypatch):
    transport, _ = _transport([(200, {"data": {"status": "Failed"}})])
    monkeypatch.setattr(fg.settings, "poll_interval_s", 0.01)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(fg.FortyGuardError, match="failed"):
            await fg._poll_status(client, "act-x")


@pytest.mark.anyio
async def test_polling_timeout(monkeypatch):
    transport, _ = _transport([(200, {"data": {"status": "Processing"}})])
    monkeypatch.setattr(fg.settings, "poll_interval_s", 0.01)
    monkeypatch.setattr(fg.settings, "poll_timeout_s", 0.05)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(fg.FortyGuardError, match="timed out"):
            await fg._poll_status(client, "act-y")


@pytest.mark.anyio
async def test_malformed_submission():
    transport, _ = _transport([(200, {"unexpected": True})])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(fg.FortyGuardError, match="malformed"):
            await fg._submit_heatmap(client, 33.44, -112.07, "2026-08-26", 14)


@pytest.mark.anyio
async def test_auth_error():
    transport, _ = _transport([(401, {"detail": "invalid key"})])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(fg.FortyGuardError, match="authentication"):
            await fg._submit_heatmap(client, 33.44, -112.07, "2026-08-26", 14)


def test_non_us_coordinates_rejected_before_any_request():
    with pytest.raises(fg.FortyGuardError, match="U.S."):
        fg.validate_us_coordinates(51.5074, -0.1278)  # London


def test_mock_grid_deterministic_and_spatially_variable():
    g1 = fg._mock_grid(33.4484, -112.0740, 14)
    g2 = fg._mock_grid(33.4484, -112.0740, 14)
    assert g1.temps_f == g2.temps_f
    assert len(set(g1.temps_f)) > 5  # micro-climate variation the planner needs
    assert g1.source == "mock"
