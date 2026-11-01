"""Takes a series of readings from an ammeter at a fixed rate.

Each reading has a fixed time slot (start + i * interval), so one slow reading
doesn't push all the following ones back.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping

from src.ammeter_client import Ammeter
from src.exceptions import AmmeterFrameworkError
from src.settings import SamplingPlan

logger = logging.getLogger(__name__)

# How often we check for a stop request while waiting, so ctrl+c works
# quickly even on a slow run.
STOP_CHECK_S = 0.05
# Anything shorter than this counts as "on time".
MIN_WAIT_S = 1e-6
# How often the main thread wakes up while the parallel samplers run.
PARALLEL_POLL_S = 0.2


class RunStatus(str, Enum):
    # Every planned reading was attempted.
    COMPLETED = "completed"
    # Stopped because of too many failures in a row.
    ABORTED = "aborted"
    # Stopped by ctrl+c or by a stop request.
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class Sample:
    """One reading. Times are in seconds from the start of the run."""

    index: int
    scheduled_s: float
    started_s: float
    duration_s: float
    # The current in amperes, or None if the reading failed.
    value: float | None
    # "ErrorType: message" when the reading failed.
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def lateness_s(self) -> float:
        return self.started_s - self.scheduled_s


@dataclass(frozen=True)
class SamplingRun:
    """Everything we got from sampling one ammeter."""

    ammeter: str
    plan: SamplingPlan
    # Wall-clock start time, in UTC.
    started_at: datetime
    elapsed_s: float
    status: RunStatus
    stop_reason: str | None
    overruns: int
    samples: tuple[Sample, ...]

    @property
    def values(self) -> list[float]:
        """The values of the readings that worked."""
        return [s.value for s in self.samples if s.ok]

    @property
    def successful_count(self) -> int:
        return sum(1 for s in self.samples if s.ok)

    @property
    def failed_count(self) -> int:
        return len(self.samples) - self.successful_count

    @property
    def achieved_frequency_hz(self) -> float | None:
        """The real rate, measured from the first reading to the last."""
        if len(self.samples) < 2:
            return None
        span = self.samples[-1].started_s - self.samples[0].started_s
        return (len(self.samples) - 1) / span if span > 0 else None

    @property
    def max_lateness_s(self) -> float:
        return max((s.lateness_s for s in self.samples), default=0.0)

    @property
    def late_count(self) -> int:
        """Readings that started more than late_tolerance of an interval late."""
        limit = self.plan.late_tolerance * self.plan.interval_s
        return sum(1 for s in self.samples if s.lateness_s > limit)

    @property
    def timing_ok(self) -> bool:
        """False when too many readings were late for the planned rate."""
        if not self.samples:
            return True
        return self.late_count / len(self.samples) <= self.plan.max_late_fraction

    def timing_problem(self) -> str | None:
        """A sentence about the timing when it wasn't met, with the numbers."""
        if self.timing_ok:
            return None
        achieved = self.achieved_frequency_hz
        achieved_text = f"{achieved:.1f} Hz" if achieved is not None else "n/a"
        return (
            f"timing not met: {self.late_count} of {len(self.samples)} readings started more "
            f"than {self.plan.late_tolerance:g} of an interval late "
            f"(allowed: {self.plan.max_late_fraction:.0%}); {achieved_text} achieved, "
            f"planned {self.plan.frequency_hz:g} Hz"
        )

    def summary(self) -> str:
        frequency = self.achieved_frequency_hz
        achieved = f"{frequency:.2f} Hz" if frequency is not None else "n/a"
        text = (
            f"{self.ammeter}: {self.status.value}, "
            f"{self.successful_count}/{self.plan.count} ok, "
            f"{self.failed_count} failed, {achieved} achieved "
            f"(planned {self.plan.frequency_hz:g} Hz), "
            f"{self.overruns} overrun(s), max lateness {self.max_lateness_s * 1000:.1f} ms"
        )
        if self.stop_reason:
            text += f". {self.stop_reason}"
        if not self.timing_ok:
            text += f". {self.timing_problem()}"
        return text


