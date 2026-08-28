"""Ensure the route-duration contract doc is present and mentions the
four canonical concepts. Keeps the doc from silently disappearing on a
future refactor."""
from pathlib import Path


def test_route_duration_contract_doc_exists_and_names_the_four_quantities():
    p = Path(__file__).resolve().parents[1] / "docs" / "route_duration_contract.md"
    assert p.is_file(), f"contract doc missing at {p}"
    body = p.read_text(encoding="utf-8")
    # Each of the four contract quantities must be explicitly named.
    for token in (
        "USER REQUESTED DURATION",
        "ACTUAL ROUTE DURATION",
        "THERMAL COVERAGE",
        "EXPOSURE",
    ):
        assert token in body, f"contract doc must define {token}"
