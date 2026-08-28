"""Regression tests for real polygon-based U.S.-only validation.

Replaces the previous rectangle-only CONUS envelope, which accepted
Toronto, Ciudad Juárez, Tijuana, Vancouver BC, Windsor, and other non-US
locations that happen to sit inside a lat/lon rectangle around CONUS. See
``app/us_geo.py`` for the exact definition of "United States" used here.
"""
from __future__ import annotations

import pytest

from app.fortyguard_client import FortyGuardError, validate_us_coordinates
from app.us_geo import coordinates_range_valid, is_in_us


# ---------------------------------------------------------------------------
# Accepted: valid US coordinates (mainland + Alaska + Hawaii + coastal)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("Phoenix, AZ", 33.4484, -112.0740),
        ("Denver, CO", 39.7392, -104.9903),
        ("Chicago, IL", 41.8781, -87.6298),
        ("New York, NY", 40.7128, -74.0060),
        ("Miami, FL (coastal)", 25.7617, -80.1918),
        ("San Diego, CA", 32.7157, -117.1611),
        ("Seattle, WA", 47.6062, -122.3321),
        ("Boston, MA (coastal)", 42.3601, -71.0589),
        ("Key West, FL (island)", 24.5551, -81.7800),
        ("Anchorage, AK", 61.2181, -149.9003),
        ("Juneau, AK", 58.3019, -134.4197),
        ("Honolulu, HI", 21.3069, -157.8583),
        ("Hilo, HI", 19.7297, -155.0900),
    ],
)
def test_valid_us_coordinates_accepted(name, lat, lon):
    assert is_in_us(lat, lon), f"expected {name} inside US polygon"
    validate_us_coordinates(lat, lon)  # must not raise


# ---------------------------------------------------------------------------
# Rejected: non-US locations that would slip past a rectangle-only envelope
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,lat,lon",
    [
        # Inside the OLD CONUS bounding box (24..50 N, -125..-66 E) but NOT US
        ("Toronto, ON, Canada", 43.6532, -79.3832),
        ("Montreal, QC, Canada", 45.5019, -73.5674),
        ("Windsor, ON, Canada (across from Detroit)", 42.2833, -83.0167),
        ("Niagara Falls, ON, Canada", 43.0896, -79.0849),
        ("Vancouver, BC, Canada", 49.2827, -123.1207),
        ("Tijuana, Mexico (across from San Diego)", 32.5149, -117.0382),
        ("Ciudad Juárez, Mexico (across from El Paso)", 31.6904, -106.4245),
        ("Nogales, Mexico (across from Nogales AZ)", 31.3067, -110.9407),
        ("Monterrey, Mexico", 25.6866, -100.3161),
        # Fully outside the old CONUS envelope, but still worth confirming
        ("Mexico City, Mexico", 19.4326, -99.1332),
        ("Cancún, Mexico", 21.1619, -86.8515),
        ("Havana, Cuba", 23.1136, -82.3666),
        ("Nassau, Bahamas", 25.0480, -77.3554),
        ("London, UK", 51.5074, -0.1278),
        ("Paris, France", 48.8566, 2.3522),
        # Oceans/gulf far from any land - never US
        ("Atlantic Ocean mid-latitudes", 35.0, -70.0),
        ("Pacific Ocean mid-latitudes", 40.0, -130.0),
        ("Gulf of Mexico deep water", 25.0, -90.0),
    ],
)
def test_non_us_coordinates_rejected(name, lat, lon):
    assert not is_in_us(lat, lon), f"{name} must NOT be treated as inside US"
    with pytest.raises(FortyGuardError, match="U.S."):
        validate_us_coordinates(lat, lon)


# ---------------------------------------------------------------------------
# Numeric-range validation (must precede polygon lookup)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_lat", [-91.0, 91.0, 200.0, float("nan")])
def test_invalid_latitude_rejected(bad_lat):
    assert not coordinates_range_valid(bad_lat, -112.0740)
    with pytest.raises(FortyGuardError):
        validate_us_coordinates(bad_lat, -112.0740)


@pytest.mark.parametrize("bad_lon", [-181.0, 181.0, 500.0, float("nan")])
def test_invalid_longitude_rejected(bad_lon):
    assert not coordinates_range_valid(33.4484, bad_lon)
    with pytest.raises(FortyGuardError):
        validate_us_coordinates(33.4484, bad_lon)


def test_range_error_message_distinguishes_from_us_rejection():
    """Numeric-range failures raise a message DIFFERENT from the US-only
    rejection message so a client can tell why a coordinate was rejected.
    """
    with pytest.raises(FortyGuardError, match="valid latitude") as ei:
        validate_us_coordinates(200.0, -112.0740)
    assert "U.S." not in str(ei.value)


# ---------------------------------------------------------------------------
# Border / edge cases: neighbors of US cities on the FOREIGN side must fail
# even though they are within a few km of the US border.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "us_name,us_lat,us_lon,foreign_name,f_lat,f_lon",
    [
        # Detroit, MI (US) vs Windsor, ON (Canada), Detroit River between them
        ("Detroit MI", 42.3314, -83.0458, "Windsor ON", 42.2833, -83.0167),
        # Buffalo, NY (US) vs Niagara Falls, ON (Canada)
        ("Buffalo NY", 42.8864, -78.8784, "Niagara Falls ON", 43.0896, -79.0849),
        # San Diego, CA (US) vs Tijuana, Mexico
        ("San Diego CA", 32.7157, -117.1611, "Tijuana MX", 32.5149, -117.0382),
        # El Paso, TX (US) vs Ciudad Juárez, Mexico (Rio Grande border)
        ("El Paso TX", 31.7619, -106.4850, "Ciudad Juárez MX", 31.6904, -106.4245),
    ],
)
def test_border_pair_us_side_accepted_foreign_side_rejected(
    us_name, us_lat, us_lon, foreign_name, f_lat, f_lon
):
    validate_us_coordinates(us_lat, us_lon)   # US side ok
    with pytest.raises(FortyGuardError, match="U.S."):
        validate_us_coordinates(f_lat, f_lon)  # foreign side rejected


def test_alaska_hawaii_still_accepted_smoke():
    """Explicit spec-required regression: Alaska and Hawaii are US states."""
    validate_us_coordinates(61.2181, -149.9003)   # Anchorage, AK
    validate_us_coordinates(21.3069, -157.8583)   # Honolulu, HI


def test_pole_rejected_edge_case():
    """Extreme edge: geographic poles are numerically valid lat/lon but not
    inside the US - polygon lookup must handle them without crashing."""
    assert not is_in_us(90.0, 0.0)
    assert not is_in_us(-90.0, 0.0)
    with pytest.raises(FortyGuardError, match="U.S."):
        validate_us_coordinates(90.0, 0.0)


def test_validator_is_deterministic_and_offline():
    """The validator must never depend on the developer machine location or
    on the network; repeated calls give identical answers."""
    # Two calls must return identical bool results.
    a = is_in_us(33.4484, -112.0740)
    b = is_in_us(33.4484, -112.0740)
    assert a is True and b is True
    # And the negative side too.
    assert is_in_us(43.6532, -79.3832) is False
    assert is_in_us(43.6532, -79.3832) is False
