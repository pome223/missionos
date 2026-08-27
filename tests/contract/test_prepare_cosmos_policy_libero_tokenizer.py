from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest

from scripts import prepare_cosmos_policy_libero_tokenizer as preflight


def _bind_fixture_digest(monkeypatch, path) -> None:
    monkeypatch.setattr(preflight, "TOKENIZER_SIZE_BYTES", path.stat().st_size)
    monkeypatch.setattr(preflight, "TOKENIZER_SHA256", preflight._sha256_path(path))


def test_preflight_requires_explicit_download_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(preflight.OPT_IN_ENV, raising=False)

    with pytest.raises(
        RuntimeError,
        match="cosmos_policy_tokenizer_download_opt_in_required",
    ):
        preflight.prepare_tokenizer(
            output_dir=tmp_path,
            token_is_available=lambda: True,
            download_file=lambda _output_dir: tmp_path / "unused",
        )


def test_missing_credential_fails_before_download(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(preflight.OPT_IN_ENV, "1")
    download_called = False

    def download_file(_output_dir):
        nonlocal download_called
        download_called = True
        return tmp_path / "unused"

    with pytest.raises(
        RuntimeError,
        match="cosmos_policy_hugging_face_credential_required",
    ):
        preflight.prepare_tokenizer(
            output_dir=tmp_path,
            token_is_available=lambda: False,
            download_file=download_file,
    )

    assert download_called is False
    assert not (tmp_path / preflight.TOKENIZER_RELATIVE_PATH).exists()
    assert not (tmp_path / "tokenizer-evidence.json").exists()


def test_secret_manager_payload_is_captured_without_logging_token(
    monkeypatch,
) -> None:
    observed_command = []

    def run(command, **_kwargs):
        observed_command.extend(command)
        return CompletedProcess(command, 0, stdout="private-token\n", stderr="")

    monkeypatch.setattr(preflight.subprocess, "run", run)

    payload = preflight._access_secret_payload(
        project="example-project",
        secret_name="example-secret",
    )

    assert payload == "private-token"
    assert "private-token" not in observed_command
    assert "--secret=example-secret" in observed_command


def test_verified_existing_tokenizer_does_not_download(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(preflight.OPT_IN_ENV, "1")
    tokenizer_path = tmp_path / preflight.TOKENIZER_RELATIVE_PATH
    tokenizer_path.parent.mkdir(parents=True)
    tokenizer_path.write_bytes(b"verified-tokenizer-fixture")
    _bind_fixture_digest(monkeypatch, tokenizer_path)

    result = preflight.prepare_tokenizer(
        output_dir=tmp_path,
        token_is_available=lambda: False,
        download_file=lambda _output_dir: pytest.fail("download must not run"),
    )

    assert result["evidence"]["download_performed"] is False
    assert result["evidence"]["credential_persisted_in_evidence"] is False
    evidence = json.loads((tmp_path / "tokenizer-evidence.json").read_text())
    assert evidence["sha256"] == preflight.TOKENIZER_SHA256


def test_download_is_verified_and_evidence_contains_no_token(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(preflight.OPT_IN_ENV, "1")

    def download_file(output_dir):
        tokenizer_path = output_dir / preflight.TOKENIZER_RELATIVE_PATH
        tokenizer_path.parent.mkdir(parents=True)
        tokenizer_path.write_bytes(b"downloaded-tokenizer-fixture")
        _bind_fixture_digest(monkeypatch, tokenizer_path)
        return tokenizer_path

    result = preflight.prepare_tokenizer(
        output_dir=tmp_path,
        token_is_available=lambda: True,
        download_file=download_file,
    )

    serialized = json.dumps(result, sort_keys=True)
    assert result["evidence"]["download_performed"] is True
    assert "access_token" not in serialized
    assert "hf_token" not in serialized


def test_wrong_existing_tokenizer_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(preflight.OPT_IN_ENV, "1")
    tokenizer_path = tmp_path / preflight.TOKENIZER_RELATIVE_PATH
    tokenizer_path.parent.mkdir(parents=True)
    tokenizer_path.write_bytes(b"wrong")

    with pytest.raises(
        RuntimeError,
        match="cosmos_policy_gated_tokenizer_file_mismatch",
    ):
        preflight.prepare_tokenizer(
            output_dir=tmp_path,
            token_is_available=lambda: True,
            download_file=lambda _output_dir: pytest.fail("must not overwrite"),
        )
