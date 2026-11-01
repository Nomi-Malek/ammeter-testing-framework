"""Reads the config dict into checked, typed settings.

load_settings() checks the whole file and reports all the problems together,
so they can be fixed in one go. After that the code only uses these
dataclasses, not the raw dict.
"""
import logging
import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from src.exceptions import ConfigError
from src.utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectionSettings:
    """How we connect to the ammeters. Same for all of them."""

    # IPv4 on purpose: "localhost" can resolve to IPv6 first on Windows and Mac,
    # and the emulators only listen on IPv4.
    host: str = "127.0.0.1"
    timeout_seconds: float = 2.0
    retries: int = 3
    retry_backoff_seconds: float = 0.1


@dataclass(frozen=True)
class AmmeterSettings:
    """What's different per ammeter: its port and its command.

    reference_current is the true current, if we know it. Only the accuracy
    check uses it, to see if the ammeter reads too high or too low.
    """

    name: str
    port: int
    command: str
    reference_current: float | None = None


@dataclass(frozen=True)
class SamplingPlan:
    """How many readings to take, and how often.

    The duration is just count / frequency_hz, since every reading gets one
    interval.
    """

    count: int
    frequency_hz: float
    max_consecutive_failures: int = 5
    # A reading is late when it starts more than this part of the interval
    # after its time (0.5 is half an interval).
    late_tolerance: float = 0.5
    # The run doesn't meet its timing when more than this share of readings
    # is late (0.1 is 10%).
    max_late_fraction: float = 0.1

    @property
    def interval_s(self) -> float:
        return 1.0 / self.frequency_hz

    @property
    def duration_s(self) -> float:
        return self.count / self.frequency_hz


DEFAULT_SAMPLING_PLAN = SamplingPlan(count=20, frequency_hz=10.0)
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "results" / "runs"
DEFAULT_PLOTS_DIR = PROJECT_ROOT / "results" / "plots"

# Only these levels, because accuracy.py has a t table for each of them.
CONFIDENCE_LEVELS = (0.90, 0.95, 0.99)
PLOT_TYPES = ("timeseries", "histogram", "comparison", "timing")


@dataclass(frozen=True)
class AnalysisSettings:
    confidence_level: float = 0.95
    # A bias counts against an ammeter only above this (0.05 is 5%).
    bias_tolerance: float = 0.05


@dataclass(frozen=True)
class VisualizationSettings:
    enabled: bool = True
    plot_types: tuple[str, ...] = PLOT_TYPES
    directory: Path = DEFAULT_PLOTS_DIR


@dataclass(frozen=True)
class FaultProfile:
    """Which faults to add to the readings (see simulation.py). All off by default.

    The three failure chances are picked with one random number, so together
    they can't be more than 1.
    """

    # The reading fails with a timeout, a dropped connection or a bad reply.
    timeout_probability: float = 0.0
    disconnect_probability: float = 0.0
    invalid_response_probability: float = 0.0
    # The reading works, but arrives late.
    slow_probability: float = 0.0
    slow_delay_seconds: float = 0.2
    # The value is multiplied by spike_factor.
    spike_probability: float = 0.0
    spike_factor: float = 10.0
    # Random noise, as a fraction of the value (0.05 is 5%).
    noise_relative: float = 0.0
    # A constant error: 0.05 makes every value 5% too high.
    bias_relative: float = 0.0

    @property
    def failure_probability(self) -> float:
        return (
            self.timeout_probability
            + self.disconnect_probability
            + self.invalid_response_probability
        )


@dataclass(frozen=True)
class SimulationSettings:
    # With no seed we pick a random one and save it in each result.
    seed: int | None = None
    profiles: dict[str, FaultProfile] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    """A named test from the config: which ammeters, how to sample, which faults.

    None in sampling or plots means "use the general setting". simulation is
    the name of a fault profile, or None for no faults.
    """

    name: str
    description: str
    ammeters: tuple[str, ...]
    sampling: SamplingPlan | None
    simulation: str | None
    plots: bool | None


