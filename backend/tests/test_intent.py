"""Intent parsing: spec demo sentence + route + priority detection."""
from app.intent import fallback_parse


def test_demo_sentence():
    i = fallback_parse("I need to work outdoors for 90 minutes starting at 2 PM.",
                       "2026-08-26", None, None)
    assert i.task == "outdoor work"
    assert i.duration_minutes == 90
    assert i.start_time == "14:00"
    assert i.location_name  # defaults to Phoenix
    assert abs(i.lat - 33.4484) < 0.01


def test_hours_duration():
    i = fallback_parse("I need to walk for 1.5 hours at 6 am", "2026-08-26", None, None)
    assert i.duration_minutes == 90
    assert i.start_time == "06:00"
    assert i.task == "walking"


def test_route_detection():
    i = fallback_parse("Walk from Phoenix to Tempe for 45 minutes at 3 pm",
                       "2026-08-26", None, None)
    assert i.task_type == "route"
    assert i.end_location_name == "Tempe, AZ"


def test_heat_priority():
    i = fallback_parse("Run for 30 minutes at 5 pm, I want the coolest option",
                       "2026-08-26", None, None)
    assert i.heat_priority == "high"


def test_defaults_warn():
    i = fallback_parse("go outside", "2026-08-26", None, None)
    assert any("duration" in w.lower() for w in i.warnings)
    assert any("location" in w.lower() for w in i.warnings)
