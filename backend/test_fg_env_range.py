import json
import time
import requests

from app.config import settings

API_KEY = settings.fortyguard_api_key

BASE = "https://api.fortyguard.com"

payload = {
    "latitude": 33.4521,
    "longitude": -111.947,
    "temperature": 40.0,
    "date_time": {
        "start_date": "2026-08-28",
        "start_time": "13:00",
        "end_time": "17:00",
        "filter_type": 2
    },
    "analysis": [
        "heat_index_celsius",
        "apparent_temperature_celsius",
        "relative_humidity_percent",
        "wet_bulb_temperature_celsius",
        "precipitation_mm",
        "cloud_cover_octas"
    ]
}

headers = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}

print("=== REQUEST ===")
print(json.dumps(payload, indent=2))

r = requests.post(
    f"{BASE}/v1/env_params",
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
    time.sleep(5)

    r = requests.get(
        f"{BASE}/v1/status/{activity_id}",
        headers={"api-key": API_KEY},
        timeout=30,
    )

    data = r.json()["data"]
    status = data.get("status")

    print(f"poll={i+1} status={status}")

    if str(status).lower() == "completed":
        result = data["result"]

        print("\n=== METADATA ===")
        print(json.dumps(result.get("metadata"), indent=2))

        print("\n=== LOCATIONS ===")
        print(json.dumps(result.get("locations"), indent=2))

        break

    if str(status).lower() in ("failed", "error"):
        print("\nFAILED")
        print(json.dumps(data, indent=2))
        break
else:
    print("TIMEOUT")