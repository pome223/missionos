"""Reviewed historical recovery evidence for host-side mission judgment.

This is past development evidence, not current telemetry, a reliability estimate
or permission to execute capture. No private raw run or provider credential is
read by the evidence loader.
"""
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INITIAL = 'docs/assets/starship-catch-guidance-search-20261013/summary.json'
CONTINUATION = 'docs/assets/starship-catch-guidance-search-20261013/continuation.json'
WATER = 'docs/assets/starship-m3-final-20261012/summary.json'


def recovery_evidence(root=ROOT):
    records, sources = {}, {}
    for name in (INITIAL, CONTINUATION, WATER):
        raw = (Path(root)/name).read_bytes()
        records[name] = json.loads(raw)
        sources[name] = sha256(raw).hexdigest()
    first, later, water = (records[n] for n in (INITIAL, CONTINUATION, WATER))
    if (first['schema'] != 'missionos.starship_catch_guidance_search_reviewed_scalars.v1'
            or later['schema'] != 'missionos.starship_catch_guidance_continuation_scalars.v1'
            or water['schema'] != 'missionos.starship_m3_final_report.v1'
            or later['extends_studies_01_to_15'] is not True):
        raise ValueError('unregistered_booster_recovery_evidence')
    completed = first['totals']['candidate_runs']
    for study in later['studies']:
        if study['kind'] != 'full_forecast_study':
            continue
        cases = study.get('candidates', [study['candidate']] if 'candidate' in study else [])
        completed += sum(c.get('outcome') in ('surface_contact', 'catch_handoff') for c in cases)
    handoffs = first['catch_handoffs']+later['candidate_handoffs_demonstrated']
    supports = first['supported_catches']+later['supported_catches']
    if any(type(v) is not int or v < 0 for v in (completed, handoffs, supports)):
        raise ValueError('invalid_booster_recovery_counts')
    nominal = water['roles']['normal']['metrics']['booster']
    repeat = water['booster_reproducibility']
    return {
        'schema': 'missionos.booster_recovery_history.v1',
        'source_sha256': sources,
        'catch': {
            'completed_development_candidates': completed,
            'launch_derived_arrival_handoffs_demonstrated': handoffs,
            'launch_derived_supported_catches_demonstrated': supports,
            'launch_derived_catch_has_ever_succeeded': supports > 0,
            'evidence_scope': 'Sequential numerical trials from one saved launch-derived separation; not independent launches.',
            'success_rate_estimated': False,
            'partial_offline_and_unretained_trials_excluded_from_count': True,
            'near_tower_initialized_support_is_launch_derived_success': False,
        },
        'splashdown': {
            'modeled_water_entry_met': nominal['water_entry_met'],
            'contact_speed_mps': nominal['contact_speed_mps'],
            'remaining_propellant_kg': nominal['propellant_kg'],
            'unique_trajectory_count': repeat['unique_trajectory_count'],
            'nominal_separation_only': repeat['nominal_separation_only'],
            'perturbation_robustness_qualified': nominal['perturbation_robustness_qualified'],
            'current_state_feasibility_established_by_this_history': False,
        },
        'interpretation': 'Use demonstrated recovery maturity as judgment evidence alongside current observations and constraints. Zero demonstrated catches does not prove physical impossibility. A nominal splashdown is not assurance for arbitrary fuel, weather or separation states.',
        'grants_execution_authority': False,
        'physical_execution': False,
    }



def model_history(evidence=None):
    """Whitelist only the user-authorized facts for external model inputs."""
    e = recovery_evidence() if evidence is None else evidence
    return {
        'scope': 'This development simulator; historical evidence, not current telemetry.',
        'catch': {
            'launch_derived_supported_catches_demonstrated': e['catch']['launch_derived_supported_catches_demonstrated'],
            'launch_derived_catch_has_ever_succeeded': e['catch']['launch_derived_catch_has_ever_succeeded'],
        },
        'splashdown': {
            'modeled_water_entry_met': e['splashdown']['modeled_water_entry_met'],
            'nominal_separation_only': e['splashdown']['nominal_separation_only'],
            'perturbation_robustness_qualified': e['splashdown']['perturbation_robustness_qualified'],
        },
    }


