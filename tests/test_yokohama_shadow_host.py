"""Detached native-protocol ownership checks; no HTTP, model, or vehicle calls."""

import copy
import json

import pytest

from scripts import ship_anwm, yokohama_decision_host as module
from scripts.yokohama_decision_host import DecisionHost, validate_native_services


def pinned_services(*, fixture=False, compact=True):
    sources = {
        name: str(index) * 64
        for index, name in enumerate((
            "yokohama_appearance.py", "yokohama_wam_profile.py", "ship_anwm_server.py",
            "ship_anwm.py", "ship_aerovla_server.py", "ship_aerovla.py",
        ), 1)
    }
    services = {
        "vla": dict(
            cpu_between_requests=True, exit_after_request=False, short_segment_flight=True,
            compact_city_flight=compact, yaw_bin_range=[45, 53] if compact else [38, 60],
            forward_bin_range=[20, 58], hold_bin_allowed=False, terminal_proposal_allowed=False,
            decoding_policy="aerovla_compact_city_grammar.v2",
            runtime_sha256={name: sources[name] for name in (
                "ship_aerovla_server.py", "ship_aerovla.py", "ship_anwm.py",
            )},
            session_uuid="1" * 32,
        ),
        "wam": dict(
            cpu_between_requests=True,
            adapter_sha256=ship_anwm.MOTION_ADAPTER_SHA256 if compact else None,
            model_time_index=1,
            appearance_policy=ship_anwm.APPEARANCE_POLICY,
            appearance_sha256=sources["yokohama_appearance.py"],
            profile_sha256=sources["yokohama_wam_profile.py"],
            candidate_contracts=[
                ship_anwm.MOTION_CONTRACT if compact else "yokohama_anwm_request.v1"
            ],
            server_sha256=sources["ship_anwm_server.py"],
            helper_sha256=sources["ship_anwm.py"],
            checkpoint_sha256=ship_anwm.MODEL_SHA256,
            upstream_revision=ship_anwm.UPSTREAM_REVISION,
            session_uuid="2" * 32,
        ),
    }
    if fixture:
        for identity in services.values():
            identity["fixture"] = True
    return {"decisions": {"wam_profile": "motion-v4" if compact else "legacy"}}, services, sources


def make_host(tmp_path, *, mock=False, services=None):
    config, expected, sources = pinned_services(fixture=mock)
    actual = expected if services is None else services
    calls = []

    def no_network(port, path, payload=None):
        calls.append((port, path, payload))
        assert path == "health" and payload is None
        return copy.deepcopy(actual["vla" if port == 10001 else "wam"])

    host = DecisionHost(
        tmp_path, config, tmp_path, "native", {"vla_port": 10001, "wam_port": 10002},
        detached=True, mock_http=mock, http_exchange=no_network,
    )
    return host, expected, sources, calls


@pytest.fixture(autouse=True)
def prohibit_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Detached host attempted a process or real HTTP request")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module, "exchange", forbidden)


@pytest.mark.parametrize("compact", [False, True])
def test_pure_identity_contract_does_not_mutate_inputs(compact):
    config, services, sources = pinned_services(compact=compact)
    before = copy.deepcopy((config, services, sources))
    validate_native_services(config, services, sources)
    assert (config, services, sources) == before


@pytest.mark.parametrize("role,key,value", [
    ("vla", "cpu_between_requests", False),
    ("wam", "cpu_between_requests", False),
    ("vla", "exit_after_request", True),
    ("vla", "short_segment_flight", False),
    ("vla", "yaw_bin_range", [38, 60]),
    ("vla", "forward_bin_range", [0, 98]),
    ("vla", "compact_city_flight", False),
    ("vla", "hold_bin_allowed", True),
    ("vla", "terminal_proposal_allowed", True),
    ("vla", "decoding_policy", "other"),
    ("vla", "runtime_sha256", {}),
    ("wam", "candidate_contracts", []),
    ("wam", "adapter_sha256", None),
    ("wam", "appearance_sha256", "0" * 64),
    ("wam", "profile_sha256", "0" * 64),
    ("wam", "server_sha256", "0" * 64),
    ("wam", "helper_sha256", "0" * 64),
    ("wam", "checkpoint_sha256", "0" * 64),
    ("wam", "upstream_revision", "other"),
])
def test_pure_identity_contract_rejects_changed_source_or_model(role, key, value):
    config, services, sources = pinned_services()
    services[role][key] = value
    with pytest.raises(ValueError):
        validate_native_services(config, services, sources)


def test_pure_identity_requires_complete_hashes():
    config, services, sources = pinned_services()
    del sources["ship_anwm.py"]
    with pytest.raises(ValueError, match="source hashes"):
        validate_native_services(config, services, sources)


