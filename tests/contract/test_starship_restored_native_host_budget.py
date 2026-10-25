"""Host JSON input budget only; no dynamics, model, network or cohort execution."""
from copy import deepcopy
import gzip
from hashlib import sha256
import inspect
import socket
import subprocess

import pytest

from src.runtime import starship_restored_native_verifier as native
from tests.contract.test_starship_restored_native_verifier import negative as native_negative


@pytest.fixture
def negative(monkeypatch):
    return native_negative.__wrapped__(monkeypatch)


@pytest.fixture(autouse=True)
def saved_only(monkeypatch):
    from src.runtime import starship_sixdof as dyn, starship_physics as env
    from src.runtime import starship_restored_return as returning, starship_sixdof_mission as mission
    from src.intelligence.starship_restored_agent import RestoredAgent
    def forbidden(*_a, **_kw):
        raise AssertionError("Host-budget verification cannot run physics, models or processes")
    for owner, name in ((dyn, "step"), (dyn, "_rk4"), (dyn, "_rhs"), (env, "step"),
        (mission, "advance_plant"), (mission, "simulate_concurrent"), (returning, "advance_plant"),
        (returning, "control"), (returning, "forecast_restored_return"), (RestoredAgent, "assess"),
        (socket, "socket"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, name, forbidden)


def test_default_and_hard_limit_are_explicit_and_separate_from_artifact_budget():
    assert native.MAX_RAW_BYTES == 128*2**20
    assert native.MAX_HOST_RECORD_BYTES == 512*2**20
    assert native.MAX_ARTIFACT_BYTES == 2**31
    assert inspect.signature(native.verify_restored_native_mission).parameters["maximum_host_record_bytes"].default == 128*2**20
    assert native._host_record_budget(1) == 1
    assert native._host_record_budget(512*2**20) == 512*2**20


@pytest.mark.parametrize("maximum", [True, False, 0, -1, 1.0, float("nan"), float("inf"), "512", None, 512*2**20+1])
def test_invalid_host_budget_rejected_strictly_without_parsing(monkeypatch, maximum):
    monkeypatch.setattr(native, "_json_read", lambda *_a: pytest.fail("invalid budget must not parse"))
    with pytest.raises(ValueError, match="host_record_budget_invalid"):
        native._read_host_record(b"{}", maximum)


def test_actual_host_json_above_existing_default_needs_explicit_opt_in():
    # A bounded 128 MiB + 1 whitespace-padded tiny object exercises the real
    # decoder at the actual boundary, without a simulation or private record.
    raw = b" "*(native.MAX_RAW_BYTES-1)+b"{}"
    assert len(raw) == native.MAX_RAW_BYTES+1
    with pytest.raises(ValueError, match="native_json_raw_budget"):
        native._read_host_record(raw, native.MAX_RAW_BYTES)
    assert native._read_host_record(raw, len(raw)) == {}
    with pytest.raises(ValueError, match="native_json_raw_budget"):
        native._read(raw)  # Ordinary saved JSON remains at the old limit.


@pytest.mark.parametrize("raw", [b"", b"{} ", bytearray(b"{}"), "{}"])
def test_explicit_host_raw_bound_and_type_remain_strict(raw):
    with pytest.raises(ValueError, match="native_json_raw_budget"):
        native._read_host_record(raw, 2)


@pytest.mark.parametrize("raw,reason", [(b'{"x":1,"x":2}', "native_duplicate_json_key"),
    (b'{"x":NaN}', "native_nonfinite_json"), (b'{"x":Infinity}', "native_nonfinite_json"),
    (b'{"x":1e999}', "Out of range float")])
def test_opted_in_host_parser_retains_strict_json(raw, reason):
    with pytest.raises(ValueError, match=reason):
        native._read_host_record(raw, native.MAX_HOST_RECORD_BYTES)
    with pytest.raises(ValueError, match=reason):
        native._read(raw)


def test_default_verification_result_is_byte_semantically_unchanged(negative):
    record, profile, args = negative
    before = deepcopy(record)
    old_default = native.verify_restored_native_mission(record, profile, **args)
    explicit_default = native.verify_restored_native_mission(record, profile, **args,
        maximum_host_record_bytes=native.MAX_RAW_BYTES)
    opted_in = native.verify_restored_native_mission(record, profile, **args,
        maximum_host_record_bytes=native.MAX_HOST_RECORD_BYTES)
    assert old_default["passed"] is True, old_default
    assert native._wire(old_default) == native._wire(explicit_default) == native._wire(opted_in)
    assert record == before


def test_public_verifier_routes_only_host_to_explicit_limit_and_keeps_external_hash(negative, monkeypatch):
    record, profile, args = negative
    ordinary, host = [], []
    read, read_host = native._read, native._read_host_record
    def ordinary_read(raw):
        ordinary.append(len(raw))
        return read(raw)
    def host_read(raw, maximum):
        host.append((len(raw), maximum))
        return read_host(raw, maximum)
    monkeypatch.setattr(native, "_read", ordinary_read)
    monkeypatch.setattr(native, "_read_host_record", host_read)
    result = native.verify_restored_native_mission(record, profile, **args,
        maximum_host_record_bytes=native.MAX_HOST_RECORD_BYTES)
    assert result["passed"] is True, result
    assert host == [(len(args["host_record_raw"]), 512*2**20)] and ordinary
    changed = {**args, "host_record_raw": args["host_record_raw"]+b" "}
    result = native.verify_restored_native_mission(record, profile, **changed,
        maximum_host_record_bytes=native.MAX_HOST_RECORD_BYTES)
    assert result["passed"] is False and any("external_host_record_pin_changed" in row["detail"] for row in result["issues"])


def test_invalid_public_budget_is_a_retained_invalid_result_not_an_exception(negative):
    record, profile, args = negative
    for maximum in (True, 0, 512*2**20+1):
        result = native.verify_restored_native_mission(record, profile, **args, maximum_host_record_bytes=maximum)
        assert result["passed"] is False
        assert result["issues"] == [{"code": "invalid_restored_native_record", "detail": "native_host_record_budget_invalid"}]


def test_host_optin_cannot_expand_ordinary_or_gzip_artifact_bounds(monkeypatch):
    # Scale down only the old raw limit to exercise gzip guard with tiny bytes.
    monkeypatch.setattr(native, "MAX_RAW_BYTES", 32)
    raw = b" "*33+b"{}"
    assert native._read_host_record(raw, native.MAX_HOST_RECORD_BYTES) == {}
    with pytest.raises(ValueError, match="native_json_raw_budget"):
        native._read(raw)
    packed = gzip.compress(raw, mtime=0)
    token = sha256(packed).hexdigest()
    reference = {"path": "prediction.json.gz", "sha256": token, "raw_sha256": sha256(raw).hexdigest()}
    with pytest.raises(ValueError, match="prediction_restore_or_raw_hash_changed"):
        native._artifact(reference, {token: packed}, packed=True)
    monkeypatch.setattr(native, "MAX_ARTIFACT_BYTES", len(packed)-1)
    with pytest.raises(ValueError, match="artifact_missing_or_changed"):
        native._artifact(reference, {token: packed}, packed=True)
