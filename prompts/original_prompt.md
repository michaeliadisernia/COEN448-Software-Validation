Original prompt: You are testing a Python-based event-driven task scheduler. Generate pytest
tests to validate delayed execution, invalid scheduling times, concurrent
task dispatch, and cancellation rules. Use unittest.mock (or pytest-mock) for time and worker
simulation. The tests must expose race conditions and boundary failures.