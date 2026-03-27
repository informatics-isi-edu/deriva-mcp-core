from __future__ import annotations

"""MCP tools for background task management.

Three read-only tools that let the LLM poll for task status and request
cancellation. All are mutates=False because they do not write to the DERIVA
catalog or Hatrac object store.

Tools:
    get_task_status(task_id)       -- Full record for one task
    list_tasks(status?)            -- All tasks for the calling principal
    cancel_task(task_id)           -- Request cancellation of a running task
"""

import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from ..context import get_request_user_id
from ..tasks.manager import TaskRecord, get_task_manager

if TYPE_CHECKING:
    from ..plugin.api import PluginContext

logger = logging.getLogger(__name__)


def _record_dict(record: TaskRecord) -> dict:
    return asdict(record)


def register(ctx: PluginContext) -> None:
    """Register task management tools against the plugin context."""

    @ctx.tool(mutates=False)
    async def get_task_status(task_id: str) -> str:
        """Return the status and result of a background task.

        Only tasks submitted by the calling principal are accessible.
        Returns an error for unknown or other-user task IDs.

        Args:
            task_id: Task ID returned by a tool that submits background work
                (e.g., clone_catalog_async).
        """
        principal = get_request_user_id()
        try:
            manager = get_task_manager()
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})
        record = manager.get(task_id, principal)
        if record is None:
            return json.dumps({"error": "not found"})
        return json.dumps(_record_dict(record))

    @ctx.tool(mutates=False)
    async def list_tasks(status: str | None = None) -> str:
        """List background tasks submitted by the calling principal.

        Returns tasks sorted by submission time, newest first.

        Args:
            status: Optional filter -- one of "pending", "running",
                "completed", "failed", "cancelled". Omit to return all.
        """
        principal = get_request_user_id()
        valid_states = {"pending", "running", "completed", "failed", "cancelled"}
        if status is not None and status not in valid_states:
            return json.dumps(
                {"error": f"invalid status {status!r}; must be one of {sorted(valid_states)}"}
            )
        try:
            manager = get_task_manager()
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})
        records = manager.list(principal, status=status)
        return json.dumps([_record_dict(r) for r in records])

    @ctx.tool(mutates=False)
    async def cancel_task(task_id: str) -> str:
        """Request cancellation of a running background task.

        Sends an asyncio cancellation signal. The task may take a short time
        to acknowledge cancellation; poll get_task_status to confirm the state
        transitions to "cancelled".

        Only tasks submitted by the calling principal can be cancelled.

        Args:
            task_id: Task ID to cancel.
        """
        principal = get_request_user_id()
        try:
            manager = get_task_manager()
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})
        ok, reason = manager.cancel(task_id, principal)
        if ok:
            return json.dumps({"cancelled": True})
        return json.dumps({"cancelled": False, "reason": reason})