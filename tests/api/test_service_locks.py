"""Lifecycle and serialization contracts for process-local run locks."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest

from agent_reliability_lab.api.services import _RunLocks

from .conftest import assert_error


def test_same_run_waiters_are_serialized_and_last_user_evicts_lock() -> None:
    """Dropping waiter references early would let one run execute concurrently."""
    locks = _RunLocks()
    run_id = uuid4()
    first_entered = Event()
    release_first = Event()
    second_started = Event()
    second_entered = Event()

    def first() -> None:
        with locks.hold(run_id):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        second_started.set()
        with locks.hold(run_id):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first)
        assert first_entered.wait(timeout=2)
        second_future = pool.submit(second)
        assert second_started.wait(timeout=2)
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        first_future.result(timeout=2)
        second_future.result(timeout=2)

    assert locks._locks == {}


def test_run_lock_is_evicted_when_holder_raises() -> None:
    """An exception leaving a lock entry behind would leak per-run state."""
    locks = _RunLocks()

    with pytest.raises(RuntimeError, match="boom"), locks.hold(uuid4()):
        raise RuntimeError("boom")

    assert locks._locks == {}


def test_missing_resume_requests_do_not_accumulate_run_locks(client, app) -> None:
    """Untrusted missing UUIDs must not grow a permanent in-memory lock map."""
    for value in range(1, 101):
        response = client.post(f"/v1/runs/{UUID(int=value)}/resume")
        assert_error(response, 404, "run_not_found")

    assert app.state.container.runs._locks._locks == {}
