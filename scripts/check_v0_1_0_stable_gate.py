#!/usr/bin/env python3
"""Fail-closed release gate for the MissionOS v0.1.0 stable candidate.

This check replays both backend corpora through the shared Core entry point and
validates the sanitized live-runtime evidence index.  It does not invoke an
LLM, a simulator, approval, dispatch, or execution authority.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.runtime.nav2_action_feasibility_corpus import (
    verify_nav2_corpus_through_core,
)
from src.runtime.px4_gazebo_route.action_feasibility_corpus import (
    verify_action_feasibility_corpus_through_core,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = ROOT / "tests" / "golden" / "action_feasibility"
EVIDENCE_PATH = (
    ROOT
    / "docs"
    / "agents"
    / "evidence"
    / "20260724-v0.1.0-stable-readiness.json"
)
EXPECTED_STATUSES = {"verified_feasible", "blocked", "unverified"}
EXPECTED_SURFACES = {"chat", "job-status", "operate", "watch", "map"}
AUTHORITY_STAGES = {
    "proposal",
    "human_approval",
    "dispatch_revalidation",
    "dispatch_authority",
    "runner_ack",
    "observed_effect",
    "completion",
}
BACKEND_TOKENS = re.compile(
    r"\b(?:px4|gazebo|nav2|ros2|turtlebot|unitree)\b",
    re.IGNORECASE,
)
_LOCAL_USER = "mana" + "bu"
PERSONAL_PATH = re.compile(
    rf"(?:/Users/{_LOCAL_USER}(?:/|$)|/var/" + r"folders/)",
    re.IGNORECASE,
)
REAL_KEY = re.compile(
    r"\b(?:sk-[0-9a-f]{24,}|gh[opusr]_[A-Za-z0-9]{20,})\b",
    re.IGNORECASE,
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _statuses(manifest_path: Path) -> set[str]:
    statuses: set[str] = set()
    manifest = _load(manifest_path)
    for entry in manifest.get("cases", []):
        case = _load(manifest_path.parent / str(entry["path"]))
        expected = dict(case.get("expected") or {})
        status = expected.get("feasibility_status", expected.get("status"))
        if status:
            statuses.add(str(status))
    return statuses


def _authority_chain_reasons(manifest_path: Path, backend: str) -> list[str]:
    reasons: list[str] = []
    manifest = _load(manifest_path)
    positive_seen = False
    refusal_seen = False
    for entry in manifest.get("cases", []):
        case = _load(manifest_path.parent / str(entry["path"]))
        scenario = str(case.get("scenario_class") or "")
        positive_seen |= scenario == "positive"
        refusal_seen |= scenario == "refusal"
        chain = dict(case.get("authority_chain") or {})
        if set(chain) != AUTHORITY_STAGES:
            reasons.append(f"{backend}_authority_stages_not_exact")
            continue
        refs = [
            str(dict(chain[name]).get("artifact_ref") or "")
            for name in sorted(AUTHORITY_STAGES)
        ]
        if any(not ref for ref in refs) or len(refs) != len(set(refs)):
            reasons.append(f"{backend}_authority_artifacts_not_distinct")
        revalidation = dict(chain["dispatch_revalidation"])
        authority = dict(chain["dispatch_authority"])
        if scenario == "positive":
            if revalidation.get("status") != "valid":
                reasons.append(f"{backend}_positive_revalidation_not_valid")
            if authority.get("created") is not True:
                reasons.append(f"{backend}_positive_authority_missing")
        elif scenario == "refusal":
            if revalidation.get("status") == "valid":
                reasons.append(f"{backend}_refusal_revalidation_valid")
            if authority.get("created") is not False:
                reasons.append(f"{backend}_refusal_authority_created")
    if not positive_seen:
        reasons.append(f"{backend}_positive_chain_missing")
    if not refusal_seen:
        reasons.append(f"{backend}_refusal_chain_missing")
    return reasons


def _core_neutrality_reasons() -> list[str]:
    reasons: list[str] = []
    core_root = ROOT / "packages" / "missionos-core" / "src"
    for path in core_root.rglob("*.py"):
        if BACKEND_TOKENS.search(path.read_text(encoding="utf-8")):
            reasons.append(f"core_backend_token:{path.relative_to(ROOT)}")
    return reasons


def _publication_reasons() -> list[str]:
    reasons: list[str] = []
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    tracked = [
        ROOT / item.decode("utf-8")
        for item in result.stdout.split(b"\0")
        if item
    ]
    forbidden_suffixes = {".db", ".sqlite", ".sqlite3"}
    for path in tracked:
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in forbidden_suffixes or path.name == ".env":
            reasons.append(f"tracked_private_artifact:{relative}")
            continue
        if path.suffix.lower() not in {
            ".json",
            ".md",
            ".py",
            ".toml",
            ".txt",
            ".yaml",
            ".yml",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if PERSONAL_PATH.search(text):
            reasons.append(f"personal_path:{relative}")
        if REAL_KEY.search(text):
            reasons.append(f"credential_shape:{relative}")
        if relative.name in {"pyproject.toml", "requirements.txt"} and re.search(
            r"(?:missionos-internal|git\+.*missionos-internal)",
            text,
            re.IGNORECASE,
        ):
            reasons.append(f"internal_dependency:{relative}")
    return reasons


def _evidence_reasons(path: Path) -> list[str]:
    if not path.exists():
        return ["stable_runtime_evidence_missing"]
    evidence = _load(path)
    reasons: list[str] = []
    if evidence.get("schema_version") != "missionos_stable_readiness.v1":
        reasons.append("stable_runtime_evidence_schema_invalid")
    backends = evidence.get("backends")
    backends = dict(backends) if isinstance(backends, Mapping) else {}
    if set(backends) != {"px4", "nav2"}:
        reasons.append("stable_runtime_backends_not_exact")
    for backend in ("px4", "nav2"):
        item = dict(backends.get(backend) or {})
        if not re.fullmatch(
            r"[0-9a-f]{64}",
            str(item.get("task_id_sha256") or ""),
        ):
            reasons.append(f"{backend}_task_digest_missing")
        if set(item.get("operator_surfaces") or []) != EXPECTED_SURFACES:
            reasons.append(f"{backend}_operator_surfaces_incomplete")
        if item.get("runtime_boundary") != "opt_in_live_simulator":
            reasons.append(f"{backend}_live_simulator_evidence_missing")
        source_ref = str(item.get("source_evidence_ref") or "")
        source_path = (ROOT / source_ref).resolve()
        if (
            not source_ref
            or ROOT not in source_path.parents
            or not source_path.is_file()
        ):
            reasons.append(f"{backend}_source_evidence_ref_invalid")
        if item.get("core_status") != "verified_feasible":
            reasons.append(f"{backend}_positive_core_status_missing")
        if item.get("model_judgment_source") != "deepseek":
            reasons.append(f"{backend}_deepseek_judgment_missing")
        if item.get("explicit_human_approval") is not True:
            reasons.append(f"{backend}_human_approval_missing")
        if item.get("dispatch_revalidation") not in {"valid", "validated"}:
            reasons.append(f"{backend}_dispatch_revalidation_missing")
        if item.get("dispatch_authority_created_after_revalidation") is not True:
            reasons.append(f"{backend}_dispatch_authority_order_invalid")
        if item.get("runner_ack_separate_from_observed_effect") is not True:
            reasons.append(f"{backend}_ack_effect_boundary_missing")
        if item.get("target_reached") is not True:
            reasons.append(f"{backend}_target_effect_missing")
        if item.get("route_resumed") is not True:
            reasons.append(f"{backend}_route_resume_missing")
        if item.get("terminal_sim_observation") is not True:
            reasons.append(f"{backend}_terminal_observation_missing")
        if item.get("delivery_completion_claimed") is not False:
            reasons.append(f"{backend}_delivery_overclaimed")
        if item.get("physical_execution_invoked") is not False:
            reasons.append(f"{backend}_physical_execution_overclaimed")
    serialized = json.dumps(evidence, ensure_ascii=True, sort_keys=True)
    if re.search(r"\btask_[0-9a-f]{8,}\b", serialized, re.IGNORECASE):
        reasons.append("stable_runtime_evidence_contains_raw_task_id")
    if PERSONAL_PATH.search(serialized) or REAL_KEY.search(serialized):
        reasons.append("stable_runtime_evidence_publication_boundary_violated")
    return reasons


def evaluate_stable_gate(
    *,
    evidence_path: Path = EVIDENCE_PATH,
) -> dict[str, Any]:
    """Evaluate all release requirements without creating authority."""

    reasons: list[str] = []
    backend_results: dict[str, Any] = {}
    definitions = {
        "px4": (
            CORPUS_ROOT / "px4_v1" / "manifest.json",
            verify_action_feasibility_corpus_through_core,
        ),
        "nav2": (
            CORPUS_ROOT / "nav2_v1" / "manifest.json",
            verify_nav2_corpus_through_core,
        ),
    }
    for backend, (manifest, runner) in definitions.items():
        verdict = runner(manifest)
        backend_results[backend] = {
            "status": verdict.get("status"),
            "case_count": verdict.get("case_count"),
            "statuses": sorted(_statuses(manifest)),
        }
        if verdict.get("status") != "verified":
            reasons.append(f"{backend}_core_conformance_failed")
        if _statuses(manifest) != EXPECTED_STATUSES:
            reasons.append(f"{backend}_tri_state_matrix_incomplete")
        reasons.extend(_authority_chain_reasons(manifest, backend))
    reasons.extend(_core_neutrality_reasons())
    reasons.extend(_evidence_reasons(evidence_path))
    reasons.extend(_publication_reasons())
    reasons = list(dict.fromkeys(reasons))
    return {
        "schema_version": "missionos_stable_gate_result.v1",
        "status": "ready" if not reasons else "blocked",
        "release": "v0.1.0",
        "backend_results": backend_results,
        "blocking_reasons": reasons,
        "llm_invoked": False,
        "approval_created": False,
        "dispatch_authority_created": False,
        "execution_invoked": False,
        "completion_claimed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--evidence", type=Path, default=EVIDENCE_PATH)
    args = parser.parse_args()
    result = evaluate_stable_gate(evidence_path=args.evidence)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