def collect_samples(
    ammeter: Ammeter,
    plan: SamplingPlan,
    *,
    stop_event: threading.Event | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> SamplingRun:
    """Takes plan.count readings from one ammeter at plan.frequency_hz.

    A failed reading is saved and sampling goes on. It stops early after too
    many failures in a row, or on ctrl+c / stop_event, and still returns what
    it collected. clock and sleep can be replaced in tests by a fake clock.
    """
    stop_event = stop_event or threading.Event()
    samples: list[Sample] = []
    overruns = 0
    consecutive_failures = 0
    status = RunStatus.COMPLETED
    stop_reason: str | None = None

    logger.info(
        "%s: sampling %d readings at %g Hz (%.2f s)",
        ammeter.name, plan.count, plan.frequency_hz, plan.duration_s,
    )
    started_at = datetime.now(timezone.utc)
    start = clock()

    def elapsed() -> float:
        return clock() - start

    def wait_until(target_s: float) -> None:
        # Sleep in small steps so a stop request is noticed quickly.
        while not stop_event.is_set():
            remaining = target_s - elapsed()
            # Don't bother with tiny waits. On the fake clock they can even
            # get lost in rounding and the loop would never end.
            if remaining <= MIN_WAIT_S:
                return
            sleep(min(remaining, STOP_CHECK_S))

    try:
        for index in range(plan.count):
            scheduled = index * plan.interval_s
            if elapsed() > scheduled and index > 0:
                # The previous reading took longer than its slot, so this one
                # starts right away instead of waiting.
                overruns += 1
                logger.debug("%s: reading %d starts late", ammeter.name, index)
            else:
                wait_until(scheduled)
            if stop_event.is_set():
                status, stop_reason = RunStatus.INTERRUPTED, "Stop requested"
                break

            started = elapsed()
            try:
                value, error = ammeter.measure(), None
            except AmmeterFrameworkError as e:
                value, error = None, f"{type(e).__name__}: {e}"
            finished = elapsed()
            samples.append(Sample(index, scheduled, started, finished - started, value, error))

            if error is None:
                consecutive_failures = 0
                logger.debug("%s: reading %d = %r A", ammeter.name, index, value)
                continue
            consecutive_failures += 1
            logger.warning("%s: reading %d failed: %s", ammeter.name, index, error)
            if consecutive_failures >= plan.max_consecutive_failures:
                status = RunStatus.ABORTED
                stop_reason = f"Stopped after {consecutive_failures} failures in a row"
                break
    except KeyboardInterrupt:
        # Keep the readings we already have instead of losing them.
        status, stop_reason = RunStatus.INTERRUPTED, "Interrupted by user (Ctrl+C)"

    run = SamplingRun(
        ammeter=ammeter.name,
        plan=plan,
        started_at=started_at,
        elapsed_s=elapsed(),
        status=status,
        stop_reason=stop_reason,
        overruns=overruns,
        samples=tuple(samples),
    )
    all_good = status is RunStatus.COMPLETED and not run.failed_count and run.timing_ok
    log = logger.info if all_good else logger.warning
    log("%s", run.summary())
    return run


def collect_samples_parallel(
    ammeters: Mapping[str, Ammeter],
    plan: SamplingPlan,
    *,
    stop_event: threading.Event | None = None,
) -> dict[str, SamplingRun]:
    """Samples several ammeters at the same time, one thread for each.

    The threads only share stop_event. Ctrl+c sets it, and every sampler
    stops and returns what it has so far.
    """
    if not ammeters:
        return {}
    stop_event = stop_event or threading.Event()

    with ThreadPoolExecutor(max_workers=len(ammeters), thread_name_prefix="sampler") as pool:
        futures = {
            name: pool.submit(collect_samples, ammeter, plan, stop_event=stop_event)
            for name, ammeter in ammeters.items()
        }
        pending = set(futures.values())
        try:
            # Short waits, because on Windows a long blocking wait can miss ctrl+c.
            while pending:
                _, pending = wait(pending, timeout=PARALLEL_POLL_S)
        except KeyboardInterrupt:
            logger.warning("Interrupted by user: stopping all samplers")
            stop_event.set()
        # If a sampler crashed because of a bug, the error comes up here.
        return {name: future.result() for name, future in futures.items()}
