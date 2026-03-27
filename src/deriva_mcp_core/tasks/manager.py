from __future__ import annotations

"""Background task manager for deriva-mcp-core.

Provides a server-level in-memory task registry so long-running operations
(catalog clone, RAG bulk ingest, ML training runs) can be submitted as async
tasks and polled via MCP tools.

Task lifecycle states: pending -> running -> completed | failed | cancelled

Principal scoping: each task is bound to the iss/sub principal that submitted it.
Tasks are only visible to their own principal -- get/list/cancel calls for a
different principal return None / empty list / False respectively.

Credential lifetime: the original MCP bearer token is captured at submission time
so that get_credential() can re-exchange for a fresh derived token if the task
outlives one derived token window (Credenza caps DERIVED sessions at 30 minutes).
As long as the bearer token itself remains valid (typically 24 hours), the task
can run to completion without user interaction.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..telemetry import audit_event

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class TaskRecord:
    """Metadata record for a single background task."""

    task_id: str
    principal: str
    name: str
    description: str
    state: str  # pending | running | completed | failed | cancelled
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: Any = None
    error: str | None = None
    progress: str | None = None


class TaskManager:
    """Server-level singleton registry for background tasks.

    Initialized once at startup in server.py and injected into PluginContext.
    All methods are safe to call from async tool handlers.
    """

    def __init__(self, token_cache: Any | None = None) -> None:
        """Args:
            token_cache: DerivedTokenCache instance for credential re-exchange.
                None in stdio mode -- get_credential() is not supported then.
        """
        self._token_cache = token_cache
        # TaskRecord objects keyed by task_id
        self._records: dict[str, TaskRecord] = {}
        # asyncio.Task objects keyed by task_id (only while running)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # (principal, bearer_token) keyed by task_id; stored separately so
        # bearer tokens are never accidentally serialized with TaskRecord data
        self._credentials: dict[str, tuple[str, str]] = {}

    def submit(
        self,
        coroutine: Any,
        name: str,
        principal: str,
        bearer_token: str | None,
        description: str = "",
    ) -> str:
        """Submit a coroutine as a background task.

        Must be called from within an asyncio event loop (i.e. from a tool handler).

        Args:
            coroutine: Awaitable to run as the task body.
            name: Short human-readable task name shown in list_tasks output.
            principal: Fully-qualified identity (iss/sub) of the submitting user.
                Pass get_request_user_id() -- "stdio" in stdio mode.
            bearer_token: Original MCP bearer token for credential re-exchange.
                None in stdio mode (no re-exchange needed).
            description: Optional longer description of the task.

        Returns:
            task_id (UUID4 string). Pass this to get/list/cancel.
        """
        task_id = str(uuid4())
        record = TaskRecord(
            task_id=task_id,
            principal=principal,
            name=name,
            description=description,
            state="pending",
            created_at=_now_iso(),
        )
        self._records[task_id] = record
        if bearer_token is not None:
            self._credentials[task_id] = (principal, bearer_token)

        async_task = asyncio.create_task(self._run_task(record, coroutine))
        self._tasks[task_id] = async_task
        async_task.add_done_callback(lambda _: self._tasks.pop(task_id, None))

        audit_event(
            "task_submitted",
            task_id=task_id,
            name=name,
            principal=principal,
        )
        logger.info("Task submitted: task_id=%s name=%r principal=%s", task_id, name, principal)
        return task_id

    async def _run_task(self, record: TaskRecord, coroutine: Any) -> None:
        record.state = "running"
        record.started_at = _now_iso()
        try:
            record.result = await coroutine
            record.state = "completed"
        except asyncio.CancelledError:
            record.state = "cancelled"
            raise
        except Exception as exc:
            record.error = str(exc)
            record.state = "failed"
            logger.warning(
                "Task failed: task_id=%s name=%r error=%s",
                record.task_id,
                record.name,
                exc,
                exc_info=True,
            )
        finally:
            record.completed_at = _now_iso()
            self._credentials.pop(record.task_id, None)
            audit_event(
                f"task_{record.state}",
                task_id=record.task_id,
                name=record.name,
                principal=record.principal,
            )

    def get(self, task_id: str, principal: str) -> TaskRecord | None:
        """Return the task record for task_id if it belongs to principal.

        Returns None for unknown task IDs or tasks belonging to a different principal,
        giving no information about other users' tasks.
        """
        record = self._records.get(task_id)
        if record is None or record.principal != principal:
            return None
        return record

    def list(self, principal: str, status: str | None = None) -> list[TaskRecord]:
        """Return all tasks for principal, sorted by created_at descending.

        Args:
            principal: Only tasks submitted by this principal are returned.
            status: Optional state filter -- one of "pending", "running",
                "completed", "failed", "cancelled".
        """
        records = [r for r in self._records.values() if r.principal == principal]
        if status is not None:
            records = [r for r in records if r.state == status]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def cancel(self, task_id: str, principal: str) -> tuple[bool, str]:
        """Request cancellation of a running task.

        Returns (True, "") if cancellation was requested, or (False, reason)
        if the task is already finished, belongs to another principal, or is
        unknown.
        """
        record = self._records.get(task_id)
        if record is None or record.principal != principal:
            return False, "not found"
        if record.state not in ("pending", "running"):
            return False, f"task is already {record.state}"
        async_task = self._tasks.get(task_id)
        if async_task is not None:
            async_task.cancel()
        return True, ""

    async def get_credential(self, task_id: str) -> dict:
        """Return a fresh derived credential for a running task.

        Calls DerivedTokenCache.get() so a near-expiry derived token is
        re-exchanged automatically. The bearer token captured at submission
        time is used as the subject token for the exchange.

        Call this before each batch of DERIVA operations inside a task body
        rather than holding a credential snapshot from submission time.

        Args:
            task_id: Task ID returned by submit().

        Returns:
            Credential dict suitable for passing to DerivaBinding.

        Raises:
            RuntimeError: If task_id is unknown, credentials were not captured
                (stdio mode), or token_cache is not configured.
            Exception: If the token exchange fails (e.g., bearer token expired).
        """
        creds = self._credentials.get(task_id)
        if creds is None:
            raise RuntimeError(
                f"No credentials for task {task_id!r}. "
                "Credential capture is only available in HTTP transport mode."
            )
        if self._token_cache is None:
            raise RuntimeError("Token cache not configured (stdio mode).")
        principal, bearer_token = creds
        derived_token = await self._token_cache.get(principal, bearer_token)
        return {"bearer-token": derived_token}

    def update_progress(self, task_id: str, progress: str) -> None:
        """Update the free-form progress string for a running task.

        Call this from within the task coroutine to give the LLM status
        information when it polls get_task_status().

        Args:
            task_id: Task ID returned by submit().
            progress: Free-form progress description (e.g. "25% complete").
        """
        record = self._records.get(task_id)
        if record is not None:
            record.progress = progress


# Module-level singleton. Set by server.py at startup.
_task_manager: TaskManager | None = None


def _set_task_manager(manager: TaskManager) -> None:
    """Set the module-level TaskManager singleton. Called once from server.py."""
    global _task_manager
    _task_manager = manager


def get_task_manager() -> TaskManager:
    """Return the server-level TaskManager singleton.

    For use by plugins that call submit() outside a PluginContext method
    (e.g., from lifecycle hooks). Raises RuntimeError if called before
    server startup.
    """
    if _task_manager is None:
        raise RuntimeError(
            "TaskManager has not been initialized. "
            "This function must be called after server startup."
        )
    return _task_manager