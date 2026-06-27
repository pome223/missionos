"""
Cron job scheduler with SQLite persistence.

Platform features:
  - croniter-based expression parsing
  - Delivery target: "isolated" (default) or "session:<id>" (deliver to main session)
  - Retry policy: max_retries, retry_delay_seconds
  - System event triggers: fire jobs on connect/disconnect/startup events
  - 30-second check interval with SubagentManager.spawn() delegation
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

try:
    from croniter import croniter as _croniter
    _CRONITER_OK = True
except ImportError:
    _CRONITER_OK = False


_DB_PATH = Path("data/cron.db")
_CHECK_INTERVAL = 30  # seconds

DeliveryTarget = Literal["isolated", "main"]


@dataclass
class CronJob:
    id: str
    name: str
    cron_expr: str
    task: str
    agent_id: str
    enabled: bool
    created_at: float
    last_run: Optional[float]
    next_run: Optional[float]
    run_count: int
    last_result: Optional[str]
    last_error: Optional[str]
    # --- platform extensions ---
    delivery_target: str = "isolated"        # "isolated" or "session:<id>"
    max_retries: int = 0
    retry_delay: int = 30                    # seconds between retries
    retry_count: int = 0
    system_event: Optional[str] = None       # "connect" | "disconnect" | "startup" | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cron_expr": self.cron_expr,
            "task": self.task,
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "delivery_target": self.delivery_target,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
            "retry_count": self.retry_count,
            "system_event": self.system_event,
        }


class CronScheduler:
    """SQLite-backed cron job scheduler with platform features."""

    def __init__(self, db_path: Path = _DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._notifier: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
        self._spawn_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None
        self._init_db()

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    cron_expr   TEXT NOT NULL,
                    task        TEXT NOT NULL,
                    agent_id    TEXT NOT NULL DEFAULT 'web_researcher',
                    enabled     INTEGER NOT NULL DEFAULT 1,
                    created_at  REAL NOT NULL,
                    last_run    REAL,
                    next_run    REAL,
                    run_count   INTEGER NOT NULL DEFAULT 0,
                    last_result TEXT,
                    last_error  TEXT
                )
            """)
            self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add platform columns if they don't exist yet."""
        with self._lock:
            existing = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(cron_jobs)").fetchall()
            }
            migrations = [
                ("delivery_target", "TEXT NOT NULL DEFAULT 'isolated'"),
                ("max_retries", "INTEGER NOT NULL DEFAULT 0"),
                ("retry_delay", "INTEGER NOT NULL DEFAULT 30"),
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("system_event", "TEXT"),
            ]
            for col, typedef in migrations:
                if col not in existing:
                    self._conn.execute(f"ALTER TABLE cron_jobs ADD COLUMN {col} {typedef}")
            self._conn.commit()

    def set_notifier(self, fn: Optional[Callable[[Dict[str, Any]], Awaitable[None]]]) -> None:
        self._notifier = fn

    def set_spawn_fn(self, fn: Callable[..., Awaitable[Dict[str, Any]]]) -> None:
        """SubagentManager.spawn を渡す。"""
        self._spawn_fn = fn

    # ------------------------------------------------------------------
    # job CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cron(expr: str) -> None:
        if not _CRONITER_OK:
            raise ValueError("croniter is not installed. Run: pip install croniter")
        try:
            _croniter(expr)
        except Exception as e:
            raise ValueError(f"Invalid cron expression '{expr}': {e}") from e

    @staticmethod
    def _next_ts(expr: str, base: Optional[float] = None) -> Optional[float]:
        if not _CRONITER_OK:
            return None
        try:
            return _croniter(expr, base or time.time()).get_next(float)
        except Exception:
            return None

    def add_job(
        self,
        name: str,
        cron_expr: str,
        task: str,
        agent_id: str = "web_researcher",
        delivery_target: str = "isolated",
        max_retries: int = 0,
        retry_delay: int = 30,
        system_event: Optional[str] = None,
    ) -> CronJob:
        if not name.strip():
            raise ValueError("name is required")
        if not task.strip():
            raise ValueError("task is required")

        # system_event jobs can use empty cron_expr
        if system_event:
            if system_event not in ("connect", "disconnect", "startup"):
                raise ValueError(
                    f"Invalid system_event: '{system_event}'. "
                    "Must be one of: connect, disconnect, startup"
                )
            if not cron_expr.strip():
                cron_expr = "0 0 1 1 *"  # placeholder, never fires by cron
        else:
            self._validate_cron(cron_expr)

        # Validate delivery_target
        if delivery_target not in ("isolated", "main") and not delivery_target.startswith("session:"):
            raise ValueError(
                f"Invalid delivery_target: '{delivery_target}'. "
                "Must be 'isolated', 'main', or 'session:<id>'"
            )

        job_id = str(uuid.uuid4())
        now = time.time()
        next_run = self._next_ts(cron_expr, now) if not system_event else None

        with self._lock:
            self._conn.execute(
                """INSERT INTO cron_jobs
                   (id, name, cron_expr, task, agent_id, created_at, next_run,
                    delivery_target, max_retries, retry_delay, system_event)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, name.strip(), cron_expr, task.strip(), agent_id, now,
                 next_run, delivery_target, max_retries, retry_delay, system_event),
            )
            self._conn.commit()
        return self._fetch(job_id)  # type: ignore[return-value]

    def list_jobs(self) -> List[CronJob]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cron_jobs ORDER BY created_at DESC"
            ).fetchall()
        return [self._row(r) for r in rows]

    def get_job(self, job_id: str) -> Optional[CronJob]:
        return self._fetch(job_id)

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def toggle_job(self, job_id: str, enabled: bool) -> Optional[CronJob]:
        with self._lock:
            self._conn.execute(
                "UPDATE cron_jobs SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, job_id),
            )
            self._conn.commit()
        return self._fetch(job_id)

    def _fetch(self, job_id: str) -> Optional[CronJob]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cron_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(r: tuple) -> CronJob:
        return CronJob(
            id=r[0], name=r[1], cron_expr=r[2], task=r[3],
            agent_id=r[4], enabled=bool(r[5]), created_at=r[6],
            last_run=r[7], next_run=r[8], run_count=r[9],
            last_result=r[10], last_error=r[11],
            # platform columns (may be missing on old DBs, defaults used)
            delivery_target=r[12] if len(r) > 12 else "isolated",
            max_retries=r[13] if len(r) > 13 else 0,
            retry_delay=r[14] if len(r) > 14 else 30,
            retry_count=r[15] if len(r) > 15 else 0,
            system_event=r[16] if len(r) > 16 else None,
        )

    # ------------------------------------------------------------------
    # scheduler loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._loop(), name="cron-scheduler")

    async def shutdown(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(_CHECK_INTERVAL)

    async def _tick(self) -> None:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM cron_jobs
                   WHERE enabled = 1
                     AND next_run IS NOT NULL
                     AND next_run <= ?
                     AND (system_event IS NULL OR system_event = '')""",
                (now,),
            ).fetchall()
        for row in rows:
            asyncio.create_task(self._run_job(self._row(row)), name=f"cron:{row[0]}")

    async def fire_system_event(self, event_name: str, context: Optional[Dict[str, Any]] = None) -> int:
        """Fire all jobs triggered by a system event.

        Returns the number of jobs fired.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cron_jobs WHERE enabled = 1 AND system_event = ?",
                (event_name,),
            ).fetchall()
        count = 0
        for row in rows:
            job = self._row(row)
            ctx = context or {}
            task_with_ctx = f"[system_event:{event_name}] {job.task}"
            if ctx:
                task_with_ctx += f"\n[context: {json.dumps(ctx, ensure_ascii=False)[:200]}]"
            runtime_job = replace(job, task=task_with_ctx)
            asyncio.create_task(
                self._run_job(runtime_job, trigger_context=ctx),
                name=f"cron:sys:{row[0]}",
            )
            count += 1
        return count

    async def _run_job(
        self,
        job: CronJob,
        retry_attempt: int = 0,
        trigger_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        # 次回実行時刻を先に更新してから実行（重複起動防止）
        if not job.system_event:
            next_run = self._next_ts(job.cron_expr)
        else:
            next_run = None

        with self._lock:
            self._conn.execute(
                """UPDATE cron_jobs
                   SET last_run = ?, next_run = ?, run_count = run_count + 1
                   WHERE id = ?""",
                (time.time(), next_run, job.id),
            )
            self._conn.commit()

        retry_label = f" (retry {retry_attempt}/{job.max_retries})" if retry_attempt > 0 else ""
        requester_session_id = f"cron_{job.id}"
        mode = "run"
        if job.delivery_target.startswith("session:"):
            requester_session_id = job.delivery_target.removeprefix("session:")
        elif job.delivery_target == "main" and trigger_context and trigger_context.get("session_id"):
            requester_session_id = str(trigger_context["session_id"])

        await self._notify(
            job.id,
            "running",
            f"[cron:{job.name}] started{retry_label}",
            requester_session_id=requester_session_id,
            delivery_target=job.delivery_target,
        )

        if self._spawn_fn is None:
            return

        try:
            result = await self._spawn_fn(
                task=job.task,
                agent_name=job.agent_id,
                requester_session_id=requester_session_id,
                user_id="cron",
                app_name="boiled-claw",
                mode=mode,
            )
            snippet = json.dumps(result, ensure_ascii=False)[:200]
            with self._lock:
                self._conn.execute(
                    "UPDATE cron_jobs SET last_result = ?, last_error = NULL, retry_count = 0 WHERE id = ?",
                    (snippet, job.id),
                )
                self._conn.commit()
            run_id = result.get("run_id", "?")
            await self._notify(
                job.id,
                "accepted",
                f"[cron:{job.name}] spawned run_id={run_id}",
                requester_session_id=requester_session_id,
                delivery_target=job.delivery_target,
            )
        except Exception as exc:
            with self._lock:
                self._conn.execute(
                    "UPDATE cron_jobs SET last_error = ?, retry_count = ? WHERE id = ?",
                    (str(exc), retry_attempt + 1, job.id),
                )
                self._conn.commit()
            await self._notify(
                job.id,
                "failed",
                f"[cron:{job.name}] error: {exc}",
                requester_session_id=requester_session_id,
                delivery_target=job.delivery_target,
            )

            # Retry logic
            if retry_attempt < job.max_retries:
                await asyncio.sleep(job.retry_delay)
                await self._run_job(
                    job,
                    retry_attempt + 1,
                    trigger_context=trigger_context,
                )

    async def _notify(
        self,
        job_id: str,
        status: str,
        message: str,
        *,
        requester_session_id: str = "",
        delivery_target: str = "isolated",
    ) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier(
                {
                    "job_id": job_id,
                    "status": status,
                    "message": message,
                    "requester_session_id": requester_session_id,
                    "delivery_target": delivery_target,
                }
            )
        except Exception:
            pass


# Global singleton
_scheduler: Optional[CronScheduler] = None


def get_scheduler() -> CronScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CronScheduler()
    return _scheduler
