"""Explanation layer.

The explanation may only VERBALIZE values that were computed
deterministically by the exposure engine and ranking. It never creates
numbers. Default: deterministic template (always available, reproducible).
Optional LLM rephrasing keeps the same numbers verbatim.
"""
from __future__ import annotations

from .models import ParsedIntent, Plan


def explain(intent: ParsedIntent, plans: list[Plan], recommended_id: str,
            scores: dict[str, float]) -> str:
    rec = next(p for p in plans if p.id == recommended_id)
    base = next(p for p in plans if p.id == "A")
    pref = {"high": "heat reduction", "medium": "a balance of heat and time",
            "low": "speed"}[intent.heat_priority]

    parts = [
        f"{rec.name} is recommended because you prioritized {pref}.",
    ]
    if recommended_id == "A":
        parts.append(
            f"It is the fastest option at {rec.duration_minutes:.0f} min "
            f"(avg {rec.avg_temperature_f:.0f} F). Cooler alternatives exist "
            "but their time cost outweighs the exposure benefit given your "
            "preference.")
    else:
        parts.append(
            f"Compared with {base.name}, it "
            f"{'starts at ' + rec.start_time + ' and ' if rec.start_time != base.start_time else ''}"
            f"reduces estimated thermal exposure by {rec.exposure_reduction_pct:.0f}% "
            f"(avg {rec.avg_temperature_f:.0f} F vs {base.avg_temperature_f:.0f} F) "
            f"for a time penalty of {rec.time_penalty_minutes:.0f} min.")
        # A candidate whose full thermal interval is not covered by usable
        # live data is excluded as infeasible, so a "C" plan may not exist.
        coolest = next((p for p in plans if p.id == "C"), None)
        if (recommended_id == "B" and coolest is not None
                and coolest.exposure_reduction_pct > rec.exposure_reduction_pct):
            parts.append(
                f"{coolest.name} cuts exposure further "
                f"(-{coolest.exposure_reduction_pct:.0f}%) but costs "
                f"+{coolest.time_penalty_minutes:.0f} min, which the "
                "deterministic scoring judged excessive for your preference.")
    parts.append(
        "All figures are computed from the thermal grid and the deterministic "
        "exposure model - decision support, not medical advice.")
    return " ".join(parts)
