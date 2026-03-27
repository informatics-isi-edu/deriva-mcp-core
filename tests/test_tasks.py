"""Unit tests for TaskManager."""

from __future__ import annotations

import asyncio

import pytest

from deriva_mcp_core.tasks.manager import TaskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager() -> TaskManager:
    return TaskManager(token_cache=None)


async def _noop() -> dict:
    return {"done": True}


async def _slow(delay: float = 0.05) -> dict:
    await asyncio.sleep(delay)
    return {"done": True}


async def _failing() -> None:
    raise ValueError("task error")


# ---------------------------------------------------------------------------
# submit + get
# ---------------------------------------------------------------------------


async def test_submit_returns_task_id():
    mgr = _make_manager()
    task_id = mgr.submit(_noop(), name="test", principal="u1", bearer_token=None)
    assert isinstance(task_id, str) and len(task_id) == 36  # UUID4


async def test_submit_rejects_non_coroutine():
    mgr = _make_manager()
    with pytest.raises(TypeError, match="asyncio.to_thread"):
        mgr.submit(lambda: None, name="bad", principal="u1", bearer_token=None)


async def test_submit_rejects_sync_return_value():
    mgr = _make_manager()

    def _sync_fn() -> dict:
        return {"done": True}

    with pytest.raises(TypeError, match="asyncio.to_thread"):
        mgr.submit(_sync_fn(), name="bad", principal="u1", bearer_token=None)


async def test_get_returns_record_for_owner():
    mgr = _make_manager()
    task_id = mgr.submit(_noop(), name="test", principal="u1", bearer_token=None)
    await asyncio.sleep(0)  # let task run
    record = mgr.get(task_id, "u1")
    assert record is not None
    assert record.task_id == task_id
    assert record.name == "test"
    assert record.principal == "u1"


async def test_get_returns_none_for_other_principal():
    mgr = _make_manager()
    task_id = mgr.submit(_noop(), name="test", principal="u1", bearer_token=None)
    await asyncio.sleep(0)
    assert mgr.get(task_id, "u2") is None


async def test_get_returns_none_for_unknown_id():
    mgr = _make_manager()
    assert mgr.get("no-such-id", "u1") is None


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


async def test_task_completes_successfully():
    mgr = _make_manager()
    task_id = mgr.submit(_noop(), name="test", principal="u1", bearer_token=None)
    await asyncio.sleep(0.01)
    record = mgr.get(task_id, "u1")
    assert record is not None
    assert record.state == "completed"
    assert record.result == {"done": True}
    assert record.error is None
    assert record.started_at is not None
    assert record.completed_at is not None


async def test_task_fails_on_exception():
    mgr = _make_manager()
    task_id = mgr.submit(_failing(), name="failing", principal="u1", bearer_token=None)
    await asyncio.sleep(0.01)
    record = mgr.get(task_id, "u1")
    assert record is not None
    assert record.state == "failed"
    assert "task error" in record.error
    assert record.result is None


async def test_task_is_pending_then_running():
    """Verify state transitions: on submission the task is pending, then running."""
    mgr = _make_manager()
    task_id = mgr.submit(_slow(0.1), name="slow", principal="u1", bearer_token=None)
    record = mgr.get(task_id, "u1")
    # Before any yield: state is "pending"
    assert record is not None
    assert record.state == "pending"
    await asyncio.sleep(0)
    # After one yield: task wrapper has started, state is "running"
    assert record.state == "running"
    await asyncio.sleep(0.15)
    assert record.state == "completed"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_all_for_principal():
    mgr = _make_manager()
    mgr.submit(_noop(), name="t1", principal="u1", bearer_token=None)
    mgr.submit(_noop(), name="t2", principal="u1", bearer_token=None)
    mgr.submit(_noop(), name="t3", principal="u2", bearer_token=None)
    await asyncio.sleep(0.01)
    records = mgr.list("u1")
    assert len(records) == 2
    assert all(r.principal == "u1" for r in records)


async def test_list_status_filter():
    mgr = _make_manager()
    mgr.submit(_noop(), name="ok", principal="u1", bearer_token=None)
    mgr.submit(_failing(), name="bad", principal="u1", bearer_token=None)
    await asyncio.sleep(0.01)
    completed = mgr.list("u1", status="completed")
    failed = mgr.list("u1", status="failed")
    assert len(completed) == 1
    assert completed[0].name == "ok"
    assert len(failed) == 1
    assert failed[0].name == "bad"


