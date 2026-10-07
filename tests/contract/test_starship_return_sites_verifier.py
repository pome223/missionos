"""Independent geometry/actor receipt tests; no flight or model execution."""

from copy import deepcopy
from hashlib import sha256
import hmac
import json
import math
from pathlib import Path
import runpy

import pytest

from src.runtime import starship_return_sites as producer
from src.runtime import starship_return_sites_verifier as checker
from src.runtime.starship_booster_recovery_verifier import _flow_and_centroid
from src.runtime.starship_sixdof_mission import _attitude

FIXTURE = runpy.run_path(str(Path(__file__).with_name("test_starship_tower_actor.py")))
KEY = FIXTURE["KEY"]


def inputs():
    return tuple(
        json.loads(Path(name).read_text())
        for name in (
            "examples/spaceflight/starship-return-sites-model-test.json",
            "examples/spaceflight/starship-sixdof-profile.json",
            "examples/spaceflight/starship-catch-profile.json",
        )
    )


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def resign(record):
    unsigned = {k: v for k, v in record.items() if k not in ("signature", "sha256")}
    record["sha256"] = digest(unsigned)
    record["signature"] = hmac.new(
        KEY,
        json.dumps(
            {k: v for k, v in record.items() if k != "signature"},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode(),
        "sha256",
    ).hexdigest()


def verify(record, *, frame=None, observations=None, integration=None, scope=None, key=KEY):
    catalog, profile, catch = inputs()
    return checker.verify_tower_actor(
        record,
        catalog,
        profile,
        catch,
        expected_scope=FIXTURE["scope"]() if scope is None else scope,
        local_integrity_key=key,
        latest_frame=frame,
        expected_observations=observations,
        expected_integration_record_sha256=integration,
    )


def application(tmp_path):
    item = FIXTURE["system"](tmp_path)
    issued = FIXTURE["directive"](item)
    FIXTURE["tick"](item, 13.0, mode="divert")
    before, after = FIXTURE["state"](12.0), FIXTURE["state"](13.0)
    item.actor.acknowledge_application(
        directive_sha256=issued["sha256"],
        before_state=before,
        after_state=after,
        integration_record_sha256="d" * 64,
        guidance_target_evidence=FIXTURE["target"](item, after),
    )
    return item


def test_public_catalog_hash_geometry_and_original_inputs_are_bound_without_success_claims():
    catalog, profile, catch = inputs()
    before = deepcopy((catalog, profile, catch))
    result = checker.verify_return_sites(
        catalog,
        profile,
        catch,
        expected_catalog_sha256=digest(catalog),
        expected_profile_sha256=digest(profile),
        expected_catch_profile_sha256=digest(catch),
    )
    assert result["passed"] and result["capture_geometry_bound"]
    assert (
        result["catalogue_sha256"]
        == producer.ReturnSites.from_dict(catalog, profile=profile, catch_config=catch).sha256
    )
    assert result["declared_parallel_arc_m"] == 30000.0
    assert all(
        result[key] is False
        for key in (
            "surveyed_mission_coordinates",
            "spacex_clearance_verified",
            "safe_landing_verified",
            "wave_or_water_contact_verified",
            "physical_execution",
            "model_value_established",
        )
    )
    assert (catalog, profile, catch) == before


@pytest.mark.parametrize(
    "change", ["schema", "negative_mass", "non_object_group", "non_object_profile"]
)
def test_invalid_original_profile_returns_a_failed_verdict_instead_of_raising(change):
    catalog, profile, catch = inputs()
    if change == "schema":
        profile["schema"] = "different"
    elif change == "negative_mass":
        profile["booster"]["dry_mass_kg"] = -1.0
    elif change == "non_object_group":
        profile["booster"] = []
    else:
        profile = []
    assert not checker.verify_return_sites(catalog, profile, catch)["passed"]


@pytest.mark.parametrize(
    "change",
    [
        "catalog_hash",
        "profile_hash",
        "catch_hash",
        "site_id",
        "site_order",
        "duplicate",
        "extra_site",
        "parallel_distance",
        "capture_points",
        "support_height",
        "elevation",
        "safe_claim",
        "datum",
        "oversized_string",
        "non_json",
        "cycle",
        "boolean",
        "non_dict_site",
    ],
)
def test_catalog_rejects_rebound_geometry_claims_and_unbounded_saved_values(change):
    catalog, profile, catch = inputs()
    kwargs = {
        "expected_catalog_sha256": digest(catalog),
        "expected_profile_sha256": digest(profile),
        "expected_catch_profile_sha256": digest(catch),
    }
    if change.endswith("_hash"):
        kwargs[
            {
                "catalog_hash": "expected_catalog_sha256",
                "profile_hash": "expected_profile_sha256",
                "catch_hash": "expected_catch_profile_sha256",
            }[change]
        ] = "f" * 64
    elif change == "site_id":
        catalog["sites"][1]["site_id"] = "safe_ocean"
    elif change == "site_order":
        catalog["sites"].reverse()
    elif change == "duplicate":
        catalog["sites"][1] = deepcopy(catalog["sites"][0])
    elif change == "extra_site":
        catalog["sites"].append(deepcopy(catalog["sites"][1]))
    elif change == "parallel_distance":
        catalog["sites"][1]["longitude_deg"] += 0.001
    elif change == "capture_points":
        catalog["sites"][0]["support_points_body_m"][0][2] += 1.0
    elif change == "support_height":
        catch["support_height_m"] += 1.0
    elif change == "elevation":
        catalog["sites"][1]["elevation_m"] = 1.0
    elif change == "safe_claim":
        catalog["safe_landing_area_verified"] = True
    elif change == "datum":
        catalog["coordinate_datum"] = "mean_sea_level"
    elif change == "oversized_string":
        catalog["profile_id"] = "x" * 20000
    elif change == "non_json":
        catalog["sites"] = tuple(catalog["sites"])
    elif change == "cycle":
        catalog["cycle"] = catalog
    elif change == "boolean":
        catalog["sites"][1]["latitude_deg"] = True
    else:
        catalog["sites"][1] = 2
    assert not checker.verify_return_sites(catalog, profile, catch, **kwargs)["passed"]


@pytest.mark.parametrize("time", [0.0, 600.0, 3600.0])
def test_independent_wgs84_frame_matches_declared_rotation_and_orthonormal_basis(time):
    catalog, profile, catch = inputs()
    sites = producer.ReturnSites.from_dict(catalog, profile=profile, catch_config=catch)
    actual = checker.return_site_frame(catalog["sites"][1], time)
    expected = producer.return_site_frame(sites.divert, time)
    for key in ("origin_eci_m", "origin_velocity_eci_mps", "east_eci", "north_eci", "up_eci"):
        assert actual[key] == pytest.approx(expected[key], abs=1e-7)
    axes = [actual[key] for key in ("east_eci", "north_eci", "up_eci")]
    assert all(math.hypot(*axis) == pytest.approx(1.0, abs=1e-12) for axis in axes)
    assert all(
        sum(a * b for a, b in zip(axes[i], axes[j])) == pytest.approx(0.0, abs=1e-12)
        for i, j in ((0, 1), (0, 2), (1, 2))
    )
    assert actual["synthetic_model_site"] is True


def test_material_pin_velocity_uses_achieved_propellant_flow_omega_and_earth_rotation():
    catalog, profile, catch = inputs()
    site = catalog["sites"][0]
    frame = checker.return_site_frame(site, 600.0)
    state = FIXTURE["state"](600.0)
    state["q_body_to_eci"] = [
        float(x) for x in _attitude(tuple(frame["up_eci"]), tuple(frame["east_eci"]))
    ]
    state["r_eci_m"] = [p + 75.4 * u for p, u in zip(frame["origin_eci_m"], frame["up_eci"])]
    state["v_eci_mps"] = [
        -7.292115e-5 * state["r_eci_m"][1],
        7.292115e-5 * state["r_eci_m"][0],
        0.0,
    ]
    state["omega_body_rad_s"] = [0.001, -0.002, 0.003]
    state["engine_states"][0]["throttle"] = 0.5
    before = deepcopy(state)
    _, com, com_rate, _ = _flow_and_centroid(state, profile)
    declared = producer.ReturnSites.from_dict(catalog, profile=profile, catch_config=catch)
    expected = producer.state_errors(
        declared.capture, state, com_body_m=com, com_rate_body_mps=com_rate
    )
    actual = checker.state_errors(site, state, profile)
    assert checker.verify_site_errors(expected, site, state, profile)["passed"]
    assert actual["surrogate_pins"][0]["velocity_enu_mps"] == pytest.approx(
        expected["surrogate_pins"][0]["velocity_enu_mps"], abs=1e-8
    )
    without_flow = deepcopy(state)
    without_flow["engine_states"][0]["throttle"] = 0.0
    changed = checker.state_errors(site, without_flow, profile)
    assert (
        actual["surrogate_pins"][0]["velocity_enu_mps"][2]
        != changed["surrogate_pins"][0]["velocity_enu_mps"][2]
    )
    assert actual["contact_or_support_verified"] is False and state == before


@pytest.mark.parametrize(
    "field", ["ground_velocity_enu_mps", "surrogate_pins", "contact_or_support_verified", "site_id"]
)
def test_geometry_receipt_rejects_changed_arithmetic_or_promoted_contact(field):
    catalog, profile, _ = inputs()
    state = FIXTURE["state"](600.0)
    site = catalog["sites"][0]
    record = checker.state_errors(site, state, profile)
    if field == "ground_velocity_enu_mps":
        record[field][1] += 1.0
    elif field == "surrogate_pins":
        record[field][0]["velocity_enu_mps"][2] += 1.0
    elif field == "site_id":
        record[field] = "divert"
    else:
        record[field] = True
    assert not checker.verify_site_errors(record, site, state, profile)["passed"]


def test_directive_without_application_is_not_mode_change_or_flight_improvement(tmp_path):
    item = FIXTURE["system"](tmp_path)
    FIXTURE["directive"](item)
    record = item.actor.receipt()
    before = deepcopy(record)
    result = verify(record)
    assert result["passed"] and result["identity_signature_valid"] and result["directive_emitted"]
    assert not result["actual_guidance_mode_observed"]
    assert (
        not result["physical_effect_verified"] and not result["physical_surface_outcome_verified"]
    )
    assert record == before


def test_signed_fixture_ack_needs_separate_controller_observations_and_integration_digest(tmp_path):
    item = application(tmp_path)
    record = item.actor.receipt()
    result = verify(record)
    assert result["passed"] and result["application_signature_valid"]
    assert not result["actual_guidance_mode_observed"]
    observations = deepcopy(record["supervision"]["observations"])
    result = verify(record, observations=observations)
    assert result["passed"] and not result["actual_guidance_mode_observed"]
    result = verify(record, observations=observations, integration="d" * 64)
    assert result["passed"] and result["caller_integration_digest_matched"]
    assert (
        result["actual_guidance_mode_observed"] and result["actual_guidance_mode_change_observed"]
    )
    assert all(
        result[key] is False
        for key in (
            "integration_independently_verified",
            "physical_execution",
            "physical_effect_verified",
            "physical_surface_outcome_verified",
            "flight_outcome_improvement",
            "safe_landing_verified",
            "model_value_established",
            "os_process_liveness_verified",
            "human_identity_verified",
        )
    )


@pytest.mark.parametrize(
    "change",
    [
        "key",
        "scope",
        "identity",
        "before_state",
        "after_state",
        "target",
        "target_origin",
        "integration",
        "app_claim",
        "actor_claim",
        "source",
        "site",
        "clock",
        "extra_field",
        "signature",
    ],
)
def test_signed_actor_cannot_transplant_sites_context_states_or_later_application(change, tmp_path):
    item = application(tmp_path)
    record = deepcopy(item.actor.receipt())
    kwargs = {}
    app = record["application_evidence"]
    if change == "key":
        kwargs["key"] = b"other-explicit-fixture-integrity-key-32bytes"
    elif change == "scope":
        kwargs["scope"] = {**FIXTURE["scope"](), "approval_record_sha256": "f" * 64}
    elif change == "identity":
        record["identity"]["actor_pid"] = 0
        resign(record["identity"])
    elif change == "before_state":
        app["before_state_sha256"] = "f" * 64
    elif change == "after_state":
        app["after_state_sha256"] = "f" * 64
    elif change == "target":
        app["guidance_target_evidence"]["active_site_id"] = "capture"
    elif change == "target_origin":
        app["guidance_target_evidence"]["target_origin_eci_m"][0] += 1.0
    elif change == "integration":
        kwargs["integration"] = "f" * 64
    elif change == "app_claim":
        app["physical_effect_verified"] = True
    elif change == "actor_claim":
        record["physical_effect_verified"] = True
    elif change == "source":
        record["identity"]["source_sha256"] = "f" * 64
        resign(record["identity"])
    elif change == "site":
        record["site_observations"][-1]["active_site_id"] = "capture"
    elif change == "clock":
        app["integration_end_time_s"] = app["integration_start_time_s"]
    elif change == "extra_field":
        app["safe_landing"] = True
    else:
        app["signature"] = "f" * 64
    if change != "signature":
        resign(app)
    assert not verify(record, **kwargs)["passed"]


def test_signed_end_record_and_inactive_ipc_keep_last_physical_clock_unchanged(tmp_path):
    item = FIXTURE["system"](tmp_path)
    FIXTURE["tick"](item)
    item.clock.now += 0.5
    record = item.actor.close()
    frame = json.loads((item.mailbox / "latest-observation.json").read_text())
    result = verify(record, frame=frame)
    assert result["passed"] and result["ended_signature_valid"]
    assert not result["os_process_liveness_verified"]
    changed = deepcopy(frame)
    changed["simulation_time_s"] += 0.5
    assert not verify(record, frame=changed)["passed"]
    changed = deepcopy(record)
    changed["ended"]["last_state_sha256"] = "f" * 64
    resign(changed["ended"])
    assert not verify(changed, frame=frame)["passed"]


def test_inactive_tick_can_precede_actor_close_without_becoming_alive(tmp_path):
    item = FIXTURE["system"](tmp_path)
    item.actor.tick(
        FIXTURE["state"](),
        tower_ready=True,
        return_mode="capture",
        active_site_id="capture",
        source_sha256=FIXTURE["SOURCE"],
        plan_sha256=FIXTURE["CONTEXT"]["plan_sha256"],
        run_active=False,
    )
    frame = json.loads((item.mailbox / "latest-observation.json").read_text())
    assert verify(item.actor.receipt(), frame=frame)["passed"]
    frame["run_active"] = True
    assert not verify(item.actor.receipt(), frame=frame)["passed"]


def test_actor_ended_without_first_observation_has_no_invented_state(tmp_path):
    item = FIXTURE["system"](tmp_path)
    record = item.actor.close()
    result = verify(record)
    assert result["passed"] and result["ended_signature_valid"]
    assert record["ended"]["last_state_sha256"] is None


@pytest.mark.parametrize("change", ["channels", "propellant", "fin", "gimbal"])
def test_generic_tower_observation_must_still_match_the_supplied_booster_profile(tmp_path, change):
    item = FIXTURE["system"](tmp_path)
    state = FIXTURE["state"]()
    if change == "channels":
        state["engine_states"].pop()
    elif change == "propellant":
        state["propellant_kg"] = 4_000_000.0
    elif change == "fin":
        state["flap_angles_rad"][0] = 0.1
    else:
        state["engine_states"][14]["gimbal_x_rad"] = 0.01
    item.actor.tick(
        state,
        tower_ready=True,
        return_mode="capture",
        active_site_id="capture",
        source_sha256=FIXTURE["SOURCE"],
        plan_sha256=FIXTURE["CONTEXT"]["plan_sha256"],
    )
    assert not verify(item.actor.receipt())["passed"]


def test_checker_import_boundary_excludes_actor_site_producer_integrator_and_provider():
    import ast

    imports = [
        node.module
        for node in ast.walk(ast.parse(Path(checker.__file__).read_text()))
        if isinstance(node, ast.ImportFrom)
    ]
    assert all(
        name
        not in (
            "starship_tower_actor",
            "starship_return_sites",
            "starship_sixdof",
            "starship_physics",
            "starship_constrained_recovery",
            "jev_assurance",
        )
        for name in imports
    )