@dataclass(frozen=True)
class Settings:
    """All the settings, after checking."""

    connection: ConnectionSettings
    ammeters: dict[str, AmmeterSettings]
    sampling: SamplingPlan = DEFAULT_SAMPLING_PLAN
    results_dir: Path = DEFAULT_RESULTS_DIR
    analysis: AnalysisSettings = AnalysisSettings()
    visualization: VisualizationSettings = VisualizationSettings()
    simulation: SimulationSettings = SimulationSettings()
    scenarios: dict[str, Scenario] = field(default_factory=dict)


_TOP_LEVEL_KEYS = {
    "testing",
    "connection",
    "ammeters",
    "analysis",
    "result_management",
    "simulation",
    "scenarios",
}
_CONNECTION_KEYS = {"host", "timeout_seconds", "retries", "retry_backoff_seconds"}
_AMMETER_KEYS = {"port", "command", "reference_current"}
_SAMPLING_KEYS = {
    "measurements_count",
    "total_duration_seconds",
    "sampling_frequency_hz",
    "max_consecutive_failures",
    "late_tolerance",
    "max_late_fraction",
}
_RESULT_MANAGEMENT_KEYS = {"directory"}
_ANALYSIS_KEYS = {"confidence_level", "bias_tolerance", "visualization"}
_VISUALIZATION_KEYS = {"enabled", "plot_types", "directory"}
_SIMULATION_KEYS = {"seed", "profiles"}
_FAULT_PROFILE_KEYS = {f.name for f in fields(FaultProfile)}
_SCENARIO_KEYS = {"description", "ammeters", "sampling", "simulation", "plots"}
_PROBABILITY_FIELDS = (
    "timeout_probability",
    "disconnect_probability",
    "invalid_response_probability",
    "slow_probability",
    "spike_probability",
)


def load_settings(config: dict[str, Any]) -> Settings:
    """Checks the config dict and returns the settings.

    Only the ammeters section is required, the rest have defaults. An unknown
    key is an error at any level, so a typo doesn't just get ignored.

    Raises:
        ConfigError: With all the problems found, one per line.
    """
    errors: list[str] = []
    if not isinstance(config, dict):
        raise ConfigError(f"config: must be a mapping, got {type(config).__name__}")
    _check_unknown_keys(config, _TOP_LEVEL_KEYS, "config", errors)
    connection = _parse_connection(config.get("connection"), errors)
    ammeters = _parse_ammeters(config.get("ammeters"), errors)
    sampling = _parse_sampling(config.get("testing"), errors)
    results_dir = _parse_result_management(config.get("result_management"), errors)
    analysis, visualization = _parse_analysis(config.get("analysis"), errors)
    simulation = _parse_simulation(config.get("simulation"), errors)
    scenarios = _parse_scenarios(
        config.get("scenarios"), ammeters, simulation, sampling, errors
    )
    _raise_if_errors(errors, "config")
    return Settings(
        connection=connection,
        ammeters=ammeters,
        sampling=sampling,
        results_dir=results_dir,
        analysis=analysis,
        visualization=visualization,
        simulation=simulation,
        scenarios=scenarios,
    )


def make_sampling_plan(
    count: int | None = None,
    duration_s: float | None = None,
    frequency_hz: float | None = None,
    max_consecutive_failures: int = DEFAULT_SAMPLING_PLAN.max_consecutive_failures,
    late_tolerance: float = DEFAULT_SAMPLING_PLAN.late_tolerance,
    max_late_fraction: float = DEFAULT_SAMPLING_PLAN.max_late_fraction,
) -> SamplingPlan:
    """Builds a sampling plan from any two of count, duration and frequency.

    The third one is calculated. If all three are given they have to match
    (count = duration * frequency). If duration * frequency isn't a whole
    number, the count is rounded down with a warning.

    Raises:
        ConfigError: A bad value, fewer than two values, or three that don't match.
    """
    errors: list[str] = []
    rules = {
        "max_consecutive_failures": max_consecutive_failures,
        "late_tolerance": late_tolerance,
        "max_late_fraction": max_late_fraction,
    }
    plan = _build_sampling_plan(count, duration_s, frequency_hz, rules, "sampling", errors)
    _raise_if_errors(errors, "sampling parameters")
    return plan


