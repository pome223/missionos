"""Optional search budget/clock/evidence regressions with stub forecasts, no 6DOF."""
from copy import deepcopy
import math
from types import SimpleNamespace

import pytest

from src.runtime import starship_return_time_search as screen
from src.runtime import starship_m2_verifier as verifier
from src.runtime.starship_replanning import digest
from src.runtime.starship_replanning_executor import ReplanningExecutor


@pytest.fixture
def item(monkeypatch, tmp_path):
    clock = [0.]
    radius = 6678137.
    speed = 7725.
    energy = speed*speed/2-3.986004418e14/radius
    a = -3.986004418e14/(2*energy)
    period = 2*math.pi*math.sqrt(a*a*a/3.986004418e14)
    actor = SimpleNamespace(s=SimpleNamespace(time_s=1000.), scheduled=2280.,
        times={'nominal': 2280., 'next_orbit': 2280.+period}, selected=None,
        forecast_count=0, sources={'fixture.py': 'a'*64}, output=tmp_path,
        p={'integration': {'coast_dt_s': 1.}},
        envelope={'batch_timeout_s': 10., 'decision_timeout_s': 35.,
            'event_supervision': {'deadline_margin_s': 5.}, 'maximum_forecasts': 12,
            'maximum_workers': 3, 'maximum_delay_s': 6000.,
            'operations': {'areas': {'open': {'radius_m': 125000., 'position': -40.}}}},
        deployment={'authority': {'return_time_search': deepcopy(screen.CONFIG)}},
        deployment_binding={'retained_count': 1}, case='event_normal', events=[])
    actor.monitor = SimpleNamespace(times=deepcopy(actor.times), pending=[])
    actor.observation = lambda: {'retained_count': 1, 'state': {'time_s': actor.s.time_s,
        'r_eci_m': [radius, 0., 0.], 'v_eci_mps': [0., speed, 0.]}}
    actor.event = lambda event, **fields: actor.events.append({'event': event, 'time_s': actor.s.time_s, **fields})
    actor.step = lambda: setattr(actor.s, 'time_s', actor.s.time_s+1.)
    notice = {'areas': {'open': [0., 20000.]}, 'expires_at_s': 20000., 'sequence': 1}
    settings = SimpleNamespace(startup_elapsed=0., wait_elapsed=1., shutdown_elapsed=0.,
        shutdown_error=False, submit_error=False, wait_error=False, incomplete=False,
        no_contact=False, pools=[], wait_calls=[], trial_calls=[])
    monkeypatch.setattr(screen.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(screen, 'notices', lambda *_: deepcopy(notice))
    monkeypatch.setattr(screen, 'metrics', lambda trial: trial.get('contact'))
    monkeypatch.setattr(verifier, 'metrics', lambda trial: trial.get('contact'))
    def distance(metric, area):
        return abs(metric['position']-area['position'])*1000.
    monkeypatch.setattr(screen, 'site_distance', distance)
    monkeypatch.setattr(verifier, 'site_distance', distance)

    class Pool:
        def __init__(self, **_):
            clock[0] += settings.startup_elapsed
            self._processes = {}
            settings.pools.append(self)
        def submit(self, _job, args):
            if settings.submit_error:
                raise OSError('fixture submit failed')
            origin, _profile, when, _scheduled, _path, scale = args
            trial = {'origin': deepcopy(origin), 'return_time_s': when, 'integration_scale': scale,
                'contact': None if settings.no_contact else {'position': when-(2280.+period), 'time_s': when+1000.}}
            return SimpleNamespace(done=lambda: True, result=lambda: deepcopy(trial))
        def shutdown(self, **_):
            clock[0] += settings.shutdown_elapsed
            if settings.shutdown_error:
                raise OSError('fixture shutdown failed')
    monkeypatch.setattr(screen, 'ProcessPoolExecutor', Pool)
    def wait(_name, poll, _limit, **_kwargs):
        settings.wait_calls.append((_limit, _kwargs))
        actor.s.time_s += settings.wait_elapsed
        clock[0] += settings.wait_elapsed
        if settings.wait_error:
            raise OSError('fixture wait failed')
        return None if settings.incomplete else poll()
    actor.wait = wait
    def calculate_trials(names, **kwargs):
        settings.trial_calls.append((list(names), dict(actor.times), kwargs))
        return True
    actor.calculate_trials = calculate_trials
    return SimpleNamespace(actor=actor, settings=settings, clock=clock, notice=notice)


def study_for(item):
    actor = item.actor
    row = actor.return_time_search
    stopped = getattr(actor, 'stop_reason', None)
    return {'authority': {'return_time_search': deepcopy(screen.CONFIG), 'return_authority': actor.envelope},
        'handoff': {'origin': {'state': row['origin']['state']}}, 'return_source_sha256': actor.sources,
        'return_study': {'scheduled_s': actor.scheduled, 'opportunities': actor.times,
            'profile': deepcopy(actor.p), 'pending_operations': deepcopy(getattr(actor, 'pending', [])),
            'forecast_count': actor.forecast_count, 'return_time_search': deepcopy(row), 'dispatch': None,
            'execution': {'events': deepcopy(actor.events), 'final_state': {'time_s': actor.s.time_s},
                'outcome': {'termination': getattr(actor, 'termination', 'return_unresolved'),
                    'stop_reason': stopped, 'contact_receipt': None}}}}


def rehash(study):
    returning = study['return_study']
    for event in returning['execution']['events']:
        if event['event'] in {'m2_return_time_screen', 'm2_return_search_stopped'}:
            event['record_sha256'] = digest(returning['return_time_search'])


def test_screen_reserves_all_requested_admission_trials(item):
    actor = item.actor
    actor.forecast_count = 4
    assert screen.search(actor, requested_names=['nominal', 'next_orbit']) is False
    assert actor.return_time_search['reserved_trial_count'] == 8
    assert actor.forecast_count == 4 and item.settings.pools == []
    assert actor.s.time_s == 1000. and actor.return_time_search['clock_debt_s'] == 0


def test_next_orbit_only_search_keeps_its_four_admission_slots(item):
    actor = item.actor
    actor.forecast_count = 4
    assert screen.search(actor, requested_names=['next_orbit'])
    assert actor.forecast_count == 7 and actor.return_time_search['reserved_trial_count'] == 4
    assert actor.forecast_count+4 <= actor.envelope['maximum_forecasts']


def test_nominal_decision_headroom_can_decline_otherwise_allowed_search(item):
    actor = item.actor
    actor.s.time_s = actor.times['nominal']-55.
    assert screen.search(actor, requested_names=['nominal', 'next_orbit']) is False
    assert actor.return_time_search['cutoff_time_s'] == actor.times['nominal']-50.
    assert not item.settings.pools and actor.forecast_count == 0


@pytest.mark.parametrize('failure', ['budget', 'no_contact', 'unavailable_area', 'submit_error', 'wait_error', 'shutdown_error'])
def test_optional_failure_keeps_original_candidates_and_calculates_them(item, failure):
    actor = item.actor
    original = deepcopy(actor.times)
    if failure == 'budget':
        actor.forecast_count = 4
    elif failure == 'unavailable_area':
        item.notice['areas'] = {}
    else:
        setattr(item.settings, failure, True)
    assert ReplanningExecutor.calculate(actor, ['nominal', 'next_orbit']) is True
    assert item.settings.trial_calls[0][0] == ['nominal', 'next_orbit']
    assert item.settings.trial_calls[0][1] == original
    assert actor.return_time_search['accepted_for_trial'] is False
    assert getattr(actor, 'stop_reason', None) is None
    if failure in ('submit_error', 'wait_error'):
        assert actor.return_time_search['reason'] == 'screen_failed'
        assert actor.return_time_search['error_type'] == 'OSError'
    if failure == 'shutdown_error':
        assert actor.return_time_search['reason'] == 'screen_shutdown_failed'
        assert actor.return_time_search['shutdown_error_type'] == 'OSError'


def test_unavailable_area_is_excluded_even_when_it_is_closest(item):
    actor = item.actor
    actor.envelope['operations']['areas']['closed'] = {'radius_m': 1e6, 'position': 0.}
    original = deepcopy(actor.envelope['operations']['areas'])
    assert screen.search(actor)
    assert actor.return_time_search['suggestion']['screen_target_area'] == 'open'
    assert actor.envelope['operations']['areas'] == original


def test_area_not_available_for_all_contact_times_is_not_screen_target(item):
    center = item.actor.times['next_orbit']
    item.notice['areas']['open'] = [center+950., center+2000.]
    assert screen.search(item.actor) is False
    assert item.actor.return_time_search['suggestion'] is None


def test_cleanup_beyond_entire_approved_coast_horizon_is_unresolved(item):
    item.settings.shutdown_elapsed = 8000.
    original = deepcopy(item.actor.times)
    assert ReplanningExecutor.calculate(item.actor, ['next_orbit'], budget_s=2., cutoff_time_s=1004.) is False
    row = item.actor.return_time_search
    assert row['clock_budget_exhausted'] is True and row['clock_debt_s'] == 721.
    assert row['end_time_s'] == 8280. and row['wall_time_s'] == 8001.
    assert row['accepted_for_trial'] is False and item.actor.times == original
    assert item.settings.trial_calls == []
    assert item.actor.stop_reason == 'return_search_clock_debt'
    study = study_for(item)
    verifier._search_evidence(study, item.actor.deployment_binding)


@pytest.mark.parametrize('mutation', ['dispatch', 'positive_outcome', 'later_forecast', 'false_debt', 'moved_time'])
def test_clock_debt_cannot_be_relabelled_as_positive_completion(item, mutation):
    item.settings.shutdown_elapsed = 8000.
    ReplanningExecutor.calculate(item.actor, ['next_orbit'], budget_s=2., cutoff_time_s=1004.)
    study = study_for(item)
    ret = study['return_study']
    if mutation == 'dispatch':
        ret['dispatch'] = {'candidate_id': 'nominal'}
    elif mutation == 'positive_outcome':
        ret['execution']['outcome']['termination'] = 'surface_contact'
    elif mutation == 'later_forecast':
        ret['forecast_count'] += 4
    elif mutation == 'false_debt':
        ret['return_time_search']['clock_debt_s'] = 0.
    else:
        ret['execution']['final_state']['time_s'] += 1.
    rehash(study)
    with pytest.raises(ValueError):
        verifier._search_evidence(study, item.actor.deployment_binding)


def test_scalar_verifier_recomputes_time_instead_of_trusting_rehashed_proposal(item):
    assert screen.search(item.actor)
    study = study_for(item)
    verifier._search_evidence(study, item.actor.deployment_binding)
    ret = study['return_study']
    ret['return_time_search']['suggestion']['return_time_s'] += 5.
    ret['opportunities']['next_orbit'] += 5.
    rehash(study)
    with pytest.raises(ValueError, match='search_suggestion_recalculation'):
        verifier._search_evidence(study, item.actor.deployment_binding)


def test_no_new_screen_is_started_for_an_existing_booking_even_near_cutoff(item):
    item.actor.selected = 'nominal'
    item.actor.s.time_s = item.actor.times['nominal']-1.
    assert screen.search(item.actor, requested_names=['nominal', 'next_orbit']) is False
    assert not item.settings.pools and item.actor.forecast_count == 0
    assert item.actor.return_time_search['work_started'] is False
    assert item.actor.return_time_search['clock_debt_s'] == 0.


def test_clock_debt_ends_event_loop_even_with_an_undrained_event(item):
    actor = item.actor
    actor.monitor.inhibited = False
    actor.candidates = {}
    actor.checks = lambda notice: {}
    actor.choose = lambda stage, allowed: "evaluate_returns"
    def calculate(names):
        actor.stop_reason = "return_search_clock_debt"
        actor.monitor.pending = [{"id": "event-1"}]
        return False
    actor.calculate = calculate
    actor.finish = lambda: {"stop_reason": actor.stop_reason}
    assert ReplanningExecutor.run_event_supervision(actor) == {"stop_reason": "return_search_clock_debt"}


def test_pending_event_disposition_accepts_only_recorded_unresolved_clock_debt():
    from test_starship_replanning_events import study
    from src.runtime.starship_replanning_events import verify_events
    record, envelope = study()
    record['decisions'] = record['decisions'][:1]
    screen_record = {'clock_budget_exhausted': True, 'clock_debt_s': 3., 'accepted_for_trial': False,
        'end_time_s': 104., 'forecast_count_after': 3}
    record['return_time_search'] = screen_record
    record['forecast_count'] = 3
    record['execution']['outcome'] = {'termination': 'return_search_clock_debt_unresolved',
        'stop_reason': 'return_search_clock_debt', 'contact_receipt': None}
    record['execution']['events'] = [
        {'event': 'm2_return_search_stopped', 'time_s': 104., 'record_sha256': digest(screen_record)},
        {'event': 'm1_supervision_terminated', 'time_s': 104., 'event_ids': ['event-1'],
            'reason': 'return_search_clock_debt', 'termination': 'return_search_clock_debt_unresolved'}]
    assert verify_events(record, envelope, expected_deployment_binding={'retained_count': 1}) == []
    record['return_time_search']['clock_debt_s'] = 0.
    record['execution']['events'][0]['record_sha256'] = digest(record['return_time_search'])
    assert 'supervision_event_unhandled' in verify_events(record, envelope, expected_deployment_binding={'retained_count': 1})


def test_optional_cleanup_crossing_search_cutoff_catches_up_and_keeps_later_plan(item):
    item.settings.shutdown_elapsed = 10.
    original = deepcopy(item.actor.times)
    assert ReplanningExecutor.calculate(item.actor, ['nominal', 'next_orbit'], budget_s=2., cutoff_time_s=1004.)
    row = item.actor.return_time_search
    assert row['end_time_s'] == 1011. and row['clock_debt_s'] == 0.
    assert row['search_cutoff_exceeded'] and not row['accepted_for_trial']
    assert item.actor.times == original
    assert item.settings.trial_calls[0][0] == ['nominal', 'next_orbit']
    verifier._search_evidence(study_for(item), item.actor.deployment_binding)

def test_cleanup_losing_nominal_does_not_discard_future_next_orbit(item):
    item.settings.shutdown_elapsed = 1300.
    assert ReplanningExecutor.calculate(item.actor, ['nominal', 'next_orbit'], budget_s=2., cutoff_time_s=1004.)
    assert item.actor.s.time_s == 2301.
    assert item.settings.trial_calls[0][0] == ['next_orbit']
    assert getattr(item.actor, 'stop_reason', None) is None



def bounded_comparison(actor):
    actor.envelope['batch_timeout_s'] = 600.
    actor.envelope['comparison_preparation'] = {'minimum_start_budget_s': 180.}


def test_preparation_equal_to_batch_deadline_screens_and_reserves_four_trials(item):
    actor = item.actor
    bounded_comparison(actor)
    actor.forecast_count = 4
    assert ReplanningExecutor.calculate(actor, ['next_orbit'], budget_s=600., cutoff_time_s=1600.)
    row = actor.return_time_search
    assert row['work_started'] and row['accepted_for_trial']
    assert row['budget']['total_budget_s'] == 600.
    assert row['budget']['screen_budget_s'] == pytest.approx(600.*3/7)
    assert row['budget']['reserved_admission_budget_s'] == pytest.approx(600.*4/7)
    assert row['budget']['preparation_cutoff_time_s'] == 1600.
    assert row['cutoff_time_s'] == pytest.approx(1000.+600.*3/7)
    assert actor.forecast_count == 7 and actor.forecast_count+4 <= actor.envelope['maximum_forecasts']
    assert item.settings.trial_calls[0][2] == {
        'budget_s': 599., 'cutoff_time_s': 1600., 'synthetic_failure': False,
    }
    assert row['return_admission_granted'] is False


@pytest.mark.parametrize('available, starts', [(180., False), (181., False), (182., True)])
def test_short_shared_window_preserves_minimum_admission_time(item, available, starts):
    actor = item.actor
    bounded_comparison(actor)
    assert ReplanningExecutor.calculate(actor, ['next_orbit'], budget_s=600.,
                                       cutoff_time_s=1000.+available)
    row = actor.return_time_search
    assert row['work_started'] is starts
    assert row['budget']['reserved_admission_budget_s'] == 180.
    assert item.settings.trial_calls[0][2]['budget_s'] == available-(1. if starts else 0.)
    assert len(item.settings.pools) == int(starts)


@pytest.mark.parametrize('maximum, starts', [(6, False), (7, True)])
def test_shared_time_does_not_relax_four_trial_count_reservation(item, maximum, starts):
    bounded_comparison(item.actor)
    item.actor.envelope['maximum_forecasts'] = maximum
    assert ReplanningExecutor.calculate(item.actor, ['next_orbit'], budget_s=600., cutoff_time_s=1600.)
    row = item.actor.return_time_search
    assert row['work_started'] is starts
    assert row['reserved_trial_count'] == 4
    assert item.actor.forecast_count+4 <= maximum


def test_shared_window_keeps_existing_booking_and_original_times(item):
    actor = item.actor
    bounded_comparison(actor)
    actor.selected = 'nominal'
    original = deepcopy(actor.times)
    assert ReplanningExecutor.calculate(actor, ['next_orbit'], budget_s=600., cutoff_time_s=1600.)
    assert not actor.return_time_search['work_started'] and not item.settings.pools
    assert actor.times == original and actor.selected == 'nominal'
    assert item.settings.trial_calls[0][2]['budget_s'] == 600.


def test_shared_budget_charges_screen_startup_wait_and_cleanup_before_admission(item):
    bounded_comparison(item.actor)
    item.settings.startup_elapsed = 20.
    item.settings.wait_elapsed = 100.
    item.settings.shutdown_elapsed = 7.
    assert ReplanningExecutor.calculate(item.actor, ['next_orbit'], budget_s=600., cutoff_time_s=1600.)
    row = item.actor.return_time_search
    assert row['work_started'] and row['accepted_for_trial']
    assert row['wall_time_s'] == 127. and row['end_time_s'] == 1127.
    assert item.settings.wait_calls[0][0] == pytest.approx(600.*3/7-20.)
    assert item.settings.trial_calls[0][2]['budget_s'] == 473.
    assert item.settings.trial_calls[0][2]['cutoff_time_s'] == 1600.


def test_screen_cleanup_cannot_start_admission_after_shared_deadline(item):
    bounded_comparison(item.actor)
    item.settings.shutdown_elapsed = 602.
    original = deepcopy(item.actor.times)
    assert not ReplanningExecutor.calculate(item.actor, ['next_orbit'], budget_s=600., cutoff_time_s=1600.)
    row = item.actor.return_time_search
    assert row['end_time_s'] == 1603. and row['clock_debt_s'] == 0.
    assert row['search_cutoff_exceeded'] and not row['accepted_for_trial']
    assert item.actor.times == original and not item.settings.trial_calls
    assert item.actor.forecast_count == 3
    assert item.actor.last_calculation_result == {
        'status': 'not_started', 'reason': 'forecast_batch_budget_exceeded',
    }



@pytest.mark.parametrize('field', [None, 'total_budget_s', 'preparation_cutoff_time_s',
    'minimum_admission_budget_s', 'reserved_admission_budget_s', 'screen_budget_s',
    'cutoff_time_s', 'decision_lead_s', 'work_started'])
def test_shared_search_budget_is_independently_recomputed(item, field):
    bounded_comparison(item.actor)
    assert screen.search(item.actor, budget_s=600., cutoff_time_s=1600.)
    study = study_for(item)
    row = study['return_study']['return_time_search']
    if field in row['budget']:
        row['budget'][field] += 1.
    elif field == 'work_started':
        row[field] = False
    elif field is not None:
        row[field] += 1.
    rehash(study)
    if field is None:
        verifier._search_evidence(study, item.actor.deployment_binding)
    else:
        with pytest.raises(ValueError, match='search_preparation_budget'):
            verifier._search_evidence(study, item.actor.deployment_binding)


@pytest.mark.parametrize('mutation', [None, 'deadline', 'names', 'booking', 'missing_budget'])
def test_shared_search_budget_binds_preparation_owner(item, mutation):
    bounded_comparison(item.actor)
    assert screen.search(item.actor, budget_s=600., cutoff_time_s=1600.)
    study = study_for(item)
    returning = study['return_study']
    preparation = {'start_time_s': 1000., 'forecast_count_before': 0,
        'missing_candidate_ids': ['next_orbit'], 'selected_before': None,
        'budget': {'available_sim_s': 600., 'cutoff_time_s': 1600.}}
    returning['comparison_preparations'] = [preparation]
    if mutation == 'deadline':
        preparation['budget']['cutoff_time_s'] += 1.
    elif mutation == 'names':
        preparation['missing_candidate_ids'].append('nominal')
    elif mutation == 'booking':
        preparation['selected_before'] = {'candidate_id': 'nominal'}
    elif mutation == 'missing_budget':
        del returning['return_time_search']['budget']
    rehash(study)
    if mutation is None:
        verifier._search_evidence(study, item.actor.deployment_binding)
    else:
        with pytest.raises(ValueError, match='search_preparation'):
            verifier._search_evidence(study, item.actor.deployment_binding)


@pytest.mark.parametrize('submit_error', [False, True])
def test_screen_pending_receipt_includes_startup_and_cleanup_even_on_failure(item, submit_error):
    actor = item.actor
    bounded_comparison(actor)
    actor.pending = []
    item.settings.startup_elapsed, item.settings.shutdown_elapsed = 20., 7.
    item.settings.submit_error = submit_error
    screen.search(actor, budget_s=600., cutoff_time_s=1600.)
    row = actor.return_time_search
    assert row['pending_operation_index'] == 0 and len(actor.pending) == 1
    operation = actor.pending[0]
    assert operation['start_time_s'] == row['start_time_s'] == 1000.
    assert operation['end_time_s'] == row['end_time_s'] == (1027. if submit_error else 1028.)
    assert operation['wall_time_s'] == row['wall_time_s']
    if submit_error:
        assert operation['expired'] and operation['error_type'] == 'OSError'
    study = study_for(item)
    verifier._search_evidence(study, actor.deployment_binding)
    study['return_study']['pending_operations'][0]['wall_time_s'] += 1.
    with pytest.raises(ValueError, match='search_pending_operation_binding'):
        verifier._search_evidence(study, actor.deployment_binding)



def test_preparation_search_and_four_trials_share_one_real_actor_deadline(monkeypatch, tmp_path):
    from test_starship_replanning_event_runtime import actor_harness, make_candidate
    from src.runtime import starship_replanning_executor as runtime
    from src.runtime.starship_replanning import notices

    actor, clock = actor_harness(monkeypatch, tmp_path, now=1600., case='comparison_update')
    actor.envelope['comparison_preparation']['initial_candidates'] = ['next_orbit']
    actor.deployment_binding = {'retained_count': 0}
    actor.deployment = {'authority': {'return_time_search': deepcopy(screen.CONFIG)}}
    original_time = actor.times['next_orbit']
    base_trial = make_candidate(actor, 'next_orbit')['trials'][0]
    pools = []

    class Pool:
        def __init__(self, **_):
            clock.now += 3.
            self.ready_at = clock.now+10.
            self._processes = {}
            self.jobs, self.closed = [], False
            pools.append(self)

        def submit(self, _function, job):
            self.jobs.append(job)
            origin, _, when, _, _, scale = job
            trial = {**deepcopy(base_trial), 'origin': origin, 'return_time_s': when,
                     'integration_scale': scale, 'wall_time_s': 10.}
            return SimpleNamespace(done=lambda: clock.now >= self.ready_at,
                                   result=lambda: trial)

        def shutdown(self, **_):
            clock.now += 5.
            self.closed = True

    monkeypatch.setattr(runtime, 'ProcessPoolExecutor', Pool)
    monkeypatch.setattr(screen, 'ProcessPoolExecutor', Pool)
    monkeypatch.setattr(screen, 'time', clock)
    monkeypatch.setattr(screen, 'suggest_time', lambda rows, center, areas, notice: {
        'return_time_s': center-40., 'screen_target_area': 'west',
        'method': 'quadratic_distance_screen_not_admission',
    })
    actor.prepare_comparison(notices(actor.case, actor.s.time_s))
    preparation, search = actor.comparison_preparations[0], actor.return_time_search
    assert preparation['status'] == 'completed' and actor.comparison_context['ready']
    assert [len(pool.jobs) for pool in pools] == [3, 4]
    assert all(pool.closed for pool in pools) and actor.forecast_count == 7
    assert actor.times['next_orbit'] == original_time-40.
    assert actor.selected is None and actor.dispatch is None
    assert len(actor.pending) == 2 and search['pending_operation_index'] == 0
    first, second = actor.pending
    assert first['start_time_s'] == preparation['start_time_s'] == 1600.
    assert first['end_time_s'] == second['start_time_s'] == search['end_time_s'] == 1618.
    assert second['end_time_s'] == preparation['end_time_s'] == 1636.
    assert first['wall_time_s'] == second['wall_time_s'] == 18.
    assert first['cutoff_time_s'] < second['cutoff_time_s']
    assert second['cutoff_time_s'] == preparation['budget']['cutoff_time_s'] == 2200.
    assert search['budget']['reserved_admission_budget_s'] == pytest.approx(600.*4/7)
    assert len(actor.candidates['next_orbit']['trials']) == 4



@pytest.mark.parametrize('start', [1460.8, 1600.7])
def test_fractional_clock_budget_rounding_preserves_cap_and_hard_cutoff(item, start):
    actor = item.actor
    bounded_comparison(actor)
    actor.s.time_s = start
    deadline = start+600.
    assert deadline-start != 600.  # Exercise both subtraction rounding signs.
    assert screen.search(actor, budget_s=600., cutoff_time_s=deadline)
    row = actor.return_time_search
    assert row['budget']['total_budget_s'] <= 600.
    assert row['budget']['preparation_cutoff_time_s'] == deadline
    study = study_for(item)
    study['return_study']['comparison_preparations'] = [{
        'start_time_s': start, 'forecast_count_before': 0,
        'missing_candidate_ids': ['next_orbit'], 'selected_before': None,
        'budget': {'available_sim_s': 600., 'cutoff_time_s': deadline},
    }]
    verifier._search_evidence(study, actor.deployment_binding)
    for mutation in ('budget', 'deadline'):
        changed = deepcopy(study)
        owner = changed['return_study']['comparison_preparations'][0]['budget']
        if mutation == 'budget':
            owner['available_sim_s'] += 1e-6
        else:
            owner['cutoff_time_s'] = math.nextafter(deadline, math.inf)
        with pytest.raises(ValueError, match='search_preparation_owner_binding'):
            verifier._search_evidence(changed, actor.deployment_binding)
