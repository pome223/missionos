#!/usr/bin/env python3
"""Download and verify the gated Cosmos Policy tokenizer before GPU use.

The Hugging Face token is read from Google Cloud Secret Manager into process
memory. It is never accepted as a command-line argument, printed, stored in the
Hugging Face credential cache, or written to the evidence record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable
from uuid import uuid4


OPT_IN_ENV = "RUN_MISSIONOS_COSMOS_POLICY_TOKENIZER_DOWNLOAD"
SECRET_PROJECT_ENV = "MISSIONOS_COSMOS_POLICY_HF_SECRET_PROJECT"
SECRET_NAME_ENV = "MISSIONOS_COSMOS_POLICY_HF_SECRET_NAME"
TOKENIZER_REPOSITORY = "nvidia/Cosmos-Predict2-2B-Video2World"
TOKENIZER_REVISION = "f50c09f5d8ab133a90cac3f4886a6471e9ba3f18"
TOKENIZER_RELATIVE_PATH = "tokenizer/tokenizer.pth"
TOKENIZER_SIZE_BYTES = 507_609_880
TOKENIZER_SHA256 = "38071ab59bd94681c686fa51d75a1968f64e470262043be31f7a094e442fd981"
EVIDENCE_SCHEMA_VERSION = "missionos.cosmos_policy_tokenizer_preflight.v1"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_tokenizer(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError("cosmos_policy_gated_tokenizer_file_missing")
    observed_size = path.stat().st_size
    observed_sha256 = _sha256_path(path)
    if observed_size != TOKENIZER_SIZE_BYTES or observed_sha256 != TOKENIZER_SHA256:
        raise RuntimeError("cosmos_policy_gated_tokenizer_file_mismatch")
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "repository": TOKENIZER_REPOSITORY,
        "revision": TOKENIZER_REVISION,
        "relative_path": TOKENIZER_RELATIVE_PATH,
        "size_bytes": observed_size,
        "sha256": observed_sha256,
        "source_access_requires_hugging_face_authentication": True,
        "credential_persisted_in_evidence": False,
        "additional_training_performed": False,
        "gpu_resource_required": False,
    }


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _access_secret_payload(*, project: str, secret_name: str) -> str:
    if not project or not secret_name:
        raise RuntimeError("cosmos_policy_secret_manager_locator_required")
    completed = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={secret_name}",
            f"--project={project}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("cosmos_policy_secret_manager_access_failed")
    payload = completed.stdout.strip()
    if not payload:
        raise RuntimeError("cosmos_policy_secret_manager_payload_empty")
    return payload


def prepare_tokenizer(
    *,
    output_dir: Path,
    token_is_available: Callable[[], bool],
    download_file: Callable[[Path], Path],
) -> dict[str, object]:
    if os.environ.get(OPT_IN_ENV) != "1":
        raise RuntimeError("cosmos_policy_tokenizer_download_opt_in_required")

    tokenizer_path = output_dir / TOKENIZER_RELATIVE_PATH
    if tokenizer_path.exists():
        evidence = _verify_tokenizer(tokenizer_path)
        evidence["download_performed"] = False
    else:
        if not token_is_available():
            raise RuntimeError("cosmos_policy_hugging_face_credential_required")
        downloaded_path = download_file(output_dir)
        if downloaded_path.resolve() != tokenizer_path.resolve():
            raise RuntimeError("cosmos_policy_tokenizer_download_path_mismatch")
        evidence = _verify_tokenizer(tokenizer_path)
        evidence["download_performed"] = True

    evidence_path = output_dir / "tokenizer-evidence.json"
    _write_json(evidence_path, evidence)
    return {
        "tokenizer_path": str(tokenizer_path.resolve()),
        "evidence_path": str(evidence_path.resolve()),
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise RuntimeError(
            "huggingface_hub_required: install it locally before authentication"
        ) from error

    secret_project = os.environ.get(SECRET_PROJECT_ENV, "")
    secret_name = os.environ.get(SECRET_NAME_ENV, "")
    token_holder: dict[str, str] = {}

    def secret_token() -> str:
        if "value" not in token_holder:
            token_holder["value"] = _access_secret_payload(
                project=secret_project,
                secret_name=secret_name,
            )
        return token_holder["value"]

    def download_file(output_dir: Path) -> Path:
        return Path(
            hf_hub_download(
                repo_id=TOKENIZER_REPOSITORY,
                filename=TOKENIZER_RELATIVE_PATH,
                revision=TOKENIZER_REVISION,
                local_dir=output_dir,
                token=secret_token(),
            )
        )

    try:
        try:
            result = prepare_tokenizer(
                output_dir=args.output_dir.resolve(),
                token_is_available=lambda: bool(secret_token()),
                download_file=download_file,
            )
        except RuntimeError as error:
            print(str(error), file=sys.stderr)
            return 2
        except Exception as error:
            print(
                f"cosmos_policy_tokenizer_download_failed:{type(error).__name__}",
                file=sys.stderr,
            )
            return 3
    finally:
        token_holder.clear()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