@pytest.mark.parametrize("mock", [False, True])
def test_attach_then_close_only_revokes_local_state(tmp_path, mock):
    host, expected, sources, calls = make_host(tmp_path, mock=mock)
    assert host.thread is None and host.process is None and not host.active
    identity = host.attach_existing_services(expected_services=expected, source_hashes=sources)
    assert calls == [(10001, "health", None), (10002, "health", None)]
    assert identity["backend"] == "native" and identity["externally_owned_services"] is True
    # Caller mutations cannot change the pinned session identity.
    expected["vla"]["session_uuid"] = "changed"
    identity["services"]["vla"]["session_uuid"] = "changed"
    assert host.identity["services"]["vla"]["session_uuid"] == "1" * 32
    host.pending[1] = {"proposal": "local-only"}
    receipt = host.close()
    assert receipt == {
        "remote_cleanup_verified": False, "externally_owned_services": True,
        "session_revoked": True,
    }
    assert host.closed and not host.active and host.pending == {}
    assert json.loads((host.folder / "shutdown.json").read_text()) == receipt
    assert host.stop() == host.close() == receipt
    assert len(calls) == 2
    with pytest.raises(ValueError, match="single use"):
        host.attach_existing_services(expected_services=expected, source_hashes=sources)
    assert len(calls) == 2


@pytest.mark.parametrize("operation", ["start", "lifecycle", "authorize", "activate"])
def test_detached_does_not_expose_lifecycle_or_dispatch(tmp_path, operation):
    host, _, _, calls = make_host(tmp_path)
    arguments = {"start": (), "lifecycle": ("stop",), "authorize": ({}, tmp_path),
                 "activate": ({}, tmp_path)}
    with pytest.raises(ValueError, match="Detached shadow host"):
        getattr(host, operation)(*arguments[operation])
    assert calls == []
    host.close()


@pytest.mark.parametrize("operation", ["vla", "wam"])
def test_detached_inference_requires_active_attachment(tmp_path, operation):
    host, _, _, calls = make_host(tmp_path)
    for closed in (False, True):
        if closed:
            host.close()
        with pytest.raises(ValueError, match="revoked or not attached"):
            getattr(host, operation)({}, tmp_path)
    assert calls == []


def test_attach_rejects_changed_live_identity_and_cannot_retry(tmp_path):
    _, changed, _ = pinned_services()
    changed["vla"]["session_uuid"] = "3" * 32
    host, expected, sources, calls = make_host(tmp_path, services=changed)
    with pytest.raises(ValueError, match="approved identity"):
        host.attach_existing_services(expected_services=expected, source_hashes=sources)
    assert not host.active
    with pytest.raises(ValueError, match="single use"):
        host.attach_existing_services(expected_services=expected, source_hashes=sources)
    assert len(calls) == 2
    assert host.close()["remote_cleanup_verified"] is False


def test_partial_attachment_failure_never_stops_external_service(tmp_path):
    host, expected, sources, calls = make_host(tmp_path)
    original = host.http_exchange

    def fail_second(port, path):
        if port == 10002:
            raise TimeoutError("offline synthetic health timeout")
        return original(port, path)

    host.http_exchange = fail_second
    with pytest.raises(TimeoutError):
        host.attach_existing_services(expected_services=expected, source_hashes=sources)
    assert len(calls) == 1 and not host.active
    assert host.close()["externally_owned_services"] is True


@pytest.mark.parametrize("mock,fixture", [(False, True), (True, False)])
def test_fixture_identities_cannot_be_relabelled_native_or_reverse(tmp_path, mock, fixture):
    _, actual, sources = pinned_services(fixture=fixture)
    host, _, _, _ = make_host(tmp_path, mock=mock, services=actual)
    with pytest.raises(ValueError, match="fixture service identities"):
        host.attach_existing_services(expected_services=actual, source_hashes=sources)
    assert not host.active
    host.close()


def test_mock_http_cannot_enable_normal_mailbox(tmp_path):
    with pytest.raises(ValueError, match="requires a detached"):
        DecisionHost(tmp_path, {}, tmp_path, "native", mock_http=True)
    assert not (tmp_path / "decisions").exists()


def test_default_fixture_mailbox_lifecycle_still_works(tmp_path):
    host = DecisionHost(tmp_path, {"decisions": {}}, tmp_path, "fixture")
    assert host.thread.is_alive()
    assert host.start() == {"backend": "fixture", "models_invoked": False}
    with pytest.raises(ValueError, match="single use"):
        host.start()
    assert host.close() == {"fixture_stopped": True, "session_revoked": True}
    assert not host.thread.is_alive()


def test_capture_root_is_separate_from_output_and_confines_paths(tmp_path, monkeypatch):
    source = tmp_path / "input"
    source.mkdir()
    capture = source / "capture.json"
    capture.write_text("{}")
    output = tmp_path / "output"
    output.mkdir()
    host = DecisionHost(output, {"decisions": {}}, source, "native", detached=True,
                        capture_root=source)
    seen = []

    def read_capture(path, **kwargs):
        seen.append(path)
        return {}, {"stamps_ns": [5_000_000_000]}

    monkeypatch.setattr(module, "load_capture", read_capture)
    request = {"capture": {"file": "capture.json", "sha256": ship_anwm.digest(capture)},
               "observation": {"sim_s": 5}}
    assert host.capture(request)[0] == capture
    assert seen == [capture] and not (output / "capture.json").exists()
    request["capture"]["file"] = "../outside.json"
    with pytest.raises(ValueError, match="Unbound city capture"):
        host.capture(request)
    assert seen == [capture]
    host.close()


def test_legacy_uninitialized_test_host_keeps_global_exchange_fallback(monkeypatch):
    host = object.__new__(DecisionHost)
    monkeypatch.setattr(module, "exchange", lambda *args: args)
    assert host._exchange(10001, "health") == (10001, "health")
