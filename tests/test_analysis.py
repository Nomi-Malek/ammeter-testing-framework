"""Tests for the statistics and the result report."""
import math
import statistics

import pytest

from src.analysis import analyze_run, compute_stats
from src.results import TestResult, compare_results
from tests.conftest import FakeAmmeter, result_of, sample


def test_known_values():
    stats = compute_stats([1, 2, 2, 3, 10])
    assert stats.count == 5 and stats.mean == pytest.approx(3.6) and stats.median == 2
    assert (stats.min, stats.max, stats.range) == (1, 10, 9)
    # Sample std, so it divides by n - 1.
    assert stats.std == pytest.approx(math.sqrt(53.2 / 4))
    assert stats.cv == pytest.approx(stats.std / 3.6)
    assert stats.sem == pytest.approx(stats.std / math.sqrt(5))


def test_sample_not_population_standard_deviation():
    values = [1, 2, 2, 3, 10]
    assert compute_stats(values).std != pytest.approx(statistics.pstdev(values))


def test_even_count_median():
    assert compute_stats([4, 1, 3, 2]).median == 2.5


def test_no_values_gives_none_not_an_error():
    stats = compute_stats([])
    assert stats.count == 0
    assert all(getattr(stats, f) is None
               for f in ("mean", "median", "std", "min", "max", "range", "cv", "sem"))


def test_one_value_has_no_spread():
    stats = compute_stats([0.05])
    assert stats.mean == stats.median == 0.05 and stats.range == 0
    assert stats.std is None and stats.cv is None and stats.sem is None


def test_cv_is_none_when_the_mean_is_zero():
    stats = compute_stats([-1.0, 1.0])
    assert stats.mean == 0 and stats.cv is None and stats.std is not None


def test_accepts_any_iterable_and_keeps_full_precision():
    tiny = [0.0123456789, 0.0123456790, 0.0123456791]
    assert compute_stats(iter(tiny)).mean == statistics.fmean(tiny)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "1.5", None, True])
def test_rejects_values_that_are_not_finite_numbers(bad):
    with pytest.raises(ValueError):
        compute_stats([1.0, bad])


def test_run_analysis_includes_failed_readings_in_timing(clock):
    run = sample(FakeAmmeter(clock, cost=0.003, fail_at={3}), count=10)
    analysis = analyze_run(run)
    assert analysis.current.count == 9 and analysis.success_rate == 0.9
    assert analysis.reading_time.count == 10
    # In milliseconds.
    assert analysis.reading_time.mean == pytest.approx(3.0)
    assert analysis.lateness.max < 1e-6


def test_report_of_a_dead_ammeter_does_not_crash(clock):
    result = result_of(FakeAmmeter(clock, fail_at=range(100)), count=10, max_failures=3)
    report = result.report()
    assert "aborted" in report and "n/a" in report
    assert "Not saved" in report


def test_test_result_is_not_collected_by_pytest():
    assert TestResult.__test__ is False
    assert "__test__" not in TestResult.__dataclass_fields__


def test_comparison_of_different_ammeters_explains_itself(clock):
    a = result_of(FakeAmmeter(clock, name="a"))
    b = result_of(FakeAmmeter(clock, name="b"))
    table = compare_results([a, b])
    assert "(baseline)" in table and "not comparable" in table
    assert compare_results([]) == "No results to compare."
