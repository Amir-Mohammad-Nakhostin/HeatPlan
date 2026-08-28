import os
import json
import time
import requests

API_KEY = os.environ.get("FORTYGUARD_API_KEY")

if not API_KEY:
    from app.config import settings
    API_KEY = settings.fortyguard_api_key

HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}

SUBMIT_URL = "https://api.fortyguard.com/v1/heatmap"
STATUS_URL = "https://api.fortyguard.com/v1/status"

payload = {
    "polygon_aoi": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-111.957, 33.442],
                        [-111.937, 33.442],
                        [-111.937, 33.462],
                        [-111.957, 33.462],
                        [-111.957, 33.442]
                    ]]
                }
            }
        ]
    },
    "date_time": {
        "start_date": "2026-08-28",
        "start_time": "13:00",
        "end_time": "17:00",
        "filter_type": 2
    },
    "granularity": 100,
    "analytic_type": "tcm",
}

print("=== SUBMIT RANGE ===")

r = requests.post(
    SUBMIT_URL,
    headers=HEADERS,
    json=payload,
    timeout=60,
)

print("HTTP:", r.status_code)
print(r.text)

r.raise_for_status()

activity_id = r.json()["data"]["activity_id"]

print("ACTIVITY:", activity_id)

print("\n=== POLLING ===")

result = None

for i in range(60):
    time.sleep(5)

    r = requests.get(
        f"{STATUS_URL}/{activity_id}",
        headers={"api-key": API_KEY},
        timeout=60,
    )

    r.raise_for_status()

    data = r.json()["data"]
    status = data.get("status")

    print(f"poll={i + 1} status={status}")

    if str(status).lower() == "completed":
        result = data["result"]
        break

    if str(status).lower() in ("failed", "error"):
        print(json.dumps(data, indent=2))
        raise SystemExit(1)

if result is None:
    raise TimeoutError("Timed out waiting for FortyGuard.")

print("\n=== RESULT STRUCTURE ===")
print("result keys:", list(result.keys()))

map_data = result.get("map_data", {})
stats_data = result.get("stats_data", {})

print("\n=== STATS KEYS ===")
print(list(stats_data.keys()))

print("\n=== MAP DATA ===")
print("type:", map_data.get("type"))

features = map_data.get("features", [])

print("feature count:", len(features))

if features:
    print("\n=== FIRST FEATURE ===")
    print(json.dumps(features[0], indent=2))

print("\n=== SEARCHING FOR TIME-RELATED FIELDS ===")

time_keywords = (
    "time",
    "timestamp",
    "date",
    "hour",
    "start",
    "end",
)

found = []

def walk(obj, path="root"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).lower()

            if any(word in key_lower for word in time_keywords):
                found.append((path + "." + str(key), value))

            walk(value, path + "." + str(key))

    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            walk(value, path + f"[{index}]")

walk(result)

if found:
    for path, value in found:
        print("\nPATH:", path)
        print("VALUE:", json.dumps(value, indent=2)[:3000])
else:
    print("No time-related fields found.")

print("\n=== TEMPERATURE SAMPLE ===")

temps = []

for feature in features:
    props = feature.get("properties", {})

    if isinstance(props.get("average_temperature"), (int, float)):
        temps.append(props["average_temperature"])

print("number of temperature tiles:", len(temps))

if temps:
    print("first 10:", temps[:10])
    print("min:", min(temps))
    print("max:", max(temps))
    print("mean:", sum(temps) / len(temps))

print("\n=== DONE ===")
        