def _raise_if_errors(errors: list[str], what: str) -> None:
    if errors:
        details = "\n  - ".join(errors)
        raise ConfigError(f"Invalid {what} ({len(errors)} problem(s)):\n  - {details}")


def _is_int(value: Any) -> bool:
    # In Python bool is a kind of int, so without this `port: true` would pass.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _check_unknown_keys(
    section: dict, allowed: set[str], where: str, errors: list[str]
) -> None:
    # Catches typos like `timeout_second`. Otherwise the key is ignored and
    # the default is used without anyone noticing.
    unknown = sorted(str(key) for key in section if key not in allowed)
    if unknown:
        errors.append(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _parse_connection(section: Any, errors: list[str]) -> ConnectionSettings:
    defaults = ConnectionSettings()
    if section is None:
        return defaults
    if not isinstance(section, dict):
        errors.append(f"connection: must be a mapping, got {type(section).__name__}")
        return defaults

    _check_unknown_keys(section, _CONNECTION_KEYS, "connection", errors)
    count_before = len(errors)

    host = section.get("host", defaults.host)
    if not isinstance(host, str) or not host.strip():
        errors.append(f"connection.host: must be a non-empty string, got {host!r}")

    timeout = section.get("timeout_seconds", defaults.timeout_seconds)
    if not _is_number(timeout) or timeout <= 0:
        errors.append(f"connection.timeout_seconds: must be a number > 0, got {timeout!r}")

    retries = section.get("retries", defaults.retries)
    if not _is_int(retries) or retries < 0:
        errors.append(f"connection.retries: must be an integer >= 0, got {retries!r}")

    backoff = section.get("retry_backoff_seconds", defaults.retry_backoff_seconds)
    if not _is_number(backoff) or backoff < 0:
        errors.append(
            f"connection.retry_backoff_seconds: must be a number >= 0, got {backoff!r}"
        )

    # With errors this value is never used, load_settings raises anyway.
    if len(errors) > count_before:
        return defaults
    return ConnectionSettings(
        host=host.strip(),
        timeout_seconds=float(timeout),
        retries=retries,
        retry_backoff_seconds=float(backoff),
    )


def _parse_ammeters(section: Any, errors: list[str]) -> dict[str, AmmeterSettings]:
    if not isinstance(section, dict) or not section:
        errors.append(
            "ammeters: must be a non-empty mapping of name -> {port, command}, "
            f"got {section!r}"
        )
        return {}

    ammeters: dict[str, AmmeterSettings] = {}
    names_by_port: dict[int, str] = {}

    for name, entry in section.items():
        where = f"ammeters.{name}"
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}: ammeter name must be a non-empty string")
            continue
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a mapping with port and command, got {entry!r}")
            continue

        count_before = len(errors)
        _check_unknown_keys(entry, _AMMETER_KEYS, where, errors)

        port = entry.get("port")
        if not _is_int(port) or not 1 <= port <= 65535:
            errors.append(f"{where}.port: must be an integer 1-65535, got {port!r}")
        elif port in names_by_port:
            errors.append(f"{where}.port: {port} is already used by '{names_by_port[port]}'")
        else:
            names_by_port[port] = name

        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            errors.append(f"{where}.command: must be a non-empty string, got {command!r}")

        reference = entry.get("reference_current")
        if reference is not None and (not _is_number(reference) or reference == 0):
            # Not 0, because the bias is a percentage of this value.
            errors.append(
                f"{where}.reference_current: must be a non-zero number, got {reference!r}"
            )

        if len(errors) == count_before:
            # Keep the command exactly as written, the ammeter compares it byte by byte.
            ammeters[name] = AmmeterSettings(
                name=name,
                port=port,
                command=command,
                reference_current=None if reference is None else float(reference),
            )

    return ammeters


