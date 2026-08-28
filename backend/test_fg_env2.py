import requests
import time
import json
import os

from app.config import settings

API_KEY = settings.fortyguard_api_key

LAT = 33.4521
LON = -111.947
DATE = "2026-08-28"
TIME = "13:00"

headers = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}

payload = {
    "latitude": LAT,
    "longitude": LON,

    # Required by FortyGuard.
    # We use the actual ambient temperature for the requested location/time.
    "temperature": 40.0,

    "date_time": {
        "start_date": DATE,
        "start_time": TIME,
        "filter_type": 1,
    },

    "analysis": [
        "heat_index_celsius",
        "apparent_temperature_celsius",
        "relative_humidity_percent",
    ],
}

print("=== REQUEST ===")
print(json.dumps(payload, indent=2))

r = requests.post(
    "https://api.fortyguard.com/v1/env_params",
    headers=headers,
    json=payload,
    timeout=60,
)

print("\n=== SUBMIT ===")
print("HTTP:", r.status_code)
print(r.text)

r.raise_for_status()

activity_id = r.json()["data"]["activity_id"]
print("\nACTIVITY:", activity_id)

for i in range(60):
    r = requests.get(
        f"https://api.fortyguard.com/v1/status/{activity_id}",
        headers={"api-key": API_KEY},
        timeout=30,
    )

    data = r.json()["data"]
    status = str(data.get("status", "")).lower()

    print(f"poll={i+1} status={status}")

    if status == "completed":
        result = data["result"]

        print("\n=== METADATA ===")
        print(json.dumps(result.get("metadata"), indent=2))

        print("\n=== LOCATIONS ===")
        print(json.dumps(result.get("locations"), indent=2))

        break

    if status in ("failed", "error"):
        print(json.dumps(data, indent=2))
        raise RuntimeError("FortyGuard task failed")

    time.sleep(5)

else:
    raise TimeoutError("FortyGuard task did not complete")