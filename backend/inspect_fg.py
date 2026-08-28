import asyncio
import json
import httpx

from app.config import settings
from app.fortyguard_client import _aoi_polygon


LAT = 33.4521
LON = -111.947
DATE = "2026-08-28"
HOUR = 13

ACTIVITY_ID = "5a71a10f-1da0-4f02-aa08-9ae3e89df286"


async def main():
    print("=== HEATPLAN REQUEST PARAMETERS ===")
    print("latitude:", LAT)
    print("longitude:", LON)
    print("date:", DATE)
    print("hour:", f"{HOUR:02d}:00")
    print("filter_type:", 1)
    print("granularity:", 100)

    print("\n=== AOI POLYGON ===")
    print(json.dumps(
        _aoi_polygon(LAT, LON),
        indent=2
    ))

    print("\n=== FORTYGUARD STATUS ===")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{settings.fortyguard_base_url}/v1/status/{ACTIVITY_ID}",
            headers={
                "api-key": settings.fortyguard_api_key,
            },
        )

        print("HTTP:", response.status_code)

        data = response.json()

        print("\n=== TOP LEVEL RESPONSE KEYS ===")
        print(list(data.keys()))

        fg_data = data.get("data", {})

        print("\n=== DATA KEYS ===")
        print(list(fg_data.keys()) if isinstance(fg_data, dict) else type(fg_data))

        result = fg_data.get("result", {})

        print("\n=== RESULT KEYS ===")
        print(
            list(result.keys())
            if isinstance(result, dict)
            else type(result)
        )

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


asyncio.run(main())