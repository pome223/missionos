"""Clock and saved-clock contracts only; no provider or physical work."""
from copy import deepcopy

import pytest
from src.runtime.starship_final_timing import MissionWallBudget, elapsed, verify_completed


def test_sleep_inclusive_utc_stops_even_when_monotonic_is_paused():
    mono, utc = [100.], [1000.]
    budget=MissionWallBudget(1800., clock=lambda:mono[0], wall_clock=lambda:utc[0])
    mono[0] += 1281.796
    utc[0] += 2143.796
    with pytest.raises(ValueError,match="wall_budget"):
        budget.finish()
    record=budget.record()
    assert record['status']=='exceeded' and record['closed']
    assert record['enforced_elapsed_s']==pytest.approx(2143.796)
    assert record['monotonic_elapsed_s']==pytest.approx(1281.796)
    assert not record['sleep_duration_inferred_from_clock_difference']
    with pytest.raises(ValueError):verify_completed(record,maximum_s=1800.)


def test_monotonic_budget_still_stops_when_utc_barely_advances():
    mono,utc=iter((0.,11.)),iter((1000.,1001.))
    budget=MissionWallBudget(10.,clock=lambda:next(mono),wall_clock=lambda:next(utc))
    with pytest.raises(ValueError):budget.finish()
    assert budget.record()['enforced_elapsed_s']==11.


@pytest.mark.parametrize('clock_key',['mono','utc'])
def test_a_clock_cannot_regress_between_checks(clock_key):
    clocks={'mono':100.,'utc':1000.}
    budget=MissionWallBudget(20.,clock=lambda:clocks['mono'],wall_clock=lambda:clocks['utc'])
    clocks['mono']+=5;clocks['utc']+=5;budget.check()
    clocks[clock_key]-=1
    with pytest.raises(ValueError,match='regressed'):budget.check()
    assert budget.record()['status']=='invalid_clock'
    with pytest.raises(ValueError):verify_completed(budget.record(),maximum_s=20.)


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,None])
def test_nonfinite_or_boolean_clock_values_are_rejected(value):
    with pytest.raises(ValueError):MissionWallBudget(10.,clock=lambda:value,wall_clock=lambda:1000.)


@pytest.mark.parametrize('maximum',[0.,-1.,True,float('nan'),float('inf')])
def test_budgets_are_finite_positive_seconds(maximum):
    with pytest.raises(ValueError):MissionWallBudget(maximum)


def test_completed_records_bind_both_clocks_and_cannot_be_retimed():
    mono,utc=iter((10.,14.)),iter((1000.,1004.))
    budget=MissionWallBudget(10.,clock=lambda:next(mono),wall_clock=lambda:next(utc))
    budget.finish();record=budget.record()
    assert verify_completed(record,maximum_s=10.)['utc_elapsed_s']==4.
    changed=deepcopy(record);changed['end']['utc_s']+=20.
    with pytest.raises(ValueError):verify_completed(changed,maximum_s=10.)
    with pytest.raises(ValueError,match='already_closed'):budget.check()
