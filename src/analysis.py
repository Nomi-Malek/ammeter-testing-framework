"""Statistics for a series of readings.

The same function is used for the currents and also for the reading times
and lateness, to see how steady the timing is.
"""
import math
import statistics
from dataclasses import dataclass
from typing import Iterable

from src.sampling import SamplingRun


@dataclass(frozen=True)
class MeasurementStats:
    """Basic statistics. A value is None when it can't be calculated:
    std, cv and sem need at least 2 values, the rest need 1.
    """

    count: int
    mean: float | None
    median: float | None
    # Sample standard deviation (divides by n - 1).
    std: float | None
    min: float | None
    max: float | None
    # max - min
    range: float | None
    # Coefficient of variation: std / |mean|. None when the mean is 0.
    cv: float | None
    # Standard error of the mean: std / sqrt(count).
    sem: float | None


def compute_stats(values: Iterable[float]) -> MeasurementStats:
    """Calculates the statistics for a list of numbers.

    We use the sample standard deviation (n - 1), because the readings are a
    sample of what the ammeter can give. Note that numpy's np.std uses n by
    default.

    Raises:
        ValueError: One of the values isn't a finite number.
    """
    data = list(values)
    for value in data:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Statistics need finite numbers, got {value!r}")

    count = len(data)
    if count == 0:
        return MeasurementStats(0, None, None, None, None, None, None, None, None)

    # fmean always returns a float and sums more accurately than sum() / len().
    mean = statistics.fmean(data)
    low, high = min(data), max(data)
    std = statistics.stdev(data) if count >= 2 else None
    return MeasurementStats(
        count=count,
        mean=mean,
        median=statistics.median(data),
        std=std,
        min=low,
        max=high,
        range=high - low,
        cv=std / abs(mean) if std is not None and mean != 0 else None,
        sem=std / math.sqrt(count) if std is not None else None,
    )


@dataclass(frozen=True)
class RunAnalysis:
    """Statistics for one sampling run."""

    # Amperes, only the readings that worked.
    current: MeasurementStats
    # Milliseconds, all the readings.
    reading_time: MeasurementStats
    lateness: MeasurementStats
    # Between 0 and 1, or None if nothing was tried.
    success_rate: float | None


def analyze_run(run: SamplingRun) -> RunAnalysis:
    """Statistics for the currents and for the timing of a run.

    The timing includes failed readings too, since a slow failure is also
    part of how the ammeter behaves.
    """
    attempted = len(run.samples)
    return RunAnalysis(
        current=compute_stats(run.values),
        reading_time=compute_stats(s.duration_s * 1000 for s in run.samples),
        lateness=compute_stats(s.lateness_s * 1000 for s in run.samples),
        success_rate=run.successful_count / attempted if attempted else None,
    )
