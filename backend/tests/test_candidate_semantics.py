"""P1 regression tests: candidate A/B/C selection semantics.

The three stationary plans must be a meaningful, deterministic trade-off:

  A = time-oriented      (earliest feasible start)
  C = exposure-oriented  (lowest deterministic exposure; earliest tie-break)
  B = balanced           (closest to the A-C midpoint in normalized
                          (start shift, exposure) space, NOT "second coolest")

Ranking weights are untouched by selection; they only pick the recommendation.
"""
from app.planner import _Candidate, _select_archetypes


def _cand(shift: int, exposure: float) -> tuple[int, _Candidate]:
    return (shift, _Candidate(
        id="", name="", strategy="", start_time=f"{14 + shift:02d}:00",
        duration_minutes=60.0, distance_m=0.0, geometry=[],
        samples=[(0, 100.0, 60.0)], exposure=exposure, avg_temp=100.0))


def _ids(chosen):
    return [c.exposure for c in chosen]


def test_a_is_earliest_and_c_is_coolest():
    cands = [_cand(0, 100.0), _cand(1, 90.0), _cand(2, 80.0), _cand(3, 70.0)]
    chosen = _select_archetypes(cands)
    assert chosen[0].exposure == 100.0          # A = earliest (shift 0)
    assert chosen[2].exposure == 70.0           # C = coolest
    assert chosen[1] not in (chosen[0], chosen[2])


def test_b_is_balanced_not_second_coolest():
    """The second-coolest candidate (shift 1, exposure 21) sits right next
    to C on the exposure axis; the balanced pick must instead be the one
    nearest the A-C midpoint (shift 2, exposure 60).

    Normalized space: shifts/exposures min-max scaled to [0, 1];
    midpoint = (0.5, 0.5).  Distances: shift1 -> 0.25+0.4875 = 0.7375;
    shift2 -> 0.0; shift3 -> 0.25+0.0125 = 0.2625.  B = shift 2."""
    cands = [_cand(0, 100.0), _cand(1, 21.0), _cand(2, 60.0),
             _cand(3, 59.0), _cand(4, 20.0)]
    chosen = _select_archetypes(cands)
    a, b, c = chosen
    assert a.exposure == 100.0 and c.exposure == 20.0
    assert b.exposure == 60.0  # balanced midpoint, NOT second-coolest (21.0)


def test_balanced_pick_prefers_midpoint_over_extremes():
    """With a linear trade-off the midpoint candidate is exactly 'balanced':
    B must be the middle shift, not the second coolest."""
    cands = [_cand(0, 100.0), _cand(1, 80.0), _cand(2, 60.0),
             _cand(3, 40.0), _cand(4, 20.0)]
    chosen = _select_archetypes(cands)
    assert chosen[0].exposure == 100.0   # A earliest
    assert chosen[2].exposure == 20.0    # C coolest
    assert chosen[1].exposure == 60.0    # B = exact midpoint (shift 2)


def test_two_candidates_degenerate_b_falls_back_to_c():
    chosen = _select_archetypes([_cand(0, 100.0), _cand(5, 50.0)])
    assert [c.exposure for c in chosen] == [100.0, 50.0]  # A, C (B == C -> dropped)


def test_single_candidate_returns_only_a():
    chosen = _select_archetypes([_cand(2, 75.0)])
    assert len(chosen) == 1 and chosen[0].exposure == 75.0


def test_coolest_tie_broken_by_earliest_start():
    cands = [_cand(0, 100.0), _cand(2, 50.0), _cand(4, 50.0)]
    chosen = _select_archetypes(cands)
    assert chosen[2].start_time == "16:00"  # shift 2, the earlier coolest


def test_selection_is_deterministic():
    cands = [_cand(0, 95.0), _cand(1, 70.0), _cand(2, 90.0), _cand(3, 65.0)]
    first = [c.exposure for c in _select_archetypes(cands)]
    for _ in range(5):
        assert [c.exposure for c in _select_archetypes(cands)] == first
