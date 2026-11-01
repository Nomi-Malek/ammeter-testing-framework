"""Accuracy assessment (src/accuracy.py)."""
import dataclasses
import math
import random

import pytest

from src.accuracy import (accuracy_report, assess, mean_confidence_interval, rank, skewness,
                          t_critical)
from src.analysis import analyze_run, compute_stats
from src.settings import FaultProfile
from src.simulation import FaultInjectingAmmeter
from tests.conftest import FakeClock, GaussianAmmeter, result_of

# t values


def test_t_table_values():
    assert t_critical(1) == 12.706 and t_critical(19) == 2.093
    assert t_critical(30, 0.99) == 2.750 and t_critical(10, 0.90) == 1.812


@pytest.mark.parametrize(
    "df, level, published",
    [(31, 0.95, 2.040), (40, 0.95, 2.021), (60, 0.95, 2.000), (120, 0.95, 1.980),
     (40, 0.99, 2.704), (60, 0.99, 2.660), (60, 0.90, 1.671)],
)
def test_t_expansion_above_30_matches_published_values(df, level, published):
    assert t_critical(df, level) == pytest.approx(published, abs=0.002)


@pytest.mark.parametrize("level", [0.90, 0.95, 0.99])
def test_t_decreases_with_more_degrees_of_freedom(level):
    values = [t_critical(df, level) for df in range(1, 300)]
    assert all(a > b for a, b in zip(values, values[1:]))


@pytest.mark.parametrize("df, level", [(0, 0.95), (5, 0.8)])
def test_t_rejects_bad_input(df, level):
    with pytest.raises(ValueError):
        t_critical(df, level)


# Interval and skewness

def test_confidence_interval_on_known_data():
    stats = compute_stats([1, 2, 2, 3, 10])
    interval = mean_confidence_interval(stats)
    assert interval.half_width == pytest.approx(2.776 * stats.sem)
    # Centered on the mean, 3.6.
    assert interval.low + interval.high == pytest.approx(7.2)
    assert interval.contains(3.6) and not interval.contains(100)
    assert mean_confidence_interval(compute_stats([5.0])) is None


def test_skewness():
    assert skewness([1, 2, 3, 4, 5]) == 0
    # A long tail to the right.
    assert skewness([1, 2, 2, 3, 10]) > 1.5
    assert skewness([2, 2, 2]) is None and skewness([1, 2]) is None


@pytest.mark.slow
def test_interval_coverage_with_the_emulator_formulas():
    """How often a 95% interval really contains the true mean.

    Same formulas as the emulators. Greenlee's long tail makes the interval
    miss far more often than 5%; that is why the report warns about skew.
    """
    rng = random.Random(7)
    formulas = {
        "greenlee": (lambda: rng.uniform(1, 10) / rng.uniform(0.1, 100),
                     5.5 * math.log(1000) / 99.9),
        "entes": (lambda: rng.uniform(0.01, 0.1) * rng.uniform(500, 2000), 68.75),
        "circutor": (lambda: sum(rng.uniform(0.1, 1) for _ in range(10))
                     * rng.uniform(0.001, 0.01), 0.03025),
    }
    coverage = {}
    for name, (draw, true_mean) in formulas.items():
        hits = sum(
            mean_confidence_interval(compute_stats([draw() for _ in range(20)])).contains(true_mean)
            for _ in range(2000)
        )
        coverage[name] = hits / 2000
    assert coverage["greenlee"] < 0.70
    assert coverage["entes"] > 0.91 and coverage["circutor"] > 0.91


# Assessment and ranking

def gaussian(name, mean=5.0, rel_std=0.2, fail_at=(), seed=3, count=100, profile=None):
    meter = GaussianAmmeter(FakeClock(), name=name, mean=mean, rel_std=rel_std,
                            fail_at=fail_at, seed=seed)
    source = FaultInjectingAmmeter(meter, profile, 1) if profile else meter
    return result_of(source, count=count, clock=meter.clock, max_failures=10)


def test_success_rate_comes_before_precision():
    # 20 failures out of 100 is clearly more than chance.
    failing_but_precise = gaussian("alpha", rel_std=0.01, fail_at=set(range(0, 100, 5)))
    reliable = gaussian("beta", rel_std=0.2)
    noisy = gaussian("gamma", rel_std=0.5)
    ranked = rank([assess(r) for r in (noisy, failing_but_precise, reliable)])
    assert [a.ammeter for a in ranked] == ["beta", "gamma", "alpha"]


def test_a_biased_meter_is_not_most_reliable_even_if_it_never_fails():
    # The reviewer's case: one ammeter reads 26% high but never fails, the
    # other one is right but a third of its readings fail.
    biased = assess(gaussian("biased", seed=4, count=200,
                             profile=FaultProfile(bias_relative=0.26)), reference=5.0)
    accurate = assess(gaussian("accurate", seed=5, count=200,
                               fail_at=set(range(0, 200, 3))), reference=5.0)
    assert biased.proven_bias and not accurate.proven_bias
    assert [a.ammeter for a in rank([biased, accurate])] == ["accurate", "biased"]
    report = accuracy_report([biased, accurate])
    assert "Most reliable: accurate, because biased reads" in report
    # Only one of them is biased, so the report mustn't say "all of them".
    assert "all of them" not in report


def test_a_small_bias_within_the_tolerance_does_not_count():
    # With 2000 readings even a 2% bias is statistically significant, but it's
    # inside the 5% tolerance, so it isn't held against the ammeter.
    slightly_off = assess(gaussian("slightly_off", rel_std=0.05, count=2000,
                                   profile=FaultProfile(bias_relative=0.02)),
                          reference=5.0, bias_tolerance=0.05)
    assert slightly_off.bias_significant and not slightly_off.proven_bias


