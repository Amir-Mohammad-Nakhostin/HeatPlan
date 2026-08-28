import os
import time
import json
import requests

API_KEY = os.environ["FORTYGUARD_API_KEY"]

BASE_URL = "https://api.fortyguard.com/v1"
HEADERS = {
    "api-key": API_KEY,
    "Content-Type": "application/json",
}

AOI = {
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


def submit_hour(hour):
    payload = {
        "polygon_aoi": AOI,
        "date_time": {
            "start_date": "2026-08-28",
            "start_time": f"{hour:02d}:00",
            "filter_type": 1,
        },
        "granularity": 100,
        "analytic_type": "tcm",
    }

    print()
    print("=" * 70)
    print(f"SUBMITTING HOUR: {hour:02d}:00")
    print("=" * 70)

    response = requests.post(
        f"{BASE_URL}/heatmap",
        headers=HEADERS,
        json=payload,
        timeout=60,
    )

    print("HTTP:", response.status_code)
    response.raise_for_status()

    body = response.json()
    print(json.dumps(body, indent=2))

    activity_id = body["data"]["activity_id"]
    print("ACTIVITY:", activity_id)

    return activity_id


def wait_for_result(activity_id):
    print("\nPOLLING...")

    for poll in range(1, 60):
        response = requests.get(
            f"{BASE_URL}/status/{activity_id}",
            headers={"api-key": API_KEY},
            timeout=60,
        )

        response.raise_for_status()
        body = response.json()

        data = body.get("data", {})
        status = data.get("status")

        print(f"poll={poll} status={status}")

        if status == "Completed":
            return data.get("result", {})

        if status == "Failed":
            raise RuntimeError(
                f"FortyGuard activity failed: {json.dumps(body, indent=2)}"
            )

        time.sleep(3)

    raise TimeoutError("Activity did not complete in time.")


def analyze(hour, result):
    map_data = result.get("map_data", {})
    stats_data = result.get("stats_data", {})

    features = map_data.get("features", [])

    print("\nRESULT ANALYSIS")
    print("-" * 70)

    print("map_data type:", map_data.get("type"))
    print("feature count:", len(features))

    if features:
        temps = []

        for feature in features:
            props = feature.get("properties", {})
            value = props.get("average_temperature")

            if isinstance(value, (int, float)):
                temps.append(float(value))

        print("temperature tiles:", len(temps))

        if temps:
            print("min:", min(temps))
            print("max:", max(temps))
            print("mean:", sum(temps) / len(temps))

            print("first 5:", temps[:5])

        print("\nFIRST FEATURE:")
        print(json.dumps(features[0], indent=2))

    print("\nSTATS:")
    print(json.dumps(stats_data, indent=2))

    return {
        "hour": hour,
        "feature_count": len(features),
        "temperatures": [
            f.get("properties", {}).get("average_temperature")
            for f in features
        ],
        "stats": stats_data,
    }


def main():
    all_results = []

    # Test 13:00, 14:00, 15:00, 16:00, 17:00 separately.
    for hour in range(13, 18):
        activity_id = submit_hour(hour)
        result = wait_for_result(activity_id)

        analyzed = analyze(hour, result)
        all_results.append(analyzed)

        # Small pause between API tasks.
        time.sleep(2)

    print()
    print("=" * 70)
    print("FINAL HOURLY COMPARISON")
    print("=" * 70)

    for item in all_results:
        temps = [
            x for x in item["temperatures"]
            if isinstance(x, (int, float))
        ]

        if temps:
            print(
                f"{item['hour']:02d}:00 | "
                f"tiles={len(temps)} | "
                f"min={min(temps):.4f} | "
                f"max={max(temps):.4f} | "
                f"mean={sum(temps)/len(temps):.4f}"
            )
        else:
            print(
                f"{item['hour']:02d}:00 | "
                f"NO TEMPERATURE DATA"
            )

    with open(
        "hourly_tcm_test_result.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            all_results,
            f,
            indent=2,
            ensure_ascii=False,
        )

    print()
    print("Saved:")
    print("hourly_tcm_test_result.json")
    print()
    print("DONE")


if __name__ == "__main__":
    main()