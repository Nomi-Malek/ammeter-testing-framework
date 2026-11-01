"""Tests for the fault simulation."""
import pytest

from src.analysis import compute_stats
from src.exceptions import AmmeterConnectionError, AmmeterTimeoutError, InvalidResponseError
from src.settings import FaultProfile
from src.simulation import FaultInjectingAmmeter

FAILURES = (AmmeterTimeoutError, AmmeterConnectionError, InvalidResponseError)
FLAKY = FaultProfile(timeout_probability=0.1, disconnect_probability=0.05,
                     invalid_response_probability=0.2)


class Constant:
    def __init__(self, name="meter", value=10.0):
        self.name, self.value, self.calls = name, value, 0

    def measure(self) -> float:
        self.calls += 1
        return self.value


def outcomes(profile, seed=1, name="meter", n=2000):
    """What n readings give back: the value, or the name of the error."""
    ammeter = FaultInjectingAmmeter(Constant(name), profile, seed, sleep=lambda s: None)
    results = []
    for _ in range(n):
        try:
            results.append(round(ammeter.measure(), 9))
        except FAILURES as e:
            results.append(type(e).__name__)
    return results, ammeter


def test_same_seed_same_faults():
    assert outcomes(FLAKY)[0] == outcomes(FLAKY)[0]


def test_different_seed_or_ammeter_different_faults():
    first = outcomes(FLAKY)[0]
    assert first != outcomes(FLAKY, seed=2)[0]
    assert first != outcomes(FLAKY, name="other")[0]


def test_failure_rates_and_counts():
    results, ammeter = outcomes(FLAKY)
    n = len(results)
    assert results.count("AmmeterTimeoutError") / n == pytest.approx(0.10, abs=0.03)
    assert results.count("AmmeterConnectionError") / n == pytest.approx(0.05, abs=0.02)
    assert results.count("InvalidResponseError") / n == pytest.approx(0.20, abs=0.03)
    assert ammeter.injected["timeout"] == results.count("AmmeterTimeoutError")


def test_a_failed_reading_does_not_reach_the_device():
    results, ammeter = outcomes(FLAKY)
    assert ammeter._inner.calls == sum(isinstance(r, float) for r in results)


def test_bias_is_exact_and_does_not_change_which_readings_fail():
    biased_profile = FaultProfile(bias_relative=0.2, timeout_probability=0.1,
                                  disconnect_probability=0.05, invalid_response_probability=0.2)
    biased = outcomes(biased_profile)[0]
    plain = outcomes(FLAKY)[0]
    assert [isinstance(r, str) for r in biased] == [isinstance(r, str) for r in plain]
    assert all(r == 12.0 for r in biased if not isinstance(r, str))


def test_spikes():
    results, _ = outcomes(FaultProfile(spike_probability=0.1, spike_factor=10))
    assert set(results) == {10.0, 100.0}
    assert results.count(100.0) / len(results) == pytest.approx(0.1, abs=0.03)


def test_noise():
    stats = compute_stats(outcomes(FaultProfile(noise_relative=0.05))[0])
    assert stats.mean == pytest.approx(10, abs=0.05) and stats.std == pytest.approx(0.5, abs=0.05)


def test_slow_readings_sleep():
    slept = []
    ammeter = FaultInjectingAmmeter(
        Constant(), FaultProfile(slow_probability=1, slow_delay_seconds=0.15), 1,
        sleep=slept.append, profile_name="slow")
    ammeter.measure()
    ammeter.measure()
    assert slept == [0.15, 0.15]
    described = ammeter.describe()
    assert described["profile"] == "slow" and described["seed"] == 1
    assert described["injected"] == {"slow": 2}
