# HeatPlan

HeatPlan finds a better time to be outside. You give it four structured
answers — **when**, **how long**, **what time window**, and **where** — and it
returns the best time slot plus the other available times in your window,
ranked by estimated thermal exposure from live FortyGuard temperature data.

No natural-language sentence is required, and no LLM decides anything about
your plan: every step from validation to ranking is deterministic.

## Structured input architecture (primary workflow)

```
STRUCTURED USER INPUT          date, duration, available window, location
  -> DETERMINISTIC VALIDATION  (no LLM; invalid input never costs API calls)
  -> TIME / HORIZON RESOLUTION (destination-local IANA timezone, zoneinfo)
  -> LOCATION RESOLUTION       (deterministic LocationResolver -> lat/lon/tz)
  -> FORECAST-HORIZON GATE     (rolling +12h window, checked BEFORE any
                                FortyGuard request - never partial submits)
  -> THERMAL DATA REQUEST      (one heatmap per UNIQUE required hour,
                                request-scoped + TTL caches)
  -> THERMAL ANALYSIS          (existing v1 exposure model, exact-hour grids)
  -> RESULT                    (best time + alternatives, deterministic rank)
```

### API contract

`POST /api/plan/structured`

```json
{
  "date": "2026-08-28",
  "duration_minutes": 60,
  "available_start": "13:00",
  "available_end": "17:00",
  "location": { "name": "Discovery Park", "latitude": 38.6126, "longitude": -121.5188 }
}
```

`location` may be replaced by `"location_text": "Discovery Park"`; the backend
resolves it through the same deterministic `LocationResolver`. Successful
responses carry `best` + `alternatives` (each with start/end, estimated
temperature, estimated thermal exposure, and a deterministic backend-derived
`reason` explaining the ranking - computed facts only, never LLM-invented),
the resolved location/timezone, and the disclaimer.
`GET /api/locations/search?q=...` returns deterministic location suggestions
for the picker. `GET /api/forecast/window` is a pre-flight endpoint: it
resolves the location, expresses the rolling +12h horizon in the
destination's local wall-clock time (`horizon_display`, e.g. "10:00 AM –
9:45 PM"), and - when a candidate window is supplied - runs the same
deterministic date/window/horizon validation as the planning endpoint,
WITHOUT submitting anything to FortyGuard. The UI uses it to flag an
out-of-horizon window before any thermal request is made.

The legacy `POST /api/plan` (natural language) endpoint is retained for
backward compatibility only; the frontend uses the structured endpoint. If
natural-language assistance is ever added back, it may only *suggest*
structured fields for user confirmation — the deterministic planner stays
authoritative.

### Error codes (concise, user-facing; 422 unless noted)

| Code | Meaning |
| --- | --- |
| `INVALID_LOCATION` | location text could not be resolved |
| `LOCATION_OUTSIDE_SUPPORTED_AREA` | coordinates outside the U.S. polygon |
| `INVALID_DATE` | malformed / impossible date |
| `INVALID_DURATION` | duration ≤ 0 |
| `INVALID_TIME_WINDOW` | end ≤ start, or duration > window |
| `FORECAST_HORIZON_EXCEEDED` | requested window reaches beyond the rolling +12h horizon (includes horizon start/end and the first unavailable hour; rejected **before** any FortyGuard request) |
| `NO_FEASIBLE_ALTERNATIVE` | every candidate hour lacks usable live thermal data (nothing fabricated) |
| `FORTYGUARD_ERROR` | live API failure (HTTP 502) |

Internal diagnostics, traces, activity ids, signed URLs, and raw FortyGuard
payloads never appear in user-facing responses.

## Date, time & timezone semantics

- All user-selected times are **local wall-clock times at the destination**.
  The `LocationResolver` returns the destination's IANA zone (e.g.
  `America/Phoenix`, `America/Los_Angeles`); times are stamped with
  `zoneinfo` — never the developer machine's timezone, never a hard-coded
  UTC offset (DST-safe).
- `date` is the destination-local calendar date (YYYY-MM-DD).
- The existing Phoenix/FortyGuard assumption is preserved and explicit:
  `FORTYGUARD_STARTTIME_TZ = "local_to_aoi"` — the hour submitted to
  FortyGuard is the destination-local hour of each timestamp.

## Forecast horizon behavior

