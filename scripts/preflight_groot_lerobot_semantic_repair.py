#!/usr/bin/env python3
"""GPU-free admission gate for the official LeRobot GR00T Repair baseline."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


OPT_IN = "RUN_MISSIONOS_GROOT_LEROBOT_SEMANTIC_PREFLIGHT"
CHECKPOINT_REPOSITORY = "nvidia/gr00t17-lerobot-libero_10-640"
CHECKPOINT_REVISION = "5ee08ab09fac5c5ef2388a14c882ea825ac861db"
COSMOS_REPOSITORY = "nvidia/Cosmos-Reason2-2B"
COSMOS_REVISION = "9ce19a195e423419c349abfc86fd07178b230561"
LEROBOT_REVISION = "6adf51511b7625090eade8d82d9f61a1846ebe56"

UrlOpen = Callable[..., Any]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


DEFAULT_OPENER = build_opener(_RejectRedirects()).open


def _probe_result(
    *,
    repository: str,
    revision: str,
    status: int,
    headers: Any,
    token_presented: bool,
) -> dict[str, Any]:
    observed_revision = headers.get("X-Repo-Commit")
    reachable = status in {200, 206, 302, 307}
    revision_matches = observed_revision == revision
    if not reachable:
        failure_code = f"http_{status}"
    elif not revision_matches:
        failure_code = "revision_mismatch"
    else:
        failure_code = None
    return {
        "accessible": bool(reachable and revision_matches),
        "credential_presented_to_huggingface": token_presented,
        "failure_code": failure_code,
        "observed_revision": observed_revision,
        "redirect_followed": False,
        "resolution": ("redirect_not_followed" if status in {302, 307} else "direct_success"),
        "repository": repository,
        "revision": revision,
    }


def _probe(
    *,
    repository: str,
    revision: str,
    filename: str,
    token: str | None,
    opener: UrlOpen,
) -> dict[str, Any]:
    url = f"https://huggingface.co/{repository}/resolve/{revision}/{filename}"
    headers = {"Range": "bytes=0-0", "User-Agent": "missionos-groot-preflight/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with opener(request, timeout=20) as response:
            status = int(response.status)
            return _probe_result(
                repository=repository,
                revision=revision,
                status=status,
                headers=response.headers,
                token_presented=bool(token),
            )
    except HTTPError as error:
        error_headers = error.headers or {}
        if error.code in {302, 307}:
            return _probe_result(
                repository=repository,
                revision=revision,
                status=error.code,
                headers=error_headers,
                token_presented=bool(token),
            )
        return {
            "accessible": False,
            "credential_presented_to_huggingface": bool(token),
            "failure_code": f"http_{error.code}",
            "observed_revision": error_headers.get("X-Repo-Commit"),
            "redirect_followed": False,
            "resolution": "request_failed",
            "repository": repository,
            "revision": revision,
        }
    except (OSError, URLError) as error:
        return {
            "accessible": False,
            "credential_presented_to_huggingface": bool(token),
            "failure_code": f"transport_error:{type(error).__name__}",
            "observed_revision": None,
            "redirect_followed": False,
            "resolution": "request_failed",
            "repository": repository,
            "revision": revision,
        }


def run_preflight(*, opener: UrlOpen = DEFAULT_OPENER) -> dict[str, Any]:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    checkpoint = _probe(
        repository=CHECKPOINT_REPOSITORY,
        revision=CHECKPOINT_REVISION,
        filename="config.json",
        token=None,
        opener=opener,
    )
    checkpoint["public_access_required"] = True
    if token:
        cosmos = _probe(
            repository=COSMOS_REPOSITORY,
            revision=COSMOS_REVISION,
            filename="config.json",
            token=token,
            opener=opener,
        )
    else:
        cosmos = {
            "accessible": False,
            "credential_presented_to_huggingface": False,
            "failure_code": "huggingface_token_missing",
            "observed_revision": None,
            "redirect_followed": False,
            "resolution": "not_attempted",
            "repository": COSMOS_REPOSITORY,
            "revision": COSMOS_REVISION,
        }
    gate_passed = bool(checkpoint["accessible"] and cosmos["accessible"])
    return {
        "schema_version": "missionos.groot_lerobot_semantic_preflight.v1",
        "gate_passed": gate_passed,
        "checkpoint": checkpoint,
        "cosmos_processor": cosmos,
        "lerobot_revision_declared": LEROBOT_REVISION,
        "next_stage_gate": {
            "l4_baseline_preconditions_met": gate_passed,
        },
        "authority": {
            "human_approval_created": False,
            "l4_provisioning_authorized": False,
            "semantic_repair_authorized": False,
            "dispatch_authority_created": False,
            "physical_execution_authorized": False,
        },
    }


def main() -> int:
    if os.environ.get(OPT_IN) != "1":
        print(json.dumps({"status": "not_run", "required_opt_in": OPT_IN}, sort_keys=True))
        return 3
    report = run_preflight()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["gate_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
