import asyncio
import json
import time
import httpx

from app.config import settings


LAT = 33.4521
LON = -111.947

DATE = "2026-08-28"
HOUR = "13:00"


# AOI بزرگ‌تر از HeatPlan فعلی
# حدود 0.02 x 0.02 درجه
POLYGON_AOI = {
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
                    [-111.957, 33.442],
                ]],
            },
        }
    ],
}


async def main():
    headers = {
        "api-key": settings.fortyguard_api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "polygon_aoi": POLYGON_AOI,
        "date_time": {
            "start_date": DATE,
            "start_time": HOUR,
            "filter_type": 1,
        },
        "granularity": 100,
        "analytic_type": "tcm",
    }

    print("=== REQUEST ===")
    print(json.dumps(payload, indent=2))

    async with httpx.AsyncClient(timeout=30) as client:

        print("\n=== SUBMIT ===")

        response = await client.post(
            f"{settings.fortyguard_base_url}/v1/heatmap",
            headers=headers,
            json=payload,
        )

        print("HTTP:", response.status_code)
        print(response.text)

        response.raise_for_status()

        activity_id = response.json()["data"]["activity_id"]

        print("\nACTIVITY ID:", activity_id)

        print("\n=== POLLING ===")

        for i in range(30):
            await asyncio.sleep(5)

            status_response = await client.get(
                f"{settings.fortyguard_base_url}/v1/status/{activity_id}",
                headers={
                    "api-key": settings.fortyguard_api_key,
                },
            )

            print(
                f"poll={i + 1}",
                "HTTP=", status_response.status_code,
            )

            status_data = status_response.json()

            data = status_data.get("data", {})
            status = str(data.get("status", "")).lower()

            print("status =", status)

            if status in ("completed", "succeeded"):
                result = data.get("result", {})

                print("\n=== RESULT KEYS ===")
                print(list(result.keys()))

                print("\n=== MAP_DATA ===")
                print(json.dumps(
                    result.get("map_data"),
                    indent=2
                )[:15000])

                print("\n=== STATS_DATA ===")
                print(json.dumps(
                    result.get("stats_data"),
                    indent=2
                )[:15000])

                return

            if status in ("failed", "error"):
                print("\nFAILED:")
                print(json.dumps(status_data, indent=2))
                return

        print("\nTIMEOUT")


asyncio.run(main())