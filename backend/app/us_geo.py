"""Deterministic, offline U.S. geographic membership check.

FortyGuard documents that heatmap coordinates for the current release must
fall within the United States (https://docs-api.fortyguard.com/docs/limitations).
The previous validator only checked a coarse CONUS lat/lon bounding box,
which incorrectly accepted non-US locations (e.g. Toronto, Ciudad Juárez,
Tijuana, Vancouver BC) that sit inside that rectangle.

This module replaces the rectangle-only check with a real polygon
membership test:

  1. Latitude / longitude are range-validated first (lat ∈ [-90, 90],
     lon ∈ [-180, 180]) so obviously invalid inputs are rejected loudly
     BEFORE any polygon test.
  2. A pre-built US polygon (``app/data/us_polygon.json``) is loaded once at
     module import; every request re-uses the same in-memory shapely
     geometry — deterministic, no network access, no external geocoding
     service, no dependency on the developer machine's location.
  3. ``is_in_us(lat, lon)`` returns True iff the coordinate lies inside that
     polygon.

DEFINITION OF "UNITED STATES"
-----------------------------

The polygon is derived from Natural Earth's 10 m ``admin_0 countries``
dataset (public domain) via an offline preparation step:

  * Start from the "USA" feature.
  * Apply an adaptive per-polygon simplification (Douglas–Peucker,
    tolerance capped at ~0.01°) so the shape stays small enough to commit
    to the repository (~640 KB).
  * Buffer the result outward by ~0.012° (≈ 1.3 km) to tolerate
    coastline simplification for coastal city centers (Miami, New York,
    Boston, Juneau, etc.).
  * Subtract the neighboring countries' polygons (Canada, Mexico, Cuba,
    Bahamas, Jamaica, Haiti, Dominican Republic, Belize, Guatemala,
    Honduras — each with a small coastal buffer) so the outward buffer
    cannot spill across land borders (a naive positive buffer would
    otherwise accept Tijuana/Ciudad Juárez/Windsor/Niagara Falls ON since
    they sit right against the border).
  * Drop trivial islands smaller than ~6 km² (they cannot affect
    activity-scale planning).

This gives a coarse, in/out-only membership test that matches FortyGuard's
"United States" wording for the 50 states plus their coastal fringe.  It
is explicitly NOT a legal boundary and is not meant to resolve
sub-kilometer border disputes; it is the smallest defensible deterministic
local solution for MVP-scale coverage validation.

FortyGuard currently states "United States" without further qualification,
so this validator applies to the 50 US states (mainland + Alaska + Hawaii)
and their coastal fringe.  Non-state US territories (Puerto Rico, US
Virgin Islands, Guam, American Samoa, Northern Mariana Islands) are NOT
covered by this polygon: FortyGuard's documented coverage does not
explicitly include them, and we prefer to reject rather than fabricate
membership.  If FortyGuard confirms coverage for a territory the polygon
can be regenerated to include it.
"""
from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import Point, shape

_US_POLYGON_PATH = Path(__file__).resolve().parent / "data" / "us_polygon.json"


def _load_us_polygon():
    with _US_POLYGON_PATH.open(encoding="utf-8") as fh:
        feat = json.load(fh)
    # Prepared geometry gives fast, deterministic point-in-polygon lookups.
    return shape(feat["geometry"])


# Loaded once at import time. Deterministic across processes: same file =
# same geometry = same in/out answers for every request.
_US_POLYGON = _load_us_polygon()


def coordinates_range_valid(lat: float, lon: float) -> bool:
    """Basic numeric-range validity (does not decide U.S. membership)."""
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def is_in_us(lat: float, lon: float) -> bool:
    """True iff (lat, lon) falls inside the packaged U.S. polygon.

    Latitude / longitude are numerically range-validated first; obviously
    invalid inputs return False without touching the polygon.
    """
    if not coordinates_range_valid(lat, lon):
        return False
    # GeoJSON coordinates are (lon, lat).
    return _US_POLYGON.covers(Point(lon, lat))
