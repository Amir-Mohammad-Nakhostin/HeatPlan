# HeatPlan route-duration contract

This document formalizes how HeatPlan's route pipeline treats "duration".
It is prescriptive: the four quantities below are distinct and must not be
conflated.

## Definitions

### A. USER REQUESTED DURATION

The number of minutes the user says they want to spend on the activity.
Comes from natural-language intent parsing and is stored as
`ParsedIntent.duration_minutes`. This is the *activity* budget; it is NOT
the estimated walking time of any specific route.

### B. ACTUAL ROUTE DURATION

For a given candidate route geometry (`path`), the estimated time required
to *traverse* that geometry:

    actual_route_duration_minutes = _path_m(path) / WALK_SPEED_M_S / 60

`_path_m` sums the haversine distance of EVERY segment of the polyline in
order — a waypoint route A→C→B is measured as A→C plus C→B, never the
straight A→B chord. `WALK_SPEED_M_S` is the single configured walking
speed constant in `app/planner.py` (currently 1.4 m/s ≈ 5 km/h).

### C. THERMAL COVERAGE

The set of hourly FortyGuard grids required for the candidate to be
feasible. For a candidate starting at Phoenix-local `start` with ACTUAL
route duration `dur`, the required timestamps are:

    required_hourly_timestamps(start, ceil(dur_minutes))

i.e. every hour boundary the walker is actually inside during the walk
(see `app/thermal.py`). A candidate is *feasible* only when EVERY
required timestamp has USABLE live thermal data (`candidate_feasible`).
An UNAVAILABLE or OUTSIDE_HORIZON timestamp makes the candidate infeasible
— we never score partial thermal coverage, and we never substitute the
grid of a different hour.

### D. EXPOSURE

The temperature-based estimated thermal burden accumulated over the
ACTUAL route duration. `_sample_path(path, start, dur, grids)` samples
the candidate along its true polyline at ~100 m spacing; each sample uses
the grid of the EXACT hour the walker is in at that point (`_grid_for_hour`
raises `KeyError` if that hour's grid is not in `grids`, which means the
candidate is infeasible and is never scored). `exposure(...)` sums each
sample's per-minute heat burden.

Exposure is an estimated *temperature-based* burden; it is NOT an
apparent-temperature index, physiological simulation, or medical risk
prediction. It is decision-support for choosing between three offered
plans.

## Interactions between the four quantities

| Case | A vs B | Behavior |
|------|--------|----------|
| A > B | User's activity budget exceeds walking time | Candidate is scored on its ACTUAL walking duration (B). We do not stretch the walk to fill A. Requested duration still gates the user's *own* requested interval (A resolution) in the reported error case. |
| A < B | User's activity budget is shorter than walking time | Candidate is scored on B; thermal coverage is validated for B, not A. If B's coverage is missing the candidate is INFEASIBLE (never scored on hour-of-A only). |
| A == B | Nominal | Same as either case above. |
| Route crosses an hour boundary | Coverage must include EVERY hour touched by [start, start + B]. Each sample uses the grid of its own hour; no fallback to the start hour. |
| Route spans multiple hourly grids | Same as above — coverage list is the full hourly walk of B. |
| Route crosses midnight / date rollover | Same rule; `required_hourly_timestamps` correctly rolls the date via `timedelta` on the Phoenix-aware datetime, and grids are keyed by top-of-hour `datetime` (see `_hour_key`) so the rollover hour is a distinct cache identity from the same hour on the previous day. |

## Requested-interval reporting vs candidate feasibility

The requested-interval resolution (`requested_result`) is computed against
the USER REQUESTED DURATION (A). It is used only for the user-facing
error message when the requested interval itself is unsupported (the
"REQUESTED vs SEARCH SPACE" separation in `app/planner.py`), and it is
never used to score candidates.

Candidate feasibility is computed against the ACTUAL ROUTE DURATION (B).
This is why a user who asks for a 60-minute walk may see a "Coolest"
candidate that is 75 minutes long, or may see that candidate excluded as
infeasible when hour 15 is UNAVAILABLE even though the requested 60-minute
interval (which alone would only require hour 14) is supported.

## Invariants (what the pipeline MUST NOT do)

The following are explicit anti-behaviors — a change that violates any of
these would be a correctness regression:

1. Never validate a candidate against the requested duration alone if its
   actual duration exceeds it. (Covered by
   `test_long_candidate_requires_thermal_coverage_for_its_actual_duration`.)
2. Never score exposure on partial thermal coverage. If any required hour
   is UNAVAILABLE or OUTSIDE_HORIZON the candidate is dropped
   (`candidate_feasible` returns False and it is excluded).
   (Covered by
   `test_all_candidates_too_long_raise_interval_unsupported`.)
3. Never substitute another hour's grid at a sample. `_grid_for_hour` is
   an exact-hour lookup; a missing grid raises `KeyError`.
   (Covered by
   `test_grid_for_hour_never_substitutes_another_hour` and
   `test_sample_path_missing_later_hour_raises_instead_of_reusing_start_hour`.)
4. Never force the route geometry to match the requested duration by
   stretching or shortening the polyline. The route's actual duration
   comes from its real distance.
5. Never conflate the activity duration (A) with the walking duration
   (B) in exposure. Exposure is always over B; A is only used for
   requested-interval error reporting.
