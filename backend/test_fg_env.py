import requests
import time
import json

API_KEY = os.environ["FORTYGUARD_API_KEY"]

HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}

LAT = 33.4521
LON = -111.947
DATE = "2026-08-28"
TIME = "13:00"

# IMPORTANT:
# This is only a test temperature value so we can inspect
# whether Environmental Parameters returns its own metrics.
# We are NOT using it in HeatPlan.
TEMPERATURE_C = 40.0

payload = {
    "latitude": LAT,
    "longitude": LON,
    "temperature": TEMPERATURE_C,
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
    headers=HEADERS,
    json=payload,
    timeout=60,
)

print("\n=== SUBMIT ===")
print("HTTP:", r.status_code)
print(r.text)

if r.status_code != 200:
    raise SystemExit()

body = r.json()
activity_id = body["data"]["activity_id"]

print("\nACTIVITY ID:", activity_id)

status_url = f"https://api.fortyguard.com/v1/status/{activity_id}"

for poll in range(30):
    time.sleep(5)

    sr = requests.get(
        status_url,
        headers={"api-key": API_KEY},
        timeout=30,
    )

    print(f"\npoll={poll + 1} HTTP={sr.status_code}")

    status_body = sr.json()
    data = status_body.get("data", {})
    status = str(data.get("status", "")).lower()

    print("status =", status)

    if status == "completed":
        print("\n=== RESULT ===")
        print(json.dumps(data.get("result"), indent=2))
        break

    if status in ("failed", "error"):
        print("\n=== FAILED ===")
        print(json.dumps(status_body, indent=2))
        break

else:
    print("\nTIMEOUT")