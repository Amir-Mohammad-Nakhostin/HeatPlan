"""Natural-language intent extraction.

Primary path: LLM extraction when LLM_PROVIDER + key are configured.
Fallback path: deterministic regex parser. The app must remain fully usable
without an LLM (spec: deterministic fallback always available).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import httpx

from .config import settings
from .fortyguard_client import PHOENIX_TZ, now_phoenix
from .models import ParsedIntent

log = logging.getLogger("heatplan.intent")

# --- deterministic parser patterns -----------------------------------------
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b")
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b")
_FROM_TO = re.compile(r"\bfrom\s+([a-z0-9 .'-]+?)\s+to\s+([a-z0-9 .'-]+?)(?:[.,]|$)")

# Small built-in gazetteer (FortyGuard coverage is U.S.-only).
_GAZETTEER = {
    "phoenix": (33.4484, -112.0740, "Phoenix, AZ"),
    "tempe": (33.4149, -111.9090, "Tempe, AZ"),
    "scottsdale": (33.4942, -111.9261, "Scottsdale, AZ"),
    "mesa": (33.4151, -111.8315, "Mesa, AZ"),
    "chandler": (33.3062, -111.8413, "Chandler, AZ"),
    "glendale": (33.5386, -112.1860, "Glendale, AZ"),
    "tucson": (32.2226, -110.9747, "Tucson, AZ"),
    "las vegas": (36.1699, -115.1398, "Las Vegas, NV"),
    "austin": (30.2672, -97.7431, "Austin, TX"),
    "dallas": (32.7767, -96.7970, "Dallas, TX"),
    "miami": (25.7617, -80.1918, "Miami, FL"),
    "new york": (40.7128, -74.0060, "New York, NY"),
    "los angeles": (34.0522, -118.2437, "Los Angeles, CA"),
    "chicago": (41.8781, -87.6298, "Chicago, IL"),
    "houston": (29.7604, -95.3698, "Houston, TX"),
    "denver": (39.7392, -104.9903, "Denver, CO"),
    "seattle": (47.6062, -122.3321, "Seattle, WA"),
}

_TASK_KEYWORDS = (
    ("work outdoors", "outdoor work"),
    ("work outside", "outdoor work"),
    ("outdoor work", "outdoor work"),
    ("commute", "walking commute"),
    ("walking", "walking"),
    ("walk", "walking"),
    ("running", "running"),
    ("run", "running"),
    ("jog", "running"),
    ("cycling", "cycling"),
    ("cycle", "cycling"),
    ("bike", "cycling"),
    ("errands", "errands"),
    ("errand", "errands"),
    ("yard", "outdoor maintenance"),
    ("garden", "outdoor maintenance"),
    ("maintenance", "outdoor maintenance"),
    ("deliver", "deliveries"),
    ("exercise", "exercise"),
    ("workout", "exercise"),
)


def _locate(text: str) -> tuple[float, float, str] | None:
    lowered = text.lower().strip().rstrip(".,")
    for name, (lat, lon, display) in _GAZETTEER.items():
        if name in lowered:
            return lat, lon, display
    return None


def fallback_parse(text: str, date: str, lat: float | None,
                   lon: float | None) -> ParsedIntent:
    """Deterministic regex intent parser - always available, reproducible."""
    t = text.lower()
    task = next((name for kw, name in _TASK_KEYWORDS if kw in t), "outdoor activity")
    warnings: list[str] = []

    # Duration (remove the matched span so it cannot be misread as a time).
    m = _DURATION.search(t)
    if m:
        value = float(m.group(1))
        unit = m.group(2)
        duration = int(round(value * 60 if unit.startswith("h") else value))
        t_wo_duration = _DURATION.sub(" ", t)
    else:
        duration = 60
        warnings.append("No duration detected; defaulted to 60 minutes.")
        t_wo_duration = t

    # Start time ("starting at 2 PM", "at 6 am", "at 15:30").
    m2 = _TIME.search(t_wo_duration)
    start_time = "14:00"
    if m2:
        hh = int(m2.group(1))
        mm = int(m2.group(2) or 0)
        ampm = (m2.group(3) or "").lower()
        if ampm:
            if ampm.startswith("a") and hh == 12:
                hh = 0
            if ampm.startswith("p") and hh < 12:
                hh += 12
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            start_time = f"{hh:02d}:{mm:02d}"
        else:
            # Never clamp a malformed clock time into a wrong-but-plausible
            # one (e.g. "99:99" must not become "23:99" and crash the
            # planner, nor "23:00" and silently shift the plan).
            warnings.append(
                f"Time '{m2.group(0).strip()}' is not a valid clock time; "
                "defaulted to 14:00.")

    # Route: "from X to Y".
    route_m = _FROM_TO.search(t)
    task_type = "route" if route_m else "stationary"
    end_lat = end_lon = None
    end_location_name = None

    # Location: explicit coords > gazetteer > default city.
    if lat is not None and lon is not None:
        location_name = "Selected location"
    else:
        found = _locate(text)
        if found:
            lat, lon, location_name = found
        else:
            lat, lon = settings.default_city_lat, settings.default_city_lon
            location_name = settings.default_city_name
            warnings.append("No location detected; defaulted to Phoenix, AZ.")

    if route_m:
        dst = _locate(route_m.group(2))
        src = _locate(route_m.group(1))
        if dst:
            end_lat, end_lon, end_location_name = dst
        if src:
            lat, lon, location_name = src
        else:
            task_type = "route" if end_lat is not None else "stationary"

    # Heat priority.
    if any(k in t for k in ("coolest", "cooler", "cool", "heat", "shade")):
        heat_priority = "high"
    elif any(k in t for k in ("fastest", "quick", "speed", "urgent")):
        heat_priority = "low"
    else:
        heat_priority = "medium"

    return ParsedIntent(
        task=task,
        task_type=task_type,
        duration_minutes=duration,
        start_time=start_time,
        date=date,
        location_name=location_name,
        lat=lat or 0.0,
        lon=lon or 0.0,
        end_lat=end_lat,
        end_lon=end_lon,
        end_location_name=end_location_name,
        heat_priority=heat_priority,
        parser="fallback",
        warnings=warnings,
    )


_LLM_SYSTEM = (
    "Extract outdoor-task constraints from the user's sentence. Respond with "
    "JSON only, keys: task (string), duration_minutes (int), start_time (HH:MM "
    "24h, default 14:00), heat_priority (low|medium|high), location (place "
    "name), end_location (place name, only for routes from X to Y). Do not "
    "invent values that are not implied by the sentence."
)


async def llm_parse(text: str) -> dict | None:
    """LLM intent extraction. Returns None on any failure (caller falls back)."""
    if settings.llm_provider == "openai" and settings.openai_api_key:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
        payload = {
            "model": "gpt-4o-mini",
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": text},
            ],
        }
    elif settings.llm_provider == "anthropic" and settings.anthropic_api_key:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": settings.anthropic_api_key,
                   "anthropic-version": "2023-06-01"}
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": _LLM_SYSTEM,
            "messages": [{"role": "user", "content": text}],
        }
    else:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            if settings.llm_provider == "openai":
                content = resp.json()["choices"][0]["message"]["content"]
            else:
                content = resp.json()["content"][0]["text"]
            return json.loads(content)
    except Exception as e:  # noqa: BLE001 - LLM is optional; fall back
        log.warning("LLM intent parsing failed, using fallback parser: %s",
                    type(e).__name__)
        return None


async def parse_intent(text: str, date: str | None, lat: float | None,
                       lon: float | None) -> ParsedIntent:
    """Resolve the user's sentence into a ParsedIntent.

    Consistency contract: explicit `lat`/`lon` and `date` from the API
    request take precedence in BOTH modes (LLM and deterministic fallback),
    so enabling an LLM provider never changes the coordinates or date a
    request resolves to. The LLM only extracts task/duration/time/priority
    and place-name classification; it never supplies or overrides numbers.
    """
    if date is None:
        date = now_phoenix().strftime("%Y-%m-%d")

    intent = None
    if settings.llm_provider and (settings.openai_api_key or settings.anthropic_api_key):
        data = await llm_parse(text)
        if isinstance(data, dict):
            try:
                intent = ParsedIntent(parser="llm")
                intent.task = str(data.get("task") or "outdoor activity")
                intent.duration_minutes = max(5, min(720, int(data.get("duration_minutes", 60))))
                start_time = str(data.get("start_time") or "14:00")
                if re.fullmatch(r"\d{1,2}:\d{2}", start_time):
                    hh, mm = (int(x) for x in start_time.split(":"))
                    if 0 <= hh <= 23 and 0 <= mm <= 59:
                        intent.start_time = start_time
                hp = str(data.get("heat_priority") or "medium").lower()
                intent.heat_priority = hp if hp in ("low", "medium", "high") else "medium"
                intent.date = date  # same date semantics as the fallback parser
                loc = data.get("location")
                if isinstance(loc, str) and loc:
                    found = _locate(loc)
                    if found:
                        intent.lat, intent.lon, intent.location_name = found
                end_loc = data.get("end_location")
                if isinstance(end_loc, str) and end_loc:
                    found = _locate(end_loc)
                    if found:
                        intent.task_type = "route"
                        intent.end_lat, intent.end_lon, intent.end_location_name = found
                if intent.lat == 0.0 and intent.lon == 0.0:
                    intent.lat, intent.lon = settings.default_city_lat, settings.default_city_lon
                    intent.location_name = settings.default_city_name
                # Explicit caller coordinates ALWAYS take precedence over
                # anything the LLM extracted: the same API request must
                # resolve to the same location whether or not an LLM
                # provider is enabled. The LLM may classify language; it may
                # never override user-supplied coordinates.
                if lat is not None and lon is not None:
                    intent.lat, intent.lon = lat, lon
                    intent.location_name = "Selected location"
            except (ValueError, TypeError):
                log.warning("LLM output invalid, using fallback parse.")
                intent = None

    if intent is None:
        intent = fallback_parse(text, date, lat, lon)
    return intent