FortyGuard forecast heatmaps cover approximately **+12 hours from now** (a
rolling window, not a fixed daily window, and not twelve pre-fetched grids).
Every hourly timestamp a candidate traverses is checked against this window
**before** any request is submitted. If any required hour is beyond it, the
request fails with `FORECAST_HORIZON_EXCEEDED` (never partially submitted,
never re-labeled as `THERMAL_INTERVAL_UNSUPPORTED`). Each required hour is
then classified:

- **USABLE** — in horizon, live response has spatial tiles
- **UNAVAILABLE** — in horizon, but `map_data.features == []` (a per-hour
  data gap; never fabricated, never borrowed from another hour/location)
- **OUTSIDE_HORIZON** — beyond the rolling window (never requested)

## Location validation

Location resolution is deterministic and offline (`app/location.py`): a
curated U.S. gazetteer of parks/cities, plus raw `lat, lon` input resolved to
a real IANA timezone via the bundled `timezonefinder` dataset. **No live
geocoding provider is configured in this build**, and none is faked —
unresolvable text fails with `INVALID_LOCATION`. To add a real geocoder,
implement the `LocationResolver.resolve` contract; nothing upstream changes.

The existing offline U.S. polygon gate (`app/us_geo.py`) remains active:
locations outside the 50-state coverage are rejected **before** any
FortyGuard submission.

## FortyGuard integration (preserved)

- `POST /v1/heatmap` with `filter_type=1` (exactly one hour), `granularity=100`,
  a small polygon AOI around the resolved coordinate → `activity_id`.
- Bounded polling of `GET /v1/status/{activity_id}` (deadline + observability)
  until `Completed` / `Failed` / timeout.
- Only **unique required hours** are requested: candidates 13→14 … 16→17
  (60 min) trigger exactly four thermal requests (hours 13, 14, 15, 16).
  The request-scoped cache keys on (location @6dp, date, hour); a global TTL
  cache sits underneath. Different locations/hours/requests never share a
  result.
- Live parsing is strict: `map_data` FeatureCollection →
  `properties.average_temperature` (°C → °F). Generic `temperature`/`temp`
  fields are never silently treated as Celsius on the live path.
- `filter_type=2` remains deliberately unused (see the decision record in
  `app/fortyguard_client.py`): per-hour activity identity and per-hour
  emptiness cannot be recovered from the documented range response.
- Credits: nothing is submitted when location/date/window/duration/horizon
  validation fails, so invalid requests never consume FortyGuard credits.

## Thermal model — honest terminology

The raw FortyGuard heatmap value (`average_temperature`) is a **tile
temperature in °C**. HeatPlan converts it to °F and computes a
**temperature-based thermal burden estimate**:

```
heat_burden(T) = max(0, T°F - 65) ^ 1.25
exposure       = Σ heat_burden(T_i) × minutes_i      [burden·min]
```

This is **not** an apparent-temperature or physiological metric (no
humidity/wind/radiation/shade model is applied), it is **not medically
validated**, and it is **decision support, not medical advice, and not a
safety guarantee**.

## Route-duration contract (preserved)

The four canonical quantities remain distinct (see
`backend/docs/route_duration_contract.md`): **A** user requested duration,
**B** actual route duration (geometry ÷ walk speed), **C** thermal coverage
(hours required by B), **D** exposure (computed over B, exact-hour grids).
The structured MVP's primary workflow is stationary/location-based; route
mode and its tests are untouched.

## Running

Backend:

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload          # mock data when no API key is set
```

Frontend:

```bash
cd frontend
npm ci
npm run dev
```

The FortyGuard API key is server-side only, read from the
`FORTYGUARD_API_KEY` environment variable (never committed, never sent to the
frontend, never logged). Without a key the backend serves clearly-labeled
mock data. There is no `.env` file in this repository; `FORTYGUARD_MOCK=false`
plus a real key in the environment enables live mode.

## Tests

```bash
cd backend && python -m pytest tests/ -v     # unit + API tests (no network)
cd frontend && npm test && npm run build && npx tsc --noEmit
```

Live FortyGuard e2e tests (`tests/test_live_e2e.py`) are skipped unless a
real `FORTYGUARD_API_KEY` is configured. The structured live test resolves a
real U.S. location, derives a window inside the rolling +12h horizon from the
destination's local clock, submits one heatmap per unique required hour,
polls each activity to `Completed`, and verifies `map_data` /
`average_temperature` end-to-end.
