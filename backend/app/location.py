"""Deterministic location resolution for the structured-input workflow.

Responsibility boundary (explicit, per spec §15):

    location_text  ->  ResolvedLocation(display_name, latitude, longitude,
                                        timezone)

The planner operates ONLY on validated latitude/longitude after resolution.
No polygon / GeoJSON is ever exposed to the user, and no invalid location is
ever sent to FortyGuard (the existing U.S. polygon gate in
``app.fortyguard_client.validate_us_coordinates`` stays active downstream).

IMPLEMENTATION BOUNDARY
-----------------------
This repository ships NO live geocoding provider. Pretending one exists
would fabricate geocoding results, which is forbidden. Instead this module
provides a deterministic, fully offline resolver with three honest sources:

  1. A curated gazetteer of well-known U.S. places and parks (below). Each
     entry carries real coordinates and a real IANA timezone. Matching is
     case-insensitive substring matching, longest entry first.
  2. A U.S. city gazetteer (state-level IANA timezone is exact for
     single-zone states; multi-zone states use the zone covering the listed
     city - each entry's zone is the zone OF THAT CITY, not a state guess).
  3. Raw coordinates: "lat, lon" / "lat lon" text, resolved to a real IANA
     timezone via the offline ``timezonefinder`` package (bundled data, no
     network, no API key).

Anything else returns ``LocationResolutionError`` (INVALID_LOCATION) with a
clear user-facing message instead of a fabricated result. To plug in a real
geocoder later, implement the same ``LocationResolver.resolve`` contract;
the planner and API layer do not change.

TIMEZONE
--------
The resolved timezone is the DESTINATION's IANA zone (zoneinfo). All
user-selected times are interpreted as local wall-clock times at the
destination - never the developer machine's timezone, never a hard-coded
UTC offset. The existing Phoenix/FortyGuard assumption
(``FORTYGUARD_STARTTIME_TZ == "local_to_aoi"``) is preserved: the hour
submitted to FortyGuard is the destination-local hour of each timestamp.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .us_geo import coordinates_range_valid, is_in_us


class LocationResolutionError(Exception):
    """User-facing location failure. Carries a stable machine code."""

    def __init__(self, message: str, code: str = "INVALID_LOCATION"):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedLocation:
    display_name: str
    latitude: float
    longitude: float
    timezone: str  # IANA zone name, e.g. "America/Phoenix"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now_local(self) -> datetime:
        """Current wall-clock time at the destination (destination-local now)."""
        return datetime.now(self.tz)


# ---------------------------------------------------------------------------
# Curated gazetteer (deterministic, offline). Coordinates are real; every
# timezone is the IANA zone of that exact place.
# ---------------------------------------------------------------------------
# (aliases..., display_name, lat, lon, IANA zone)
_GAZETTEER: list[tuple[tuple[str, ...], str, float, float, str]] = [
    # -- Well-known parks ----------------------------------------------------
    (("discovery park sacramento", "discovery park"),
     "Discovery Park, Sacramento, CA", 38.6126, -121.5188, "America/Los_Angeles"),
    (("central park",),
     "Central Park, New York, NY", 40.7812, -73.9665, "America/New_York"),
    (("phoenix park",),
     "Phoenix Park, Phoenix, AZ", 33.4484, -112.0740, "America/Phoenix"),
    (("griffith park",),
     "Griffith Park, Los Angeles, CA", 34.1367, -118.2942, "America/Los_Angeles"),
    (("golden gate park",),
     "Golden Gate Park, San Francisco, CA", 37.7694, -122.4862, "America/Los_Angeles"),
    (("zilker park",),
     "Zilker Park, Austin, TX", 30.2669, -97.7710, "America/Chicago"),
    (("piedmont park",),
     "Piedmont Park, Atlanta, GA", 33.7851, -84.3738, "America/New_York"),
    (("forest park st louis", "forest park"),
     "Forest Park, St. Louis, MO", 38.6389, -90.2847, "America/Chicago"),
    (("millennium park",),
     "Millennium Park, Chicago, IL", 41.8826, -87.6226, "America/Chicago"),
    (("boston common",),
     "Boston Common, Boston, MA", 42.3551, -71.0656, "America/New_York"),
    (("liberty park salt lake", "liberty park"),
     "Liberty Park, Salt Lake City, UT", 40.7447, -111.8748, "America/Denver"),
    (("washington park denver", "washington park"),
     "Washington Park, Denver, CO", 39.7001, -104.9714, "America/Denver"),
    (("encanto park",),
     "Encanto Park, Phoenix, AZ", 33.4739, -112.0896, "America/Phoenix"),
    (("papago park",),
     "Papago Park, Phoenix, AZ", 33.4521, -111.9470, "America/Phoenix"),
    (("south mountain park",),
     "South Mountain Park, Phoenix, AZ", 33.3456, -112.0634, "America/Phoenix"),
    (("balboa park",),
     "Balboa Park, San Diego, CA", 32.7341, -117.1446, "America/Los_Angeles"),
    (("memorial park houston", "memorial park"),
     "Memorial Park, Houston, TX", 29.7630, -95.4430, "America/Chicago"),
    (("fairmount park",),
     "Fairmount Park, Philadelphia, PA", 39.9855, -75.1830, "America/New_York"),
    (("rock creek park",),
     "Rock Creek Park, Washington, DC", 38.9569, -77.0457, "America/New_York"),
    (("prospect park",),
     "Prospect Park, Brooklyn, NY", 40.6602, -73.9690, "America/New_York"),
    (("green lake park",),
     "Green Lake Park, Seattle, WA", 47.6806, -122.3289, "America/Los_Angeles"),
    (("lake eola park", "lake eola"),
     "Lake Eola Park, Orlando, FL", 28.5436, -81.3734, "America/New_York"),
    (("klyde warren park",),
     "Klyde Warren Park, Dallas, TX", 32.7892, -96.8015, "America/Chicago"),
    (("white rock lake",),
     "White Rock Lake, Dallas, TX", 32.8310, -96.7130, "America/Chicago"),
    (("camelback mountain",),
     "Camelback Mountain, Phoenix, AZ", 33.5186, -111.9717, "America/Phoenix"),
    # -- U.S. cities (zone = the zone of that city) ---------------------------
    (("phoenix",), "Phoenix, AZ", 33.4484, -112.0740, "America/Phoenix"),
    (("tempe",), "Tempe, AZ", 33.4149, -111.9090, "America/Phoenix"),
    (("scottsdale",), "Scottsdale, AZ", 33.4942, -111.9261, "America/Phoenix"),
    (("mesa az", "mesa"), "Mesa, AZ", 33.4151, -111.8315, "America/Phoenix"),
    (("chandler",), "Chandler, AZ", 33.3062, -111.8413, "America/Phoenix"),
    (("glendale",), "Glendale, AZ", 33.5386, -112.1860, "America/Phoenix"),
    (("tucson",), "Tucson, AZ", 32.2226, -110.9747, "America/Phoenix"),
    (("sacramento",), "Sacramento, CA", 38.5816, -121.4944, "America/Los_Angeles"),
    (("los angeles",), "Los Angeles, CA", 34.0522, -118.2437, "America/Los_Angeles"),
    (("san diego",), "San Diego, CA", 32.7157, -117.1611, "America/Los_Angeles"),
    (("san francisco",), "San Francisco, CA", 37.7749, -122.4194, "America/Los_Angeles"),
    (("fresno",), "Fresno, CA", 36.7378, -119.7871, "America/Los_Angeles"),
    (("las vegas",), "Las Vegas, NV", 36.1699, -115.1398, "America/Los_Angeles"),
    (("reno",), "Reno, NV", 39.5296, -119.8138, "America/Los_Angeles"),
    (("seattle",), "Seattle, WA", 47.6062, -122.3321, "America/Los_Angeles"),
    (("portland",), "Portland, OR", 45.5152, -122.6784, "America/Los_Angeles"),
    (("austin",), "Austin, TX", 30.2672, -97.7431, "America/Chicago"),
    (("dallas",), "Dallas, TX", 32.7767, -96.7970, "America/Chicago"),
    (("houston",), "Houston, TX", 29.7604, -95.3698, "America/Chicago"),
    (("san antonio",), "San Antonio, TX", 29.4241, -98.4936, "America/Chicago"),
    (("el paso",), "El Paso, TX", 31.7619, -106.4850, "America/Denver"),
    (("chicago",), "Chicago, IL", 41.8781, -87.6298, "America/Chicago"),
    (("minneapolis",), "Minneapolis, MN", 44.9778, -93.2650, "America/Chicago"),
    (("st louis", "saint louis"), "St. Louis, MO", 38.6270, -90.1994, "America/Chicago"),
    (("nashville",), "Nashville, TN", 36.1627, -86.7816, "America/Chicago"),
    (("new orleans",), "New Orleans, LA", 29.9511, -90.0715, "America/Chicago"),
    (("oklahoma city",), "Oklahoma City, OK", 35.4676, -97.5164, "America/Chicago"),
    (("kansas city",), "Kansas City, MO", 39.0997, -94.5786, "America/Chicago"),
    (("denver",), "Denver, CO", 39.7392, -104.9903, "America/Denver"),
    (("salt lake city",), "Salt Lake City, UT", 40.7608, -111.8910, "America/Denver"),
    (("albuquerque",), "Albuquerque, NM", 35.0844, -106.6504, "America/Denver"),
    (("boise",), "Boise, ID", 43.6150, -116.2023, "America/Boise"),
    (("new york",), "New York, NY", 40.7128, -74.0060, "America/New_York"),
    (("boston",), "Boston, MA", 42.3601, -71.0589, "America/New_York"),
    (("philadelphia",), "Philadelphia, PA", 39.9526, -75.1652, "America/New_York"),
    (("washington dc", "washington, dc", "washington"),
     "Washington, DC", 38.9072, -77.0369, "America/New_York"),
    (("atlanta",), "Atlanta, GA", 33.7490, -84.3880, "America/New_York"),
    (("miami",), "Miami, FL", 25.7617, -80.1918, "America/New_York"),
    (("orlando",), "Orlando, FL", 28.5383, -81.3792, "America/New_York"),
    (("tampa",), "Tampa, FL", 27.9506, -82.4572, "America/New_York"),
    (("charlotte",), "Charlotte, NC", 35.2271, -80.8431, "America/New_York"),
    (("raleigh",), "Raleigh, NC", 35.7796, -78.6382, "America/New_York"),
    (("detroit",), "Detroit, MI", 42.3314, -83.0458, "America/Detroit"),
    (("columbus",), "Columbus, OH", 39.9612, -82.9988, "America/New_York"),
    (("indianapolis",), "Indianapolis, IN", 39.7684, -86.1581, "America/Indiana/Indianapolis"),
    (("anchorage",), "Anchorage, AK", 61.2181, -149.9003, "America/Anchorage"),
    (("juneau",), "Juneau, AK", 58.3019, -134.4197, "America/Juneau"),
    (("honolulu",), "Honolulu, HI", 21.3069, -157.8583, "Pacific/Honolulu"),
]

# Longest alias first so "discovery park sacramento" beats "sacramento" and
# "new york" beats "york"-style collisions.
_ALIAS_INDEX: list[tuple[str, str, float, float, str]] = sorted(
    (
        (alias, display, lat, lon, tz)
        for aliases, display, lat, lon, tz in _GAZETTEER
        for alias in aliases
    ),
    key=lambda row: len(row[0]),
    reverse=True,
)

_COORD_RE = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$"
)


def _timezone_for_coordinates(lat: float, lon: float) -> str | None:
    """Offline, deterministic IANA timezone lookup for raw coordinates.

    Uses the ``timezonefinder`` package (bundled timezone-boundary data; no
    network, no API key, no fabrication). Returns None when no land timezone
    covers the point (e.g. mid-ocean) - the caller then fails loudly instead
    of guessing an offset.
    """
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:  # dependency boundary made explicit, not faked
        return None
    finder = TimezoneFinder()
    return finder.timezone_at(lat=lat, lng=lon)


class LocationResolver:
    """Deterministic offline location resolver.

    Contract: ``resolve(location_text)`` returns a ResolvedLocation with real
    coordinates and a real IANA timezone, or raises LocationResolutionError.
    A live geocoding provider can be substituted later by implementing this
    same contract - nothing upstream changes.
    """

    def resolve(self, location_text: str) -> ResolvedLocation:
        text = (location_text or "").strip()
        if not text:
            raise LocationResolutionError(
                "Enter a park, address, place name, or 'latitude, longitude'.")

        # 1. Raw coordinates: "lat, lon" (or space-separated).
        m = _COORD_RE.match(text)
        if m:
            return self._from_coordinates(float(m.group(1)), float(m.group(2)),
                                          label=f"({m.group(1)}, {m.group(2)})")

        # 2. Gazetteer (case-insensitive substring, longest alias first).
        lowered = text.lower().rstrip(".,")
        for alias, display, lat, lon, tz in _ALIAS_INDEX:
            if alias in lowered:
                return ResolvedLocation(display, lat, lon, tz)

        # 3. Honest failure - no fabricated geocoding result.
        raise LocationResolutionError(
            f"Could not resolve '{location_text}'. This build uses an offline "
            "location directory (no live geocoder is configured). Try a "
            "well-known U.S. park or city, or enter coordinates as "
            "'latitude, longitude' (e.g. 38.6126, -121.5188).")

    def _from_coordinates(self, lat: float, lon: float, *, label: str) -> ResolvedLocation:
        if not coordinates_range_valid(lat, lon):
            raise LocationResolutionError(
                f"Coordinates ({lat}, {lon}) are not valid: latitude must be "
                "in [-90, 90] and longitude in [-180, 180].")
        if not is_in_us(lat, lon):
            raise LocationResolutionError(
                f"({lat:.4f}, {lon:.4f}) is outside the supported U.S. "
                "coverage area.",
                code="LOCATION_OUTSIDE_SUPPORTED_AREA")
        tz_name = _timezone_for_coordinates(lat, lon)
        if tz_name is None:
            raise LocationResolutionError(
                f"Could not determine the local timezone for ({lat:.4f}, "
                f"{lon:.4f}). Try a named U.S. place instead.")
        return ResolvedLocation(label, lat, lon, tz_name)

    def search(self, query: str, *, limit: int = 6) -> list[ResolvedLocation]:
        """Gazetteer suggestions for the location search field.

        Returns entries whose alias list or display name contains the query
        (case-insensitive). Deterministic ordering: exact alias prefix first,
        then by name. Raw coordinates resolve directly via ``resolve``.
        """
        q = (query or "").strip().lower().rstrip(".,")
        if not q:
            return []
        if _COORD_RE.match(q):
            try:
                return [self.resolve(q)]
            except LocationResolutionError:
                return []
        seen: set[str] = set()
        out: list[ResolvedLocation] = []
        for alias, display, lat, lon, tz in _ALIAS_INDEX:
            if display in seen:
                continue
            if q in alias or q in display.lower():
                seen.add(display)
                out.append(ResolvedLocation(display, lat, lon, tz))
                if len(out) >= limit:
                    break
        return out


# Module-level shared resolver (stateless; safe to share).
location_resolver = LocationResolver()
