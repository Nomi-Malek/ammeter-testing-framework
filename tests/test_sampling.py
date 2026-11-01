"""Tests for sampling at a fixed rate.

The timing tests use a fake clock, so they're exact and fast on any machine.
"""
import threading
import time
from datetime import timedelta

import pytest

from src.exceptions import InvalidResponseError
from src.sampling import RunStatus, collect_samples, collect_samples_parallel
from src.settings import SamplingPlan
from tests.conftest import FakeAmmeter, FakeClock, emulator_client, sample


def test_readings_follow_the_schedule_without_drift(clock):
    run = sample(FakeAmmeter(clock, cost=0.005), count=600, frequency_hz=10)
    assert run.status is RunStatus.COMPLETED and len(run.samples) == 600
    assert run.overruns == 0
    assert all(abs(s.started_s - s.scheduled_s) < 1e-9 for s in run.samples)
    assert run.samples[-1].started_s == pytest.approx(59.9)


def test_slow_readings_overrun_without_a_catch_up_burst(clock):
    run = sample(FakeAmmeter(clock, cost=0.15), count=10, frequency_hz=10)
    assert len(run.samples) == 10 and run.overruns == 9
    gaps = {round(b.started_s - a.started_s, 6) for a, b in zip(run.samples, run.samples[1:])}
    # One right after the other. A catch-up burst would show gaps near 0.
    assert gaps == {0.15}


def test_a_run_that_misses_its_rate_does_not_meet_timing(clock):
    # The reviewer's case: each reading takes 0.2 s at 10 Hz, so the real
    # rate is 5 Hz and 19 of the 20 readings start late.
    run = sample(FakeAmmeter(clock, cost=0.2), count=20, frequency_hz=10)
    assert run.status is RunStatus.COMPLETED
    assert run.late_count == 19
    assert run.achieved_frequency_hz == pytest.approx(5)
    assert not run.timing_ok
    assert "timing not met" in run.summary()


def test_small_delays_are_not_late(clock):
    # 0.04 s late at 10 Hz is less than half an interval (0.05 s): on time.
    class SlightlyLate(FakeAmmeter):
        def measure(self) -> float:
            value = super().measure()
            self.clock.now += 0.04
            return value

    run = sample(SlightlyLate(clock), count=20, frequency_hz=10)
    assert run.late_count == 0 and run.timing_ok


def test_the_allowed_share_of_late_readings_comes_from_the_plan(clock):
    # 1 late reading out of 20 is 5%: fine at 10%, not fine at 2%.
    class OneSlow(FakeAmmeter):
        def measure(self) -> float:
            value = super().measure()
            if self.calls == 5:
                self.clock.now += 0.3
            return value

    lenient = sample(OneSlow(clock), count=20, frequency_hz=10)
    strict_clock = FakeClock()
    strict = collect_samples(
        OneSlow(strict_clock), SamplingPlan(20, 10.0, max_late_fraction=0.02),
        clock=strict_clock.time, sleep=strict_clock.sleep,
    )
    assert lenient.late_count == strict.late_count >= 1
    assert lenient.timing_ok and not strict.timing_ok


def test_failed_readings_are_recorded_and_the_run_continues(clock):
    run = sample(FakeAmmeter(clock, fail_at={2, 5, 6}), count=10)
    assert run.status is RunStatus.COMPLETED
    assert run.failed_count == 3 and run.successful_count == 7
    assert run.values == [0, 1, 3, 4, 7, 8, 9]
    assert run.samples[2].error.startswith("AmmeterConnectionError")


def test_run_aborts_after_too_many_failures_in_a_row(clock):
    run = sample(FakeAmmeter(clock, fail_at=range(3, 100)), count=50, max_failures=5)
    assert run.status is RunStatus.ABORTED and len(run.samples) == 8
    assert "5 failures in a row" in run.stop_reason


def test_invalid_replies_count_as_failures(clock):
    class BadReply(FakeAmmeter):
        def measure(self) -> float:
            raise InvalidResponseError("reply is not a number: 'abc'")

    run = sample(BadReply(clock), count=10, max_failures=3)
    assert run.status is RunStatus.ABORTED and run.failed_count == 3


def test_ctrl_c_keeps_the_readings_taken_so_far(clock):
    class CtrlC(FakeAmmeter):
        def measure(self) -> float:
            if self.calls == 4:
                raise KeyboardInterrupt
            return super().measure()

    run = sample(CtrlC(clock), count=10)
    assert run.status is RunStatus.INTERRUPTED and len(run.samples) == 4


def test_stop_event_ends_a_slow_run_quickly():
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    start = time.perf_counter()
    run = collect_samples(FakeAmmeter(FakeClock()), SamplingPlan(5, 0.2), stop_event=stop)
    assert run.status is RunStatus.INTERRUPTED and len(run.samples) == 1
    # The next reading was only due after 5 seconds.
    assert time.perf_counter() - start < 3.0


def test_summary_and_derived_values(clock):
    run = sample(FakeAmmeter(clock), count=11, frequency_hz=10)
    assert run.achieved_frequency_hz == pytest.approx(10)
    assert "11/11 ok" in run.summary()


def test_parallel_sampling_of_real_emulators(emulator_ports):
    ammeters = {name: emulator_client(name, port) for name, port in emulator_ports.items()}
    runs = collect_samples_parallel(ammeters, SamplingPlan(20, 20.0))
    for run in runs.values():
        assert run.status is RunStatus.COMPLETED and run.successful_count == 20
    # In parallel the three runs overlap in time: every run started before
    # any other one ended. This doesn't depend on how fast the machine is.
    ends = [run.started_at + timedelta(seconds=run.elapsed_s) for run in runs.values()]
    assert max(run.started_at for run in runs.values()) < min(ends)
