"""Tests for reading and checking the config."""
import copy

import pytest

from src.exceptions import ConfigError
from src.settings import (ConnectionSettings, DEFAULT_SAMPLING_PLAN, FaultProfile,
                          load_settings, make_sampling_plan)
from src.utils.config import load_config

MINIMAL = {"ammeters": {"a": {"port": 6000, "command": "X"}}}


@pytest.fixture
def config() -> dict:
    return copy.deepcopy(load_config())


def problems(config: dict) -> str:
    with pytest.raises(ConfigError) as error:
        load_settings(config)
    return str(error.value)


def test_project_config_is_valid(config):
    settings = load_settings(config)
    assert set(settings.ammeters) == {"greenlee", "entes", "circutor"}
    assert settings.ammeters["entes"].reference_current == 68.75
    assert settings.connection == ConnectionSettings("127.0.0.1", 2.0, 3, 0.1)
    assert settings.sampling.count == 20 and settings.sampling.frequency_hz == 10
    assert {"quick_check", "flaky_network", "biased_meter"} <= set(settings.scenarios)


def test_optional_sections_fall_back_to_defaults():
    settings = load_settings(MINIMAL)
    assert settings.connection == ConnectionSettings()
    assert settings.sampling == DEFAULT_SAMPLING_PLAN
    assert settings.scenarios == {} and settings.simulation.profiles == {}
    assert settings.visualization.enabled and settings.analysis.confidence_level == 0.95


def test_missing_ammeters_section_is_an_error():
    assert "ammeters" in problems({})


def test_all_problems_are_reported_together():
    text = problems({
        "connection": {"host": "", "timeout_seconds": 0, "retries": -1,
                       "retry_backoff_seconds": "x", "timeout_second": 3},
        "ammeters": {
            "a": {"port": True, "command": "X"},
            "b": {"port": 70000, "command": ""},
            "c": {"port": 6000, "command": "C"},
            "d": {"port": 6000, "command": "D", "prot": 1},
            "e": "not a mapping",
        },
    })
    assert "(11 problem(s))" in text
    for expected in ("connection.host", "timeout_second", "ammeters.a.port", "70000",
                     "already used by 'c'", "'prot'", "ammeters.e"):
        assert expected in text


def test_unknown_top_level_key_is_an_error():
    assert "unknown key(s) ['scenario']" in problems({**MINIMAL, "scenario": {}})


def test_bonus_sections_are_validated(config):
    config["simulation"]["profiles"]["x"] = {"timeout_probability": 1.5, "colour": 1}
    config["simulation"]["profiles"]["y"] = {"timeout_probability": 0.6,
                                             "disconnect_probability": 0.6}
    config["scenarios"]["s"] = {"ammeters": ["fluke"], "simulation": "nope", "plots": "yes"}
    config["analysis"]["confidence_level"] = 0.8
    config["analysis"]["visualization"]["plot_types"] = ["pie"]
    config["ammeters"]["entes"]["reference_current"] = 0
    text = problems(config)
    for expected in ("between 0 and 1", "colour", "add up to 1.2", "'fluke'", "'nope'",
                     "plots", "confidence_level", "pie", "reference_current"):
        assert expected in text


def test_timing_and_bias_tolerances_come_from_the_config(config):
    settings = load_settings(config)
    assert settings.sampling.late_tolerance == 0.5
    assert settings.sampling.max_late_fraction == 0.1
    assert settings.analysis.bias_tolerance == 0.05
    # A scenario with its own sampling still gets the timing rules.
    assert settings.scenarios["precision_study"].sampling.max_late_fraction == 0.1


def test_bad_tolerances_are_rejected(config):
    config["testing"]["sampling"]["late_tolerance"] = 0
    config["testing"]["sampling"]["max_late_fraction"] = 1.5
    config["analysis"]["bias_tolerance"] = -0.1
    text = problems(config)
    for expected in ("late_tolerance", "max_late_fraction", "bias_tolerance"):
        assert expected in text


def test_scenario_defaults(config):
    settings = load_settings(config)
    flaky = settings.scenarios["flaky_network"]
    # No ammeters list means all of them.
    assert flaky.ammeters == ("greenlee", "entes", "circutor")
    assert flaky.sampling.count == 60 and flaky.simulation == "flaky_network"
    # No sampling means testing.sampling is used.
    assert settings.scenarios["slow_device"].sampling is None


def test_fault_profile_defaults_are_off():
    profile = FaultProfile()
    assert profile.failure_probability == 0 and profile.bias_relative == 0


# Sampling plan: any two of count, duration and frequency

@pytest.mark.parametrize(
    "given, expected",
    [
        ({"duration_s": 5, "frequency_hz": 10}, (50, 10)),
        ({"count": 20, "duration_s": 4}, (20, 5)),
        ({"count": 30, "duration_s": 3, "frequency_hz": 10}, (30, 10)),
        # 0.29 * 100 is 28.999999999999996 in floats, and it should still be 29.
        ({"duration_s": 0.29, "frequency_hz": 100}, (29, 100)),
        # 7.5 is rounded down, with a warning.
        ({"duration_s": 2.5, "frequency_hz": 3}, (7, 3)),
    ],
)
def test_the_missing_sampling_value_is_derived(given, expected):
    plan = make_sampling_plan(**given)
    assert (plan.count, plan.frequency_hz) == expected


@pytest.mark.parametrize(
    "given, message",
    [
        ({"count": 100, "duration_s": 5, "frequency_hz": 10}, "must equal"),
        ({"count": 10}, "at least two"),
        ({"duration_s": 0.05, "frequency_hz": 10}, "less than one measurement"),
        ({"count": 0, "frequency_hz": -1}, "measurements_count"),
    ],
)
def test_invalid_sampling_values_are_rejected(given, message):
    with pytest.raises(ConfigError, match=message):
        make_sampling_plan(**given)


def test_unknown_sampling_key_is_rejected():
    config = {**MINIMAL, "testing": {"sampling": {"measurements_count": 5, "freq": 2}}}
    assert "'freq'" in problems(config)
