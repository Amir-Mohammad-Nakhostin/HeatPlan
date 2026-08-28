"""Deterministic thermal exposure engine (v1, transparent, NOT medical).

Model
-----
heat_burden(T)   = max(0, T - 65) ** 1.25                 [burden units]
exposure(samples) = Σ heat_burden(T_i) × duration_i       [burden·min]

- T is the raw FortyGuard temperature (converted to °F internally). This is
  a temperature-based thermal burden estimate; FortyGuard does not return an
  apparent-temperature / physiological metric, and none is claimed here.
- Only fields actually returned by FortyGuard are used; nothing is invented.
- Decision support only; not physiologically validated, not medical advice.
"""
from __future__ import annotations

COMFORT_THRESHOLD_F = 65.0
EXPONENT = 1.25


def heat_burden(temp_f: float) -> float:
    return max(0.0, temp_f - COMFORT_THRESHOLD_F) ** EXPONENT


def exposure(samples: list[tuple[float, float]]) -> float:
    """samples: list of (temperature_f, duration_minutes)."""
    return sum(heat_burden(temp_f) * duration for temp_f, duration in samples)


def exposure_timeline(samples: list[tuple[int, float, float]]) -> list[dict]:
    """samples: (offset_minutes, temperature_f, duration_minutes)."""
    return [
        {
            "offset_minutes": offset,
            "temperature_f": round(temp_f, 1),
            "heat_burden": round(heat_burden(temp_f), 2),
        }
        for offset, temp_f, duration in samples
    ]