def test_a_chance_difference_in_success_rate_moves_on_to_precision():
    # 99% against 100% out of 100 readings is chance, so precision decides.
    precise = assess(gaussian("precise", rel_std=0.05, count=100, fail_at={50}))
    noisy = assess(gaussian("noisy", rel_std=0.5, count=100))
    assert [a.ammeter for a in rank([noisy, precise])] == ["precise", "noisy"]
    report = accuracy_report([noisy, precise])
    assert "Most reliable: precise, because its mean is more precise" in report


def test_simulated_failures_do_not_decide_the_winner():
    # Like flaky_network: every ammeter gets the same chance of a simulated
    # failure, so a different success rate is luck, not the ammeter.
    def simulated(result):
        return dataclasses.replace(result, metadata={"simulation": {"profile": "flaky"}})

    lucky = gaussian("lucky", rel_std=0.3, count=60, seed=1)
    unlucky = gaussian("unlucky", rel_std=0.3, count=60, seed=2, fail_at=set(range(0, 60, 3)))

    # Real failures: the success rate decides.
    real = accuracy_report([assess(lucky), assess(unlucky)])
    assert "because more of its readings succeeded" in real

    # Simulated failures: the success rate is skipped, and only the next step
    # (precision) may decide.
    sim = accuracy_report([assess(simulated(lucky)), assess(simulated(unlucky))])
    assert "more of its readings succeeded" not in sim


def test_all_of_them_is_said_only_when_all_are_biased():
    one = assess(gaussian("one", seed=1, count=200, rel_std=0.05,
                          profile=FaultProfile(bias_relative=0.3)), reference=5.0)
    two = assess(gaussian("two", seed=2, count=200, rel_std=0.5,
                          profile=FaultProfile(bias_relative=0.3)), reference=5.0)
    assert one.proven_bias and two.proven_bias
    assert "all of them have a bias" in accuracy_report([one, two])


def test_injected_bias_is_found_and_ranks_the_meter_lower():
    biased = gaussian("delta", seed=9, count=200, profile=FaultProfile(bias_relative=0.2))
    unbiased = gaussian("beta")
    with_reference = assess(biased, reference=5.0)
    assert with_reference.bias == pytest.approx(0.2, abs=0.05)
    assert with_reference.bias_significant and with_reference.proven_bias
    # Narrower interval (more readings), but a proven bias: ranked below.
    assert [a.ammeter for a in rank([with_reference, assess(unbiased, 5.0)])] == ["beta", "delta"]
    # Without a reference the bias is unknown, so precision decides.
    assert rank([assess(biased), assess(unbiased)])[0].ammeter == "delta"
    assert assess(unbiased).bias is None
    report = accuracy_report([with_reference, assess(unbiased, 5.0)])
    assert "Most reliable: beta" in report and "*" in report


def test_unbiased_meter_has_no_significant_bias():
    assessment = assess(gaussian("beta"), reference=5.0)
    assert assessment.bias_significant is False and abs(assessment.bias) < 0.05


def test_skewed_data_is_marked_with_a_question_mark():
    result = gaussian("g", count=20)
    values = [math.exp(random.Random(i).gauss(0, 1.2)) for i in range(20)]
    run = dataclasses.replace(result.run, samples=tuple(
        dataclasses.replace(s, value=v) for s, v in zip(result.run.samples, values)))
    skewed = dataclasses.replace(result, run=run, analysis=analyze_run(run))
    assessment = assess(skewed, reference=100.0)
    assert assessment.strongly_skewed and assessment.bias_significant
    assert not assessment.proven_bias
    assert any("sampling effect" in w for w in assessment.warnings)
    assert "?" in accuracy_report([assessment])


def test_small_difference_in_precision_is_no_clear_winner():
    # Same kind of ammeter, 20 readings each: whoever is first, it's chance.
    a = assess(gaussian("a", rel_std=0.30, count=20, seed=1))
    b = assess(gaussian("b", rel_std=0.32, count=20, seed=2))
    report = accuracy_report([a, b])
    assert "No clear winner" in report and "Most reliable" not in report


def test_clear_difference_in_precision_names_a_winner():
    precise = assess(gaussian("precise", rel_std=0.05, count=100))
    noisy = assess(gaussian("noisy", rel_std=0.5, count=100))
    report = accuracy_report([noisy, precise])
    assert "Most reliable: precise, because its mean is more precise" in report


def test_the_winner_reason_is_the_real_one():
    # The winner here wins on success rate, even though its interval is wider,
    # so the report must not say "more precise".
    reliable = assess(gaussian("reliable", rel_std=0.3, count=100))
    failing = assess(gaussian("failing", rel_std=0.1, count=100, fail_at=set(range(0, 100, 3))))
    report = accuracy_report([failing, reliable])
    assert "Most reliable: reliable, because more of its readings succeeded" in report


def test_numbers_keep_their_trailing_zeros():
    assessment = assess(gaussian("g", mean=0.03, rel_std=0.0001, count=10))
    assert "0.03000" in accuracy_report([assessment])


def test_dead_ammeter_gives_a_clear_message():
    dead = gaussian("dead", fail_at=range(1000), count=20)
    assessment = assess(dead)
    assert assessment.interval is None
    assert any("no confidence interval" in w for w in assessment.warnings)
    assert "Most reliable: none" in accuracy_report([assessment])
    assert accuracy_report([]) == "No results to assess."
