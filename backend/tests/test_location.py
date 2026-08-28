"""Location resolution tests (spec §21-E).

Covers: valid mainland US, Alaska, Hawaii, Canada, Mexico, invalid
coordinates, coordinate-text input, timezone resolution, and the honest
no-fake-geocoder failure mode. The existing US polygon gate
(test_us_geo.py) is preserved and extended here for the resolver layer.
"""
import pytest

from app.location import (
    LocationResolutionError,
    LocationResolver,
    ResolvedLocation,
    location_resolver,
)


# ---------------------------------------------------------------------------
# Gazetteer resolution (deterministic, offline)
# ---------------------------------------------------------------------------
def test_named_park_resolves_with_timezone():
    r = location_resolver.resolve("Discovery Park")
    assert r.display_name == "Discovery Park, Sacramento, CA"
    assert abs(r.latitude - 38.6126) < 1e-6
    assert abs(r.longitude - (-121.5188)) < 1e-6
    assert r.timezone == "America/Los_Angeles"


def test_city_resolves_to_destination_timezone_not_machine_tz():
    """Phoenix times must be interpreted as Phoenix local - the resolver
    returns the destination's IANA zone, never the dev machine's."""
    r = location_resolver.resolve("Phoenix")
    assert r.timezone == "America/Phoenix"
    now = r.now_local()
    assert now.utcoffset().total_seconds() == -7 * 3600  # fixed, no DST


def test_longest_alias_wins():
    """'discovery park sacramento' must not be shadowed by 'sacramento'."""
    r = location_resolver.resolve("discovery park sacramento")
    assert "Discovery Park" in r.display_name


def test_unknown_place_fails_loudly_no_fabrication():
    with pytest.raises(LocationResolutionError) as ei:
        location_resolver.resolve("zzz definitely not a real place zzz")
    assert ei.value.code == "INVALID_LOCATION"
    assert "offline" in str(ei.value)  # honest about the boundary


# ---------------------------------------------------------------------------
# Coordinate input + timezone lookup
# ---------------------------------------------------------------------------
def test_coordinate_text_resolves_real_timezone():
    r = location_resolver.resolve("38.6126, -121.5188")
    assert r.timezone == "America/Los_Angeles"
    r2 = location_resolver.resolve("33.4484 -112.0740")  # space-separated
    assert r2.timezone == "America/Phoenix"


def test_valid_mainland_us_accepted():
    r = location_resolver._from_coordinates(40.7812, -73.9665, label="x")
    assert r.timezone == "America/New_York"


def test_alaska_and_hawaii_resolve():
    ak = location_resolver._from_coordinates(61.2181, -149.9003, label="x")
    assert ak.timezone == "America/Anchorage"
    hi = location_resolver._from_coordinates(21.3069, -157.8583, label="x")
    assert hi.timezone == "Pacific/Honolulu"


def test_canada_rejected_outside_supported_area():
    with pytest.raises(LocationResolutionError) as ei:
        location_resolver._from_coordinates(43.6532, -79.3832, label="Toronto")
    assert ei.value.code == "LOCATION_OUTSIDE_SUPPORTED_AREA"


def test_mexico_rejected_outside_supported_area():
    with pytest.raises(LocationResolutionError) as ei:
        location_resolver._from_coordinates(32.5149, -117.0382, label="Tijuana")
    assert ei.value.code == "LOCATION_OUTSIDE_SUPPORTED_AREA"


def test_invalid_coordinates_rejected():
    with pytest.raises(LocationResolutionError) as ei:
        location_resolver._from_coordinates(200.0, -112.0, label="x")
    assert ei.value.code == "INVALID_LOCATION"


# ---------------------------------------------------------------------------
# Search suggestions
# ---------------------------------------------------------------------------
def test_search_returns_suggestions():
    out = location_resolver.search("park")
    assert len(out) > 0
    assert all(isinstance(r, ResolvedLocation) for r in out)
    assert any("Park" in r.display_name for r in out)


def test_search_coordinates_resolves_directly():
    out = location_resolver.search("33.4484, -112.0740")
    assert len(out) == 1
    assert out[0].timezone == "America/Phoenix"


def test_search_empty_or_unknown_returns_empty():
    assert location_resolver.search("") == []
    assert location_resolver.search("zzz nothing zzz") == []


def test_resolver_is_stateless_and_shareable():
    r = LocationResolver()
    a = r.resolve("Phoenix")
    b = location_resolver.resolve("Phoenix")
    assert a == b
