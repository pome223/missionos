"""Persistent validation-run storage for simulation-first physical AI flows."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from src.config.settings import get_settings


class PhysicalAIValidationStore:
    """Persist validation runs so dispatch decisions survive process restarts."""

    def __init__(self, db_path: str = "data/physical_ai_validation.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS physical_ai_validation_runs (
                    run_id TEXT PRIMARY KEY,
                    adapter TEXT NOT NULL,
                    status TEXT NOT NULL,
                    validated INTEGER NOT NULL,
                    workflow TEXT,
                    scenario TEXT,
                    robot TEXT,
                    task TEXT,
                    response_json TEXT NOT NULL,
                    mission_contract_json TEXT NOT NULL DEFAULT '{}',
                    telemetry_health_json TEXT NOT NULL DEFAULT '{}',
                    verifier_result_json TEXT NOT NULL DEFAULT '{}',
                    replay_plan_json TEXT NOT NULL DEFAULT '{}',
                    action_envelope_json TEXT NOT NULL DEFAULT '{}',
                    governor_decision_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            cursor.execute("PRAGMA table_info(physical_ai_validation_runs)")
            columns = {row[1] for row in cursor.fetchall()}
            for column in (
                ("mission_contract_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("telemetry_health_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("verifier_result_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("replay_plan_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("action_envelope_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("governor_decision_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                if column[0] in columns:
                    continue
                cursor.execute(
                    f"""
                    ALTER TABLE physical_ai_validation_runs
                    ADD COLUMN {column[0]} {column[1]}
                    """
                )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_physical_ai_validation_runs_updated_at
                ON physical_ai_validation_runs(updated_at DESC)
                """
            )
            conn.commit()

    def upsert(self, run: dict[str, Any]) -> None:
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO physical_ai_validation_runs (
                    run_id,
                    adapter,
                    status,
                    validated,
                    workflow,
                    scenario,
                    robot,
                    task,
                    response_json,
                    mission_contract_json,
                    telemetry_health_json,
                    verifier_result_json,
                    replay_plan_json,
                    action_envelope_json,
                    governor_decision_json,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    adapter = excluded.adapter,
                    status = excluded.status,
                    validated = excluded.validated,
                    workflow = excluded.workflow,
                    scenario = excluded.scenario,
                    robot = excluded.robot,
                    task = excluded.task,
                    response_json = excluded.response_json,
                    mission_contract_json = excluded.mission_contract_json,
                    telemetry_health_json = excluded.telemetry_health_json,
                    verifier_result_json = excluded.verifier_result_json,
                    replay_plan_json = excluded.replay_plan_json,
                    action_envelope_json = excluded.action_envelope_json,
                    governor_decision_json = excluded.governor_decision_json,
                    updated_at = excluded.updated_at
                """,
                (
                    run["run_id"],
                    run["adapter"],
                    run["status"],
                    1 if run.get("validated") else 0,
                    run.get("workflow"),
                    run.get("scenario"),
                    run.get("robot"),
                    run.get("task"),
                    json.dumps(run.get("response") or {}, ensure_ascii=True),
                    json.dumps(run.get("mission_contract") or {}, ensure_ascii=True),
                    json.dumps(run.get("telemetry_health") or {}, ensure_ascii=True),
                    json.dumps(run.get("verifier_result") or {}, ensure_ascii=True),
                    json.dumps(run.get("replay_plan") or {}, ensure_ascii=True),
                    json.dumps(run.get("action_envelope") or {}, ensure_ascii=True),
                    json.dumps(run.get("governor_decision") or {}, ensure_ascii=True),
                    run.get("created_at", now),
                    now,
                ),
            )
            conn.commit()

    def get(self, run_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT run_id, adapter, status, validated, workflow, scenario, robot, task,
                       response_json, mission_contract_json, telemetry_health_json,
                       verifier_result_json, replay_plan_json, action_envelope_json,
                       governor_decision_json, created_at, updated_at
                FROM physical_ai_validation_runs
                WHERE run_id = ?
                """,
                (run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {
            "run_id": row[0],
            "adapter": row[1],
            "status": row[2],
            "validated": bool(row[3]),
            "workflow": row[4],
            "scenario": row[5],
            "robot": row[6],
            "task": row[7],
            "response": json.loads(row[8]) if row[8] else {},
            "mission_contract": json.loads(row[9]) if row[9] else {},
            "telemetry_health": json.loads(row[10]) if row[10] else {},
            "verifier_result": json.loads(row[11]) if row[11] else {},
            "replay_plan": json.loads(row[12]) if row[12] else {},
            "action_envelope": json.loads(row[13]) if row[13] else {},
            "governor_decision": json.loads(row[14]) if row[14] else {},
            "created_at": row[15],
            "updated_at": row[16],
        }

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM physical_ai_validation_runs")
            conn.commit()


_validation_store: PhysicalAIValidationStore | None = None


def get_physical_ai_validation_store() -> PhysicalAIValidationStore:
    global _validation_store
    if _validation_store is None:
        settings = get_settings()
        db_path = getattr(
            settings,
            "physical_ai_validation_db_path",
            Path("data/physical_ai_validation.db"),
        )
        if not db_path:
            db_path = Path("data/physical_ai_validation.db")
        _validation_store = PhysicalAIValidationStore(db_path=str(db_path))
    return _validation_store


def reset_physical_ai_validation_store() -> None:
    global _validation_store
    _validation_store = None
