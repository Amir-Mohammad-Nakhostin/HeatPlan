"""Deterministic best-time explanation tests (spec: backend-derived facts).

explain_ranking must verbalize ONLY the deterministic ranking outcome and
computed exposures/temperatures - never invent numbers or rationale.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.structured import StructuredCandidate, explain_ranking

TZ = ZoneInfo("America/Phoenix")


def _cand(hour: int, exposure: float, temp: float) -> StructuredCandidate:
    return StructuredCandidate(
        start_time=f"{hour:02d}:00", end_time=f"{hour + 1:02d}:00",
        start_dt=datetime(2026, 8, 28, hour, tzinfo=TZ), duration_minutes=60,
        thermal_status="ok", avg_temperature_f=temp, exposure=exposure)


def test_single_candidate_is_only_window_fit():
    ranked = [_cand(14, 100.0, 96.0)]
    reasons = explain_ranking(ranked, total_candidates=1)
    assert reasons["14:00"] == "The only time that fits inside your window."


def test_single_feasible_among_unavailable_candidates():
    ranked = [_cand(14, 100.0, 96.0)]
    reasons = explain_ranking(ranked, total_candidates=4)
    assert "only time in your window with usable live thermal data" in reasons["14:00"]


def test_best_is_lowest_exposure():
    ranked = [_cand(15, 80.0, 94.0), _cand(14, 120.0, 98.0)]
    reasons = explain_ranking(ranked, total_candidates=2)
    assert "Lowest estimated thermal exposure" in reasons["15:00"]


def test_tie_break_reason_names_earliest_start():
    ranked = [_cand(14, 100.0, 96.0), _cand(15, 100.0, 95.0)]
    reasons = explain_ranking(ranked, total_candidates=2)
    assert "earliest" in reasons["14:00"]
    assert "Tied" in reasons["14:00"]


def test_alternative_reason_carries_real_percent_and_temp_delta():
    ranked = [_cand(15, 100.0, 95.0), _cand(14, 150.0, 99.0)]
    reasons = explain_ranking(ranked, total_candidates=2)
    alt = reasons["14:00"]
    assert "50%" in alt            # (150-100)/100 - a real computed delta
    assert "4 °F warmer" in alt    # 99 - 95


def test_alternative_nearly_same_exposure():
    ranked = [_cand(15, 100.0, 95.0), _cand(14, 100.5, 95.2)]
    reasons = explain_ranking(ranked, total_candidates=2)
    assert reasons["14:00"].lower().startswith("nearly the same estimated thermal exposure")


def test_zero_exposure_tie_uses_same_exposure_wording():
    ranked = [_cand(15, 0.0, 60.0), _cand(14, 0.0, 61.0)]
    reasons = explain_ranking(ranked, total_candidates=2)
    assert "same estimated thermal exposure" in reasons["14:00"]


def test_empty_ranking_gives_no_reasons():
    assert explain_ranking([], total_candidates=3) == {}
