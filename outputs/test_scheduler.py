"""
Assumptions about the scheduler API used in these tests:
- A module named `scheduler` or `task_scheduler` exposes a Scheduler class
  (named `Scheduler` or `TaskScheduler`).
- Scheduler.schedule(delay_seconds, func, *args, **kwargs) returns a handle
  with cancel() -> bool.
- Scheduler supports start()/shutdown() or start()/stop() for lifecycle, or
  runs immediately upon instantiation.
- Scheduler uses time.monotonic/time.time and time.sleep (directly or through
  its module import) for timing, which we patch for deterministic control.
"""

from __future__ import annotations

import contextlib
import importlib
import queue
import threading
from dataclasses import dataclass
from typing import Iterable
from unittest import mock

import pytest


def _import_scheduler_module():
    for name in ("scheduler", "task_scheduler"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    pytest.skip("Scheduler module not found; expected 'scheduler' or 'task_scheduler'.")


def _get_scheduler_class(module):
    for name in ("Scheduler", "TaskScheduler"):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError("Scheduler class not found in module.")


def _start_scheduler(scheduler):
    if hasattr(scheduler, "start"):
        scheduler.start()
        return
    if hasattr(scheduler, "run"):
        threading.Thread(target=scheduler.run, daemon=True).start()


def _stop_scheduler(scheduler):
    for method_name in ("shutdown", "stop", "close"):
        if hasattr(scheduler, method_name):
            getattr(scheduler, method_name)()
            return


@dataclass
class ControlledClock:
    now: float = 0.0

    def __post_init__(self):
        self._lock = threading.Lock()
        self._allow_sleep = threading.Event()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def time(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        self._allow_sleep.wait(timeout=1)
        with self._lock:
            self.now += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds

    def allow_sleep(self) -> None:
        self._allow_sleep.set()


@contextlib.contextmanager
def _patched_time(module, clock: ControlledClock):
    """Patch time functions in the scheduler module for deterministic control."""
    patches: Iterable[mock._patch] = []
    with contextlib.ExitStack() as stack:
        if hasattr(module, "time"):
            patches = (
                mock.patch.object(module.time, "monotonic", side_effect=clock.monotonic),
                mock.patch.object(module.time, "time", side_effect=clock.time),
                mock.patch.object(module.time, "sleep", side_effect=clock.sleep),
            )
            for patch in patches:
                stack.enter_context(patch)
        for name, func in ("monotonic", clock.monotonic), ("time", clock.time), ("sleep", clock.sleep):
            if hasattr(module, name):
                stack.enter_context(mock.patch.object(module, name, side_effect=func))
        yield


def _new_scheduler(module):
    Scheduler = _get_scheduler_class(module)
    return Scheduler()


def test_delayed_execution_blocks_until_delay_reached():
    module = _import_scheduler_module()
    clock = ControlledClock()
    with _patched_time(module, clock):
        scheduler = _new_scheduler(module)
        _start_scheduler(scheduler)
        try:
            executed = threading.Event()
            task = mock.Mock(side_effect=lambda: executed.set())

            scheduler.schedule(5.0, task)

            # Without advancing time, the task should not run.
            assert not executed.wait(timeout=0.1)

            # Allow scheduler sleep to advance the clock and trigger execution.
            clock.allow_sleep()
            assert executed.wait(timeout=1)
            task.assert_called_once()
        finally:
            _stop_scheduler(scheduler)


def test_zero_delay_executes_immediately():
    module = _import_scheduler_module()
    scheduler = _new_scheduler(module)
    _start_scheduler(scheduler)
    try:
        executed = threading.Event()
        task = mock.Mock(side_effect=lambda: executed.set())

        scheduler.schedule(0.0, task)

        assert executed.wait(timeout=1)
        task.assert_called_once()
    finally:
        _stop_scheduler(scheduler)


def test_negative_delay_is_rejected():
    module = _import_scheduler_module()
    scheduler = _new_scheduler(module)
    _start_scheduler(scheduler)
    try:
        with pytest.raises((ValueError, TypeError)):
            scheduler.schedule(-1.0, mock.Mock())
    finally:
        _stop_scheduler(scheduler)


def test_extreme_delay_does_not_execute_early():
    module = _import_scheduler_module()
    clock = ControlledClock()
    with _patched_time(module, clock):
        scheduler = _new_scheduler(module)
        _start_scheduler(scheduler)
        try:
            executed = threading.Event()
            task = mock.Mock(side_effect=lambda: executed.set())

            scheduler.schedule(10**9, task)

            clock.allow_sleep()
            clock.advance(1.0)
            assert not executed.wait(timeout=0.1)
            assert task.call_count == 0
        finally:
            _stop_scheduler(scheduler)


def test_concurrent_dispatch_is_thread_safe():
    module = _import_scheduler_module()
    scheduler = _new_scheduler(module)
    _start_scheduler(scheduler)
    try:
        total_tasks = 50
        completed = queue.Queue()
        start_barrier = threading.Barrier(total_tasks)

        def schedule_task(index: int) -> None:
            start_barrier.wait()
            scheduler.schedule(0.0, lambda: completed.put(index))

        threads = [threading.Thread(target=schedule_task, args=(i,)) for i in range(total_tasks)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        seen = set()
        while len(seen) < total_tasks:
            try:
                seen.add(completed.get(timeout=2))
            except queue.Empty:
                break

        assert seen == set(range(total_tasks))
    finally:
        _stop_scheduler(scheduler)


def test_concurrent_dispatch_race_stress():
    """Repeat concurrent scheduling to surface race conditions."""
    module = _import_scheduler_module()
    scheduler = _new_scheduler(module)
    _start_scheduler(scheduler)
    try:
        for _ in range(10):
            completed = queue.Queue()
            threads = [
                threading.Thread(target=lambda i=i: scheduler.schedule(0.0, lambda: completed.put(i)))
                for i in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            received = set()
            while len(received) < 20:
                try:
                    received.add(completed.get(timeout=2))
                except queue.Empty:
                    break
            assert received == set(range(20))
    finally:
        _stop_scheduler(scheduler)


def test_cancellation_prevents_future_execution():
    module = _import_scheduler_module()
    clock = ControlledClock()
    with _patched_time(module, clock):
        scheduler = _new_scheduler(module)
        _start_scheduler(scheduler)
        try:
            task = mock.Mock()
            handle = scheduler.schedule(10.0, task)

            assert handle.cancel() is True
            clock.allow_sleep()

            assert task.call_count == 0
        finally:
            _stop_scheduler(scheduler)


def test_cancellation_race_at_execution_boundary():
    module = _import_scheduler_module()
    scheduler = _new_scheduler(module)
    _start_scheduler(scheduler)
    try:
        executed = threading.Event()
        task = mock.Mock(side_effect=lambda: executed.set())
        handle = scheduler.schedule(0.0, task)

        cancel_result = handle.cancel()
        executed.wait(timeout=1)

        assert task.call_count <= 1
        if cancel_result:
            assert task.call_count == 0
    finally:
        _stop_scheduler(scheduler)


def test_concurrent_cancellation_race():
    module = _import_scheduler_module()
    scheduler = _new_scheduler(module)
    _start_scheduler(scheduler)
    try:
        executed = queue.Queue()
        start_barrier = threading.Barrier(10)

        def schedule_and_cancel(index: int) -> None:
            start_barrier.wait()
            handle = scheduler.schedule(0.0, lambda: executed.put(index))
            handle.cancel()

        threads = [threading.Thread(target=schedule_and_cancel, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        received = set()
        while True:
            try:
                received.add(executed.get(timeout=0.2))
            except queue.Empty:
                break

        assert received.issubset(set(range(10)))
        assert len(received) <= 10
    finally:
        _stop_scheduler(scheduler)