def comparison_payload(evidence, model):
    from .space_ops_investigator import canonical_bytes
    context = {'scope': 'historical_booster_recovery_objective_assessment',
        'objectives_to_compare': ['capture', 'splashdown'],
        'current_vehicle_state_provided': False,
        'execution_permitted_actions': ['splashdown'],
        'capture_controller_connected': False,
        'recovery_history': model_history(evidence)}
    identity = sha256(canonical_bytes(context)).hexdigest()
    payload = {'model': model, 'stream': False, 'thinking': {'type': 'disabled'},
        'temperature': 0, 'max_tokens': 800, 'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': 'Assess which recovery objective to prefer for the present development simulator, using the supplied historical evidence, including whether a launch-derived catch has ever succeeded. Compare capture and splashdown. Do not invent current vehicle data, assert impossibility from past failures, or assume splashdown is robust beyond its tested domain. This assessment grants no execution authority; current-state feasibility and human-approved scope remain independent. Return JSON with exactly action (capture or splashdown) and summary (brief reasoning). No additional fields.'},
            {'role': 'user', 'content': canonical_bytes(context).decode()}]}
    return context, identity, payload


def assess_recovery(transport, evidence, model):
    from .space_ops_investigator import _json
    from .starship_flight_supervisor import _receipt
    context, identity, payload = comparison_payload(evidence, model)
    receipt = _receipt('live', 'deepseek', context)
    receipt.update(maximum_output_tokens=payload['max_tokens'], maximum_instance_calls=None)

    def reject(reason):
        receipt['validation_failure_reason'] = reason
        raise ValueError('invalid_recovery_response')

    def decode(value):
        known_models = ('deepseek-chat', 'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4.1-flash')
        reported = value.get('model') if type(value) is dict else None
        receipt['reported_model_id'] = reported if reported in known_models else 'other_or_missing'
        if (type(value) is not dict or value.get('model') != model
                or type(value.get('choices')) is not list or len(value['choices']) != 1):
            reject('model_or_choices_shape')
        choice = value['choices'][0]
        if choice.get('finish_reason') != 'stop':
            reject('completion_not_stop')
        message = choice['message']
        if (message.get('role') != 'assistant' or message.get('tool_calls') or message.get('function_call')
                or type(message.get('content')) is not str):
            reject('message_shape')
        try:
            answer = _json(message['content'])
        except (ValueError, TypeError):
            reject('answer_json')
        if (type(answer) is not dict or set(answer) != {'action', 'summary'}
                or answer['action'] not in ('capture', 'splashdown')
                or type(answer['summary']) is not str or not 1 <= len(answer['summary']) <= 1600):
            reject('answer_shape_or_choice')
        return answer, model

    answer = transport._invoke('deepseek', payload, receipt, decode)
    return {'schema': 'missionos.booster_recovery_assessment.v1', 'context': context,
        'evidence_sha256': identity, 'answer': answer, 'llm_invocation': receipt,
        'execution_dispatched': False, 'new_flight_executed': False,
        'free_text_reasoning_verified': False, 'local_history_sources': evidence['source_sha256']}


REPORT_SOURCES = (INITIAL, CONTINUATION, WATER,
    "docs/assets/starship-catch-intercept-20261013/summary.json",
    "docs/assets/starship-catch-approach-diagnosis-20261014/summary.json")


def mission_recovery_report(root=ROOT):
    """Portable, frozen judgment material; no private run, keys or hidden state."""
    history = recovery_evidence(root)
    intercept = json.loads((Path(root) / REPORT_SOURCES[3]).read_text())
    diagnosis = json.loads((Path(root) / REPORT_SOURCES[4]).read_text())
    latest = next(s for s in intercept["studies"] if s["study_number"] == 60)
    if (intercept["launch_derived_supported_catches_demonstrated"] != 0
            or diagnosis["launch_derived_catch_demonstrated"] is not False):
        raise ValueError("recovery_report_requires_review_after_new_catch")
    return {
        "schema": "missionos.booster_recovery_mission_report.v1",
        "scope": "Past development simulator evidence; not current telemetry or flight certification.",
        "catch": {**history["catch"],
            "latest_launch_derived_trial": {
                "contact_speed_mps": latest["outcome"]["final_ground_speed_mps"],
                "remaining_propellant_kg": latest["fuel_kg"],
                "arrival_handoff_reached": latest["outcome"]["handoff_reached"],
                "arm_support_executed": latest["arm_continuation"]["executed"],
            },
            "diagnosis": "Saved plan, conditioned reference and actual trajectory differ; all warm replans timed out. The causal effect of conditioning/allocation is not proven. No repair has been admitted.",
            "diagnosis_causal_effect_proven": False,
            "capture_controller_connected": False,
            "physical_impossibility_proven": False,
        },
        "splashdown": history["splashdown"],
        "interpretation": history["interpretation"],
        "grants_execution_authority": False,
        "physical_execution": False,
    }