def _section(value: Any, where: str, allowed: set[str], errors: list[str]) -> dict | None:
    """An optional section. Returns None if it's missing or not a mapping (that's reported)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(f"{where}: must be a mapping, got {type(value).__name__}")
        return None
    _check_unknown_keys(value, allowed, where, errors)
    return value


def _parse_analysis(
    section: Any, errors: list[str]
) -> tuple[AnalysisSettings, VisualizationSettings]:
    section = _section(section, "analysis", _ANALYSIS_KEYS, errors)
    if section is None:
        return AnalysisSettings(), VisualizationSettings()

    level = section.get("confidence_level", AnalysisSettings.confidence_level)
    if level not in CONFIDENCE_LEVELS or isinstance(level, bool):
        errors.append(
            f"analysis.confidence_level: must be one of {list(CONFIDENCE_LEVELS)}, got {level!r}"
        )
        level = AnalysisSettings.confidence_level

    tolerance = section.get("bias_tolerance", AnalysisSettings.bias_tolerance)
    if not _is_number(tolerance) or tolerance < 0:
        errors.append(f"analysis.bias_tolerance: must be a number >= 0, got {tolerance!r}")
        tolerance = AnalysisSettings.bias_tolerance
    analysis = AnalysisSettings(float(level), float(tolerance))

    defaults = VisualizationSettings()
    vis = _section(
        section.get("visualization"), "analysis.visualization", _VISUALIZATION_KEYS, errors
    )
    if vis is None:
        return analysis, defaults

    enabled = vis.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        errors.append(f"analysis.visualization.enabled: must be true or false, got {enabled!r}")
        enabled = defaults.enabled

    plot_types = vis.get("plot_types")
    # An empty key means all the plot types.
    if plot_types is None:
        plot_types = defaults.plot_types
    elif (
        not isinstance(plot_types, list)
        or not plot_types
        or any(p not in PLOT_TYPES for p in plot_types)
    ):
        errors.append(
            f"analysis.visualization.plot_types: must be a non-empty list of {list(PLOT_TYPES)}, "
            f"got {plot_types!r}"
        )
        plot_types = defaults.plot_types

    directory = _parse_directory(
        vis.get("directory"), "analysis.visualization.directory", DEFAULT_PLOTS_DIR, errors
    )
    return analysis, VisualizationSettings(
        enabled=enabled, plot_types=tuple(dict.fromkeys(plot_types)), directory=directory
    )


def _parse_simulation(section: Any, errors: list[str]) -> SimulationSettings:
    section = _section(section, "simulation", _SIMULATION_KEYS, errors)
    if section is None:
        return SimulationSettings()

    seed = section.get("seed")
    if seed is not None and not _is_int(seed):
        errors.append(f"simulation.seed: must be an integer or null, got {seed!r}")
        seed = None

    profiles: dict[str, FaultProfile] = {}
    raw_profiles = section.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        errors.append(f"simulation.profiles: must be a mapping, got {type(raw_profiles).__name__}")
        raw_profiles = {}
    for name, entry in raw_profiles.items():
        profile = _parse_fault_profile(entry, f"simulation.profiles.{name}", errors)
        if profile is not None:
            profiles[str(name)] = profile
    return SimulationSettings(seed=seed, profiles=profiles)


def _parse_fault_profile(entry: Any, where: str, errors: list[str]) -> FaultProfile | None:
    entry = _section(entry if entry is not None else {}, where, _FAULT_PROFILE_KEYS, errors)
    if entry is None:
        return None
    count_before = len(errors)
    for key, value in entry.items():
        # Unknown keys were already reported above.
        if key not in _FAULT_PROFILE_KEYS:
            continue
        if not _is_number(value):
            errors.append(f"{where}.{key}: must be a number, got {value!r}")
        elif key in _PROBABILITY_FIELDS and not 0 <= value <= 1:
            errors.append(f"{where}.{key}: must be between 0 and 1, got {value!r}")
        elif key in ("slow_delay_seconds", "noise_relative") and value < 0:
            errors.append(f"{where}.{key}: must be >= 0, got {value!r}")
        elif key == "bias_relative" and value <= -1:
            errors.append(f"{where}.{key}: must be > -1 (-1 would make every value 0), got {value!r}")
    if len(errors) > count_before:
        return None
    profile = FaultProfile(**{key: float(value) for key, value in entry.items()})
    if profile.failure_probability > 1 + 1e-9:
        errors.append(
            f"{where}: timeout, disconnect and invalid_response probabilities add up to "
            f"{profile.failure_probability:g}; the sum must be at most 1"
        )
        return None
    return profile


def _parse_scenarios(
    section: Any,
    ammeters: dict[str, AmmeterSettings],
    simulation: SimulationSettings,
    default_plan: SamplingPlan,
    errors: list[str],
) -> dict[str, Scenario]:
    if section is None:
        return {}
    if not isinstance(section, dict):
        errors.append(f"scenarios: must be a mapping, got {type(section).__name__}")
        return {}

    scenarios: dict[str, Scenario] = {}
    for name, entry in section.items():
        where = f"scenarios.{name}"
        entry = _section(entry, where, _SCENARIO_KEYS, errors)
        if entry is None:
            if section[name] is None:
                errors.append(f"{where}: is empty")
            continue
        count_before = len(errors)

        description = entry.get("description", "")
        if not isinstance(description, str):
            errors.append(f"{where}.description: must be a string, got {description!r}")

        # No list means all the ammeters.
        names = entry.get("ammeters")
        if names is None:
            names = list(ammeters)
        elif not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            errors.append(f"{where}.ammeters: must be a non-empty list of names, got {names!r}")
            names = []
        unknown = [n for n in names if n not in ammeters]
        # If there are no ammeters at all, that error is reported elsewhere.
        if unknown and ammeters:
            errors.append(
                f"{where}.ammeters: unknown ammeter(s) {unknown}; available: {sorted(ammeters)}"
            )

        plan = None
        sampling = _section(entry.get("sampling"), f"{where}.sampling", _SAMPLING_KEYS, errors)
        if sampling is not None:
            plan = _build_sampling_plan(
                sampling.get("measurements_count"),
                sampling.get("total_duration_seconds"),
                sampling.get("sampling_frequency_hz"),
                _sampling_rules(sampling, default_plan),
                f"{where}.sampling",
                errors,
            )

        profile = entry.get("simulation")
        if profile is not None and (not isinstance(profile, str) or profile not in simulation.profiles):
            errors.append(
                f"{where}.simulation: unknown fault profile {profile!r}; "
                f"available: {sorted(simulation.profiles)}"
            )

        plots = entry.get("plots")
        if plots is not None and not isinstance(plots, bool):
            errors.append(f"{where}.plots: must be true, false or absent, got {plots!r}")

        if len(errors) == count_before:
            scenarios[str(name)] = Scenario(
                name=str(name),
                description=description.strip(),
                ammeters=tuple(dict.fromkeys(names)),
                sampling=plan,
                simulation=profile,
                plots=plots,
            )
    return scenarios


def _parse_directory(value: Any, where: str, default: Path, errors: list[str]) -> Path:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: must be a non-empty string, got {value!r}")
        return default
    path = Path(value.strip())
    # A relative path starts from the project folder, not from wherever we run.
    return path if path.is_absolute() else PROJECT_ROOT / path


def _parse_result_management(section: Any, errors: list[str]) -> Path:
    if section is None:
        return DEFAULT_RESULTS_DIR
    if not isinstance(section, dict):
        errors.append(f"result_management: must be a mapping, got {type(section).__name__}")
        return DEFAULT_RESULTS_DIR
    _check_unknown_keys(section, _RESULT_MANAGEMENT_KEYS, "result_management", errors)
    return _parse_directory(
        section.get("directory"), "result_management.directory", DEFAULT_RESULTS_DIR, errors
    )


def _parse_sampling(testing: Any, errors: list[str]) -> SamplingPlan:
    if testing is None:
        return DEFAULT_SAMPLING_PLAN
    if not isinstance(testing, dict):
        errors.append(f"testing: must be a mapping, got {type(testing).__name__}")
        return DEFAULT_SAMPLING_PLAN
    section = testing.get("sampling")
    if section is None:
        return DEFAULT_SAMPLING_PLAN
    if not isinstance(section, dict):
        errors.append(f"testing.sampling: must be a mapping, got {type(section).__name__}")
        return DEFAULT_SAMPLING_PLAN

    _check_unknown_keys(section, _SAMPLING_KEYS, "testing.sampling", errors)
    plan = _build_sampling_plan(
        section.get("measurements_count"),
        section.get("total_duration_seconds"),
        section.get("sampling_frequency_hz"),
        _sampling_rules(section, DEFAULT_SAMPLING_PLAN),
        "testing.sampling",
        errors,
    )
    # plan is None only when there are errors, and then load_settings raises.
    return plan or DEFAULT_SAMPLING_PLAN


def _sampling_rules(section: dict, defaults: SamplingPlan) -> dict[str, Any]:
    """The rules for a good run (failures, lateness), with defaults for missing ones."""
    return {
        key: section.get(key, getattr(defaults, key))
        for key in ("max_consecutive_failures", "late_tolerance", "max_late_fraction")
    }


def _build_sampling_plan(
    count: Any,
    duration: Any,
    frequency: Any,
    rules: dict[str, Any],
    where: str,
    errors: list[str],
) -> SamplingPlan | None:
    """Checks the values and calculates the missing one. None means "not set"."""
    count_before = len(errors)
    max_failures = rules["max_consecutive_failures"]
    late_tolerance = rules["late_tolerance"]
    max_late_fraction = rules["max_late_fraction"]

    if not _is_number(late_tolerance) or late_tolerance <= 0:
        errors.append(f"{where}.late_tolerance: must be a number > 0, got {late_tolerance!r}")
    if not _is_number(max_late_fraction) or not 0 <= max_late_fraction <= 1:
        errors.append(
            f"{where}.max_late_fraction: must be between 0 and 1, got {max_late_fraction!r}"
        )
    if count is not None and (not _is_int(count) or count < 1):
        errors.append(f"{where}.measurements_count: must be an integer >= 1, got {count!r}")
    if duration is not None and (not _is_number(duration) or duration <= 0):
        errors.append(
            f"{where}.total_duration_seconds: must be a number > 0, got {duration!r}"
        )
    if frequency is not None and (not _is_number(frequency) or frequency <= 0):
        errors.append(
            f"{where}.sampling_frequency_hz: must be a number > 0, got {frequency!r}"
        )
    if not _is_int(max_failures) or max_failures < 1:
        errors.append(
            f"{where}.max_consecutive_failures: must be an integer >= 1, got {max_failures!r}"
        )

    given = [
        key
        for key, value in (
            ("measurements_count", count),
            ("total_duration_seconds", duration),
            ("sampling_frequency_hz", frequency),
        )
        if value is not None
    ]
    if len(given) < 2:
        errors.append(
            f"{where}: set at least two of measurements_count, total_duration_seconds "
            f"and sampling_frequency_hz; got {given or 'none'}"
        )

    if len(errors) > count_before:
        return None

    if frequency is None:
        frequency = count / duration
    elif count is None:
        exact = duration * frequency
        # The small extra handles float noise, like 0.29 * 100 = 28.999999999999996.
        count = math.floor(exact + 1e-9)
        if count < 1:
            errors.append(
                f"{where}: {duration} s at {frequency} Hz is less than one measurement"
            )
            return None
        if not math.isclose(count, exact, abs_tol=1e-9):
            logger.warning(
                "%s: %s s x %s Hz = %g measurements; rounded down to %d",
                where, duration, frequency, exact, count,
            )
    elif duration is not None:
        expected = duration * frequency
        if not math.isclose(count, expected, rel_tol=1e-9, abs_tol=1e-9):
            errors.append(
                f"{where}: measurements_count ({count}) must equal "
                f"total_duration_seconds x sampling_frequency_hz "
                f"({duration} x {frequency} = {expected:g}). Set only two of them, "
                f"or make all three agree"
            )
            return None

    return SamplingPlan(
        count=count,
        frequency_hz=float(frequency),
        max_consecutive_failures=max_failures,
        late_tolerance=float(late_tolerance),
        max_late_fraction=float(max_late_fraction),
    )