async def test_list_sorted_newest_first():
    mgr = _make_manager()
    for i in range(3):
        mgr.submit(_noop(), name=f"t{i}", principal="u1", bearer_token=None)
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)
    records = mgr.list("u1")
    created_ats = [r.created_at for r in records]
    assert created_ats == sorted(created_ats, reverse=True)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_cancel_running_task():
    mgr = _make_manager()
    task_id = mgr.submit(_slow(5.0), name="long", principal="u1", bearer_token=None)
    await asyncio.sleep(0)  # let task start
    ok, reason = mgr.cancel(task_id, "u1")
    assert ok is True
    assert reason == ""
    await asyncio.sleep(0.01)
    record = mgr.get(task_id, "u1")
    assert record is not None
    assert record.state == "cancelled"


async def test_cancel_completed_task_returns_false():
    mgr = _make_manager()
    task_id = mgr.submit(_noop(), name="done", principal="u1", bearer_token=None)
    await asyncio.sleep(0.01)
    ok, reason = mgr.cancel(task_id, "u1")
    assert ok is False
    assert "completed" in reason


async def test_cancel_other_principal_returns_false():
    mgr = _make_manager()
    task_id = mgr.submit(_slow(5.0), name="long", principal="u1", bearer_token=None)
    await asyncio.sleep(0)
    ok, reason = mgr.cancel(task_id, "u2")
    assert ok is False
    assert "not found" in reason
    # Clean up
    mgr.cancel(task_id, "u1")


async def test_cancel_unknown_id_returns_false():
    mgr = _make_manager()
    ok, reason = mgr.cancel("no-such-id", "u1")
    assert ok is False
    assert "not found" in reason


# ---------------------------------------------------------------------------
# principal isolation
# ---------------------------------------------------------------------------


async def test_principal_isolation_get():
    mgr = _make_manager()
    t1 = mgr.submit(_noop(), name="t", principal="alice", bearer_token=None)
    t2 = mgr.submit(_noop(), name="t", principal="bob", bearer_token=None)
    await asyncio.sleep(0.01)
    assert mgr.get(t1, "alice") is not None
    assert mgr.get(t1, "bob") is None
    assert mgr.get(t2, "bob") is not None
    assert mgr.get(t2, "alice") is None


# ---------------------------------------------------------------------------
# update_progress
# ---------------------------------------------------------------------------


async def test_update_progress():
    mgr = _make_manager()

    async def _with_progress() -> dict:
        # Need the task_id -- use the record directly via the manager internals
        # (tests may inspect progress via a separate call)
        return {"done": True}

    task_id = mgr.submit(_with_progress(), name="prog", principal="u1", bearer_token=None)
    mgr.update_progress(task_id, "50% done")
    record = mgr.get(task_id, "u1")
    assert record is not None
    assert record.progress == "50% done"
    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# get_credential
# ---------------------------------------------------------------------------


async def test_get_credential_no_cache_raises():
    mgr = _make_manager()  # token_cache=None
    task_id = mgr.submit(_noop(), name="t", principal="u1", bearer_token="tok")
    # credentials are cleared after task completes, so test before completion
    with pytest.raises(RuntimeError, match="Token cache not configured"):
        await mgr.get_credential(task_id)


async def test_get_credential_unknown_task_raises():
    mgr = _make_manager()
    with pytest.raises(RuntimeError, match="No credentials"):
        await mgr.get_credential("no-such-id")


async def test_get_credential_calls_token_cache():
    from unittest.mock import AsyncMock

    mock_cache = AsyncMock()
    mock_cache.get = AsyncMock(return_value="derived-token")

    mgr = TaskManager(token_cache=mock_cache)
    task_id = mgr.submit(_slow(5.0), name="t", principal="alice", bearer_token="bearer-tok")
    await asyncio.sleep(0)  # let task start (state=running, credentials still present)

    cred = await mgr.get_credential(task_id)
    assert cred == {"bearer-token": "derived-token"}
    mock_cache.get.assert_called_once_with("alice", "bearer-tok")

    # Clean up
    mgr.cancel(task_id, "alice")


# ---------------------------------------------------------------------------
# description field
# ---------------------------------------------------------------------------


async def test_submit_with_description():
    mgr = _make_manager()
    task_id = mgr.submit(
        _noop(), name="named", principal="u1", bearer_token=None, description="my desc"
    )
    record = mgr.get(task_id, "u1")
    assert record is not None
    assert record.description == "my desc"
    await asyncio.sleep(0.01)