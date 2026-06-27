"""Shared helpers for self-improvement flows."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from src.config.settings import get_settings


STATE_DIRNAME = ".boiled-claw-self-improvement"
STATE_FILENAME = "state.json"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "canary"


def run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


def trim_output(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated]"


def compact_text(text: str, limit: int = 180) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 1]}..."


def repo_path(repo_path: Optional[str]) -> Path:
    return Path(repo_path or ".").resolve()


def worktree_root(worktree_root: Optional[str]) -> Path:
    settings = get_settings()
    return (
        Path(worktree_root).resolve()
        if worktree_root
        else settings.self_improvement_canary_root.resolve()
    )


def split_commands(commands: str) -> list[str]:
    return [line.strip() for line in commands.splitlines() if line.strip()]


def state_path(canary: Path) -> Path:
    return canary / STATE_DIRNAME / STATE_FILENAME


def read_state(canary: Path) -> dict[str, Any]:
    current = state_path(canary)
    if not current.exists():
        return {}
    return json.loads(current.read_text(encoding="utf-8"))


def write_state(canary: Path, state: dict[str, Any]) -> None:
    current = state_path(canary)
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def persist_state(canary: Path, **updates: Any) -> dict[str, Any]:
    state = read_state(canary)
    state.update(updates)
    write_state(canary, state)
    return state


def cached_benchmark_result(canary: Path, commands: str) -> dict[str, Any] | None:
    state = read_state(canary)
    benchmark = state.get("benchmark")
    if not isinstance(benchmark, dict):
        return None
    cached_commands = benchmark.get("commands")
    command_list = split_commands(commands)
    if cached_commands != command_list:
        return None
    if not benchmark.get("all_passed") and benchmark.get("fail_fast"):
        return None
    return {
        "success": True,
        "all_passed": bool(benchmark.get("all_passed")),
        "canary_path": str(canary),
        "results": list(benchmark.get("results") or []),
        "reused": True,
    }


def parse_candidate_specs(candidate_specs_json: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(candidate_specs_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"candidate_specs_json must be valid JSON: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise ValueError("candidate_specs_json must be a non-empty JSON array")

    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Candidate spec #{index} must be an object")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"Candidate spec #{index} must include a non-empty name")
        commands_value = entry.get("commands")
        if isinstance(commands_value, list):
            commands = [str(command).strip() for command in commands_value if str(command).strip()]
        elif isinstance(commands_value, str):
            commands = split_commands(commands_value)
        else:
            commands = []
        if not commands:
            raise ValueError(f"Candidate spec '{name}' must include at least one command")

        goal = str(entry.get("goal") or "").strip() or None
        improvement_summary = str(entry.get("improvement_summary") or "").strip() or None
        normalized.append(
            {
                "name": name,
                "commands": commands,
                "goal": goal,
                "improvement_summary": improvement_summary,
            }
        )
    return normalized


def search_candidate_goal(search_goal: str, candidate_name: str, candidate_goal: Optional[str]) -> str:
    if candidate_goal:
        return candidate_goal
    return f"{search_goal} [{candidate_name}]"


def search_candidate_summary(
    base_summary: str,
    candidate_name: str,
    candidate_summary: Optional[str],
) -> str:
    if candidate_summary:
        return candidate_summary
    return f"{base_summary} Candidate: {candidate_name}."


def candidate_diff_metrics(canary: Path) -> dict[str, int]:
    numstat = run_git(canary, "diff", "--numstat")
    changed_files = 0
    changed_lines = 0
    for line in numstat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        changed_files += 1
        added = 0 if parts[0] == "-" else int(parts[0])
        deleted = 0 if parts[1] == "-" else int(parts[1])
        changed_lines += added + deleted
    return {"changed_files": changed_files, "changed_lines": changed_lines}


def candidate_benchmark_counts(package: dict[str, Any]) -> tuple[int, int]:
    benchmark = package.get("benchmark")
    if not isinstance(benchmark, dict):
        return (0, 0)
    results = benchmark.get("results")
    if not isinstance(results, list):
        return (0, 0)
    passed = sum(1 for result in results if isinstance(result, dict) and result.get("passed"))
    return (passed, len(results))


def candidate_ranking_key(candidate: dict[str, Any]) -> tuple[int, int, int, int, int, int]:
    package = candidate.get("package")
    package = package if isinstance(package, dict) else {}
    passed, total = candidate_benchmark_counts(package)
    diff_metrics = candidate.get("diff_metrics")
    diff_metrics = diff_metrics if isinstance(diff_metrics, dict) else {}
    changed_files = int(diff_metrics.get("changed_files") or 0)
    changed_lines = int(diff_metrics.get("changed_lines") or 0)
    return (
        1 if package.get("promotable") else 0,
        1 if package.get("success") else 0,
        passed,
        -total,
        -changed_files,
        -changed_lines,
    )


__all__ = [
    "STATE_DIRNAME",
    "STATE_FILENAME",
    "cached_benchmark_result",
    "candidate_benchmark_counts",
    "candidate_diff_metrics",
    "candidate_ranking_key",
    "compact_text",
    "parse_candidate_specs",
    "persist_state",
    "read_state",
    "repo_path",
    "run_git",
    "search_candidate_goal",
    "search_candidate_summary",
    "slugify",
    "split_commands",
    "trim_output",
    "worktree_root",
    "write_state",
]
