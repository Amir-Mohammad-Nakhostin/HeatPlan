"""Ranking: deterministic, reproducible, sensitive to exposure and time."""
import pytest

from app.planner import _Candidate, rank


def _cand(cid, exp_proxy_temp, dur, dist):
    return _Candidate(id=cid, name=cid, strategy="", start_time="14:00",
                      duration_minutes=dur, distance_m=dist, geometry=[],
                      samples=[(0, exp_proxy_temp, dur)], exposure=exp_proxy_temp)


def test_ranking_deterministic_same_input_same_output():
    cands = [_cand("A", 100, 10, 1000), _cand("B", 92, 15, 1200),
             _cand("C", 88, 20, 1500)]
    assert rank(cands, "high") == rank(cands, "high")


def test_coolest_wins_when_heat_priority_high():
    cands = [_cand("A", 105, 10, 1000), _cand("B", 95, 15, 1200),
             _cand("C", 85, 20, 1500)]
    # exposures here act directly as exposure proxy values
    scores = rank(cands, "high")
    assert min(scores, key=scores.get) == "C"


def test_fastest_wins_when_heat_priority_low():
    cands = [_cand("A", 105, 10, 1000), _cand("B", 95, 20, 1200),
             _cand("C", 85, 40, 3000)]
    scores = rank(cands, "low")
    assert min(scores, key=scores.get) == "A"


def test_time_penalty_affects_ranking():
    fast = [_cand("A", 100, 10, 1000), _cand("B", 90, 12, 1100),
            _cand("C", 80, 15, 1500)]
    slow = [_cand("A", 100, 10, 1000), _cand("B", 90, 12, 1100),
            _cand("C", 80, 60, 1500)]
    s_fast = rank(fast, "medium")
    s_slow = rank(slow, "medium")
    # C's time penalty stretches the time axis, so B's normalized time cost
    # (and thus its score) must drop - the time term demonstrably moves scores.
    assert s_slow["B"] < s_fast["B"]
    assert s_slow["C"] == pytest.approx(s_fast["C"])
