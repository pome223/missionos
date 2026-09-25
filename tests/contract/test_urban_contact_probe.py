"""Contact evidence must fail closed while releasing transport callbacks."""

from types import SimpleNamespace

import pytest

from scripts import urban_headroom_contact_probe as probe


class Node:
    def __init__(self, reject=None, release_failure=None):
        self.callbacks = {}
        self.released = []
        self.reject = reject
        self.release_failure = release_failure

    def subscribe(self, message_type, topic, callback):
        if topic == self.reject:
            return False
        self.callbacks[topic] = callback
        return True

    def unsubscribe(self, topic):
        self.released.append(topic)
        if topic == self.release_failure:
            raise RuntimeError("injected cleanup error")
        del self.callbacks[topic]
        return True

    def subscribed_topics(self):
        return list(self.callbacks)


def test_partial_subscription_failure_releases_already_registered_callback():
    node = Node(reject="pose")
    with pytest.raises(RuntimeError, match="subscriptions rejected"):
        with probe.subscriptions(node, [(None, "contact", None), (None, "pose", None)]):
            pytest.fail("must not enter the probe")
    assert node.released == ["contact"]
    assert node.subscribed_topics() == []


def test_cleanup_failure_attempts_other_subscription_and_is_not_success():
    node = Node(release_failure="pose")
    with pytest.raises(RuntimeError, match="subscriptions not released"):
        with probe.subscriptions(node, [(None, "contact", None), (None, "pose", None)]):
            pass
    assert node.released == ["pose", "contact"]
    assert node.subscribed_topics() == ["pose"]


@pytest.mark.parametrize(
    "failure", [None, "create", "no_contact", "remove", "no_removal_pose"]
)
def test_probe_releases_callbacks_on_success_and_service_or_observation_failure(
    monkeypatch, failure
):
    node = Node()
    clock = [0.0]
    spawned = [False]
    removed = [False]
    services = []
    name = "headroom_contact_positive_control"

    def monotonic():
        clock[0] += 0.1
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds
        callbacks = list(node.callbacks.values())
        if len(callbacks) != 2:
            return
        if spawned[0] and not removed[0]:
            callbacks[1](SimpleNamespace(pose=[SimpleNamespace(name=name)]))
            if failure != "no_contact":
                callbacks[0](
                    SimpleNamespace(
                        contact=[
                            SimpleNamespace(
                                collision1=SimpleNamespace(name=name),
                                collision2=SimpleNamespace(name="building"),
                            )
                        ]
                    )
                )
        if removed[0] and failure != "no_removal_pose":
            callbacks[1](SimpleNamespace(pose=[]))

    def service(endpoint, *_args):
        services.append(endpoint)
        if endpoint == failure:
            raise RuntimeError("injected " + endpoint)
        if endpoint == "create":
            spawned[0] = True
        else:
            removed[0] = True

    monkeypatch.setattr(probe.time, "monotonic", monotonic)
    monkeypatch.setattr(probe.time, "sleep", sleep)
    monkeypatch.setattr(probe, "service", service)
    building = {"name": "fixture", "lower_enu_m": [0, 0, 0], "upper_enu_m": [2, 2, 2]}
    if failure:
        with pytest.raises(RuntimeError):
            probe.observe_probe(building, node, None, None)
    else:
        result = probe.observe_probe(building, node, None, None)
        assert result["probe_contacts"] > 0
        assert result["probe_removed_observed"] and result["subscriptions_released"]
        assert result["aircraft_commands_sent"] is False
        assert result["model_invoked"] is False
    assert services == (["create"] if failure == "create" else ["create", "remove"])
    assert len(node.released) == 2
    assert node.subscribed_topics() == []


def test_probe_fix_does_not_allow_resuming_old_frozen_cohort(tmp_path, monkeypatch):
    import json
    from scripts import px4_urban_headroom_trial as trial

    trial.freeze(tmp_path / "cohort")
    frozen_path = tmp_path / "cohort/protocol.json"
    frozen = json.loads(frozen_path.read_text())
    frozen["source_sha256"]["scripts/urban_headroom_contact_probe.py"] = "0" * 64
    frozen_path.write_text(json.dumps(frozen))
    monkeypatch.setattr(
        trial, "setup", lambda *a, **k: pytest.fail("must reject before setup")
    )
    with pytest.raises(ValueError, match="implementation changed after freeze"):
        trial.run_case(SimpleNamespace(output_dir=tmp_path / "cohort"), trial.CASES[0])


def test_published_shutdown_checks_do_not_reclassify_abort_or_reopen_gpu_gate():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    report = json.loads(
        (root / "docs/assets/urban-contact-shutdown-20260924/summary.json").read_text()
    )
    assert [r["exit_code"] for r in report["legacy_attempts"]] == [0] * 7 + [134]
    positive = (
        report["fixed_checks_original_simulator"]
        + report["fixed_checks_fresh_simulator"][:-1]
    )
    assert len(positive) == report["fixed_positive_checks"] == 50
    for row in positive:
        assert row["exit_code"] == 0
        assert row["receipt"]["probe_contacts"] > 0
        assert row["receipt"]["probe_removed_observed"] is True
        assert row["receipt"]["subscriptions_released"] is True
        assert row["receipt"]["aircraft_commands_sent"] is False
        assert row["receipt"]["model_invoked"] is False
    negative = report["fixed_checks_fresh_simulator"][-1]
    assert negative["kind"] == "missing_contact_topic"
    assert negative["exit_code"] == 1 and negative["receipt"] is None
    assert report["expected_negative_checks"] == 1
    assert report["frozen_cohort_decision"] == "incomplete_stop"
    for key in (
        "gpu_gate_passed",
        "frozen_cohort_changed",
        "learned_navigation_benefit_established",
        "native_root_cause_proven",
        "aircraft_controller_started",
    ):
        assert report[key] is False
    for key in ("new_flights", "new_model_calls", "new_rented_gpu_instances"):
        assert report[key] == 0
