"""Exposure engine: deterministic calculations on known fixture data."""
from app.exposure import exposure, heat_burden


def test_burden_below_threshold_is_zero():
    assert heat_burden(60.0) == 0.0
    assert heat_burden(65.0) == 0.0


def test_burden_monotonic():
    assert heat_burden(90.0) < heat_burden(100.0) < heat_burden(110.0)


def test_exposure_sum_over_time():
    # 30 min at 95F + 30 min at 105F, computed by hand:
    b95 = (95 - 65) ** 1.25
    b105 = (105 - 65) ** 1.25
    assert abs(exposure([(95, 30), (105, 30)]) - (b95 * 30 + b105 * 30)) < 1e-9


def test_exposure_deterministic():
    s = [(100.0, 45.0), (98.5, 45.0)]
    assert exposure(s) == exposure(s)


def test_cooler_is_lower_exposure():
    assert exposure([(85.0, 60)]) < exposure([(105.0, 60)])
