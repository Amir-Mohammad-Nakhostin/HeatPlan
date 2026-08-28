"""
Thermal-time architecture: rolling forecast horizon vs hourly requests.

AUTHORITATIVE MODEL
-------------------

FortyGuard's forecast availability is a rolling window of approximately
+12 hours from the current time.

Important distinction:

1. FORECAST HORIZON
   Phoenix local time:
       now .. now + 12h

2. HOURLY HEATMAP REQUESTS
   filter_type=1 is a Single Hour request.
   Each requested hourly timestamp is sent as its own request.

3. USABLE THERMAL TIMESTAMPS
   A requested timestamp is usable only when FortyGuard returns usable
   spatial tiles.

Each required timestamp is classified as exactly one of:

    USABLE
        Timestamp is inside the rolling horizon and the live response
        contains usable spatial tiles.

    UNAVAILABLE
        Timestamp is inside the rolling horizon, but FortyGuard completed
        the request with no usable spatial tiles.

    OUTSIDE_HORIZON
        Timestamp itself is outside the rolling forecast horizon.
        No FortyGuard request is sent.

IMPORTANT BOUNDARY RULE
-----------------------

For filter_type=1, the timestamp identifies the requested hour.

Therefore if:

    horizon = 10:00 .. 22:00

then:

    22:00 -> allowed
    23:00 -> outside horizon

We do NOT require the implicit one-hour interval after 22:00 to finish
before the horizon boundary.

This matches FortyGuard's documented Single Hour semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Awaitable, Callable

from .fortyguard_client import (
    MAX_FUTURE_HOURS,
    PHOENIX_TZ,
    HeatGrid,
    ThermalDataUnavailableError,
    forecast_horizon,
)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

FetchHour = Callable[
    [float, float, datetime],
    Awaitable[HeatGrid],
]


class TimestampStatus(str, Enum):
    """
    Tri-state classification for every required thermal timestamp.
    """

    USABLE = "usable"

    UNAVAILABLE = "unavailable"

    OUTSIDE_HORIZON = "outside_horizon"


@dataclass
class TimestampCheck:
    """
    Result of checking one required hourly timestamp.
    """

    timestamp: datetime
    status: TimestampStatus
    detail: str = ""


@dataclass
class ThermalIntervalResult:
    """
    Complete resolution result for one candidate activity interval.
    """

    horizon_start: datetime
    horizon_end: datetime

    required: list[datetime] = field(default_factory=list)

    checks: list[TimestampCheck] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """
        The interval is feasible only if every required timestamp
        has usable live thermal data.
        """

        return (
            bool(self.required)
            and len(self.checks) == len(self.required)
            and all(
                check.status is TimestampStatus.USABLE
                for check in self.checks
            )
        )

    @property
    def has_unavailable(self) -> bool:
        """
        True when at least one required timestamp is inside the horizon
        but has no usable spatial result.
        """

        return any(
            check.status is TimestampStatus.UNAVAILABLE
            for check in self.checks
        )

    @property
    def has_outside_horizon(self) -> bool:
        """
        True when at least one required timestamp lies outside
        the rolling forecast horizon.
        """

        return any(
            check.status is TimestampStatus.OUTSIDE_HORIZON
            for check in self.checks
        )

    def summary(self) -> str:
        """
        Human-readable diagnostic summary.
        """

        required_text = [
            timestamp.strftime("%Y-%m-%d %H:%M")
            for timestamp in self.required
        ]

        checks_text = ", ".join(
            f"{check.timestamp:%Y-%m-%d %H:%M}={check.status.value}"
            for check in self.checks
        )

        return (
            f"horizon "
            f"{self.horizon_start:%Y-%m-%d %H:%M}"
            f" -> "
            f"{self.horizon_end:%Y-%m-%d %H:%M}"
            f" | required={required_text}"
            f" | {checks_text}"
        )


# ---------------------------------------------------------------------------
# Time normalization
# ---------------------------------------------------------------------------

def _ensure_phoenix(dt: datetime) -> datetime:
    """
    Normalize a datetime to Phoenix local time.

    Naive datetimes are interpreted as Phoenix local time.

    Aware datetimes are converted using zoneinfo.
    """

    if dt.tzinfo is None:
        return dt.replace(tzinfo=PHOENIX_TZ)

    return dt.astimezone(PHOENIX_TZ)


# ---------------------------------------------------------------------------
# Required hourly timestamps
# ---------------------------------------------------------------------------

def required_hourly_timestamps(
    start_local: datetime,
    duration_minutes: int,
) -> list[datetime]:
    """
    Determine which hourly thermal snapshots are required.

    Activity interval is treated as half-open:

        [start, start + duration)

    The hour containing the start is always required.

    Every subsequent top-of-hour timestamp is required only when the
    activity actually enters that hour.

    Examples
    --------

    start = 14:00

    30 min:
        [14:00]

    60 min:
        [14:00]

    90 min:
        [14:00, 15:00]

    120 min:
        [14:00, 15:00]

    150 min:
        [14:00, 15:00, 16:00]


    start = 14:30

    30 min:
        [14:00]

    60 min:
        [14:00, 15:00]

    90 min:
        [14:00, 15:00]
    """

    start = _ensure_phoenix(start_local)

    duration = max(1, int(duration_minutes))

    end = start + timedelta(minutes=duration)

    first_hour = start.replace(
        minute=0,
        second=0,
        microsecond=0,
    )

    required: list[datetime] = [
        first_hour
    ]

    next_hour = first_hour + timedelta(hours=1)

    while next_hour < end:
        required.append(next_hour)
        next_hour += timedelta(hours=1)

    return required


# ---------------------------------------------------------------------------
# Horizon validation
# ---------------------------------------------------------------------------

def timestamp_within_horizon(
    ts: datetime,
    h_start: datetime,
    h_end: datetime,
) -> bool:
    """
    Return True when the requested hourly timestamp itself is inside
    the rolling forecast horizon.

    IMPORTANT:

    FortyGuard filter_type=1 is a Single Hour request.

    The timestamp is the identifier for the requested hour.

    We therefore check the timestamp itself, NOT:

        timestamp + 1 hour

    Example:

        horizon:
            10:00 -> 22:00

        22:00:
            allowed

        23:00:
            rejected

    The boundary is inclusive because the 22:00 hourly request is still
    exactly at the end of the documented rolling horizon.
    """

    ts = _ensure_phoenix(ts)
    h_start = _ensure_phoenix(h_start)
    h_end = _ensure_phoenix(h_end)

    return h_start <= ts <= h_end


# ---------------------------------------------------------------------------
# Forecast horizon
# ---------------------------------------------------------------------------

def determine_forecast_horizon(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """
    Return the current rolling FortyGuard forecast horizon.

    Delegates to the authoritative implementation in
    fortyguard_client.py.
    """

    return forecast_horizon(now)


# ---------------------------------------------------------------------------
# Main thermal resolver
# ---------------------------------------------------------------------------

async def resolve_thermal_interval(
    lat: float,
    lon: float,
    start_local: datetime,
    duration_minutes: int,
    fetch: FetchHour,
    now: datetime | None = None,
) -> ThermalIntervalResult:
    """
    Resolve one candidate activity interval.

    Pipeline:

        Layer 1:
            determine rolling forecast horizon

        Layer 2:
            derive required hourly timestamps

        Layer 3:
            classify each required timestamp

    OUTSIDE_HORIZON timestamps are never fetched.

    UNAVAILABLE timestamps are fetched because they are inside the horizon,
    but FortyGuard returned no usable spatial data.

    No missing thermal data is fabricated.
    """

    # ------------------------------------------------------------------
    # Layer 1: rolling forecast horizon
    # ------------------------------------------------------------------

    horizon_start, horizon_end = determine_forecast_horizon(now)

    # ------------------------------------------------------------------
    # Layer 2: required hourly timestamps
    # ------------------------------------------------------------------

    start = _ensure_phoenix(start_local)

    required = required_hourly_timestamps(
        start,
        duration_minutes,
    )

    checks: list[TimestampCheck] = []

    # ------------------------------------------------------------------
    # Layer 3: fetch and classify each required timestamp
    # ------------------------------------------------------------------

    for timestamp in required:

        # --------------------------------------------------------------
        # Horizon gate
        #
        # IMPORTANT:
        # Check the timestamp itself.
        #
        # Do NOT require timestamp + 1 hour <= horizon_end.
        # --------------------------------------------------------------

        if not timestamp_within_horizon(
            timestamp,
            horizon_start,
            horizon_end,
        ):
            checks.append(
                TimestampCheck(
                    timestamp=timestamp,
                    status=TimestampStatus.OUTSIDE_HORIZON,
                    detail=(
                        f"timestamp is beyond the rolling "
                        f"+{MAX_FUTURE_HOURS}h forecast horizon "
                        f"ending at "
                        f"{horizon_end:%Y-%m-%d %H:%M}; "
                        f"request not sent"
                    ),
                )
            )

            continue

        # --------------------------------------------------------------
        # Timestamp is inside horizon.
        #
        # Now and ONLY now do we request FortyGuard data.
        # --------------------------------------------------------------

        try:

            grid = await fetch(
                lat,
                lon,
                timestamp,
            )

        except ThermalDataUnavailableError:

            checks.append(
                TimestampCheck(
                    timestamp=timestamp,
                    status=TimestampStatus.UNAVAILABLE,
                    detail=(
                        "FortyGuard completed the request but returned "
                        "no usable spatial result "
                        "(map_data.features == [] / n_cells == 0)"
                    ),
                )
            )

            continue

        # --------------------------------------------------------------
        # Defensive validation.
        #
        # A fetch implementation must return a grid with thermal samples.
        # --------------------------------------------------------------

        if (
            grid is None
            or not getattr(grid, "temps_f", None)
        ):

            checks.append(
                TimestampCheck(
                    timestamp=timestamp,
                    status=TimestampStatus.UNAVAILABLE,
                    detail=(
                        "FortyGuard returned an empty thermal grid"
                    ),
                )
            )

            continue

        # --------------------------------------------------------------
        # Successfully usable
        # --------------------------------------------------------------

        checks.append(
            TimestampCheck(
                timestamp=timestamp,
                status=TimestampStatus.USABLE,
                detail=(
                    f"{len(grid.temps_f)} tile samples"
                ),
            )
        )

    # ------------------------------------------------------------------
    # Return complete interval result
    # ------------------------------------------------------------------

    return ThermalIntervalResult(
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        required=required,
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Planner gate
# ---------------------------------------------------------------------------

def candidate_feasible(
    result: ThermalIntervalResult,
) -> bool:
    """
    Planner gate.

    A candidate is feasible ONLY when every required thermal timestamp
    is usable.

    Therefore:

        all USABLE
            -> True

        USABLE + UNAVAILABLE
            -> False

        USABLE + OUTSIDE_HORIZON
            -> False

        UNAVAILABLE + OUTSIDE_HORIZON
            -> False

        empty required list
            -> False
    """

    return result.valid