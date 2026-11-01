"""The main class for running tests on the ammeters.

A test samples an ammeter, calculates the statistics and saves the result.
Tests can also come from a named scenario in the config, with or without
simulated faults.
"""
import dataclasses
import logging
import threading
from pathlib import Path
from typing import Any, Sequence

from src.accuracy import AccuracyAssessment, accuracy_report, assess
from src.ammeter_client import Ammeter, TcpAmmeterClient, clients_from_settings
from src.exceptions import ConfigError, ResultStorageError
from src.results import TestResult, make_result, new_batch_id
from src.sampling import SamplingRun, collect_samples, collect_samples_parallel
from src.settings import SamplingPlan, Scenario, load_settings, make_sampling_plan
from src.simulation import FaultInjectingAmmeter, new_seed
from src.storage import ResultStore
from src.utils.config import load_config
from src.visualization import color_map, plot_results

logger = logging.getLogger(__name__)


class AmmeterTestFramework:
    """Reads the config once, then runs tests on the ammeters.

    The ammeters (or their emulators) have to be running already.
    """

    def __init__(self, config_path: str | Path | None = None):
        """Raises ConfigError if the config is missing or wrong."""
        self.config = load_config(config_path)
        self.settings = load_settings(self.config)
        self.ammeters: dict[str, TcpAmmeterClient] = clients_from_settings(self.settings)
        self.store = ResultStore(self.settings.results_dir)

    # Running tests

    def run_test(
        self,
        ammeter_type: str,
        *,
        count: int | None = None,
        duration_s: float | None = None,
        frequency_hz: float | None = None,
        fault_profile: str | None = None,
        save: bool = True,
        stop_event: threading.Event | None = None,
    ) -> TestResult:
        """Samples one ammeter, calculates the statistics and saves the result.

        By default the sampling plan comes from the config. count, duration_s
        or frequency_hz change it for this run only, and whatever isn't given
        comes from the config. fault_profile is the name of a profile from
        the simulation section.

        If saving fails we don't raise. The result comes back with save_error
        set, and its report shows a clear warning.

        Raises:
            ConfigError: Unknown ammeter or fault profile, or bad sampling values.
        """
        self._get_ammeter(ammeter_type)
        plan = self._plan(count, duration_s, frequency_hz)
        wrapped = self._wrap([ammeter_type], fault_profile)
        run = collect_samples(wrapped[ammeter_type], plan, stop_event=stop_event)
        return self._finish(run, wrapped[ammeter_type], None, None, save)

    def run_all(
        self,
        names: list[str] | None = None,
        *,
        count: int | None = None,
        duration_s: float | None = None,
        frequency_hz: float | None = None,
        fault_profile: str | None = None,
        save: bool = True,
        stop_event: threading.Event | None = None,
    ) -> dict[str, TestResult]:
        """Samples several ammeters at the same time (all of them by default).

        The results get the same batch_id, so they can be found together later.

        Raises:
            ConfigError: Unknown ammeter or fault profile, or bad sampling values.
        """
        selected = names if names is not None else list(self.ammeters)
        plan = self._plan(count, duration_s, frequency_hz)
        return self._run_batch(selected, plan, fault_profile, None, save, stop_event)

    def run_scenario(
        self,
        name: str,
        *,
        only: Sequence[str] | None = None,
        save: bool = True,
        stop_event: threading.Event | None = None,
    ) -> dict[str, TestResult]:
        """Runs a named scenario from the config, all its ammeters at once.

        only limits it to some of the ammeters, for example the ones that are up.

        Raises:
            ConfigError: Unknown scenario.
        """
        scenario = self.get_scenario(name)
        names = [n for n in scenario.ammeters if only is None or n in only]
        plan = scenario.sampling or self.settings.sampling
        return self._run_batch(names, plan, scenario.simulation, scenario, save, stop_event)

    def get_scenario(self, name: str) -> Scenario:
        try:
            return self.settings.scenarios[name]
        except KeyError:
            available = ", ".join(sorted(self.settings.scenarios)) or "none defined"
            raise ConfigError(f"Unknown scenario {name!r}. Available: {available}") from None

    # After a run

    def assess(self, results: Sequence[TestResult]) -> list[AccuracyAssessment]:
        """Checks each result against the reference values from the config."""
        analysis = self.settings.analysis
        return [
            assess(r, self._reference(r.ammeter), analysis.confidence_level, analysis.bias_tolerance)
            for r in results
        ]

    def accuracy_report(self, results: Sequence[TestResult]) -> str:
        return accuracy_report(self.assess(results), self.settings.analysis.confidence_level)

    def plot(self, results: Sequence[TestResult], path: Path | None = None) -> Path:
        """Saves the charts for these results and returns the file path.

        Raises:
            VisualizationError: No matplotlib, or the file can't be written.
        """
        if path is None:
            first = results[0] if results else None
            stem = (first.batch_id or first.id) if first else "empty"
            path = self.settings.visualization.directory / f"{stem}.png"
        return plot_results(
            results,
            path,
            plot_types=self.settings.visualization.plot_types,
            colors=color_map(list(self.settings.ammeters)),
        )

    def plots_enabled(self, scenario: Scenario | None = None) -> bool:
        if scenario is not None and scenario.plots is not None:
            return scenario.plots
        return self.settings.visualization.enabled

    # Internal helpers

    def _run_batch(
        self,
        names: Sequence[str],
        plan: SamplingPlan,
        fault_profile: str | None,
        scenario: Scenario | None,
        save: bool,
        stop_event: threading.Event | None,
    ) -> dict[str, TestResult]:
        for name in names:
            self._get_ammeter(name)
        wrapped = self._wrap(names, fault_profile)
        batch_id = new_batch_id()
        runs = collect_samples_parallel(wrapped, plan, stop_event=stop_event)
        return {
            name: self._finish(run, wrapped[name], batch_id, scenario, save)
            for name, run in runs.items()
        }

    def _wrap(self, names: Sequence[str], fault_profile: str | None) -> dict[str, Ammeter]:
        """The ammeters to sample: the plain clients, or wrapped with fault simulation."""
        if fault_profile is None:
            return {name: self.ammeters[name] for name in names}
        simulation = self.settings.simulation
        try:
            profile = simulation.profiles[fault_profile]
        except KeyError:
            available = ", ".join(sorted(simulation.profiles)) or "none defined"
            raise ConfigError(
                f"Unknown fault profile {fault_profile!r}. Available: {available}"
            ) from None
        seed = simulation.seed if simulation.seed is not None else new_seed()
        logger.warning("Simulating faults: profile %r, seed %d", fault_profile, seed)
        return {
            name: FaultInjectingAmmeter(
                self.ammeters[name], profile, seed, profile_name=fault_profile
            )
            for name in names
        }

    def _finish(
        self,
        run: SamplingRun,
        sampled: Ammeter,
        batch_id: str | None,
        scenario: Scenario | None,
        save: bool,
    ) -> TestResult:
        ammeter = self.ammeters[run.ammeter]
        result = make_result(run, ammeter.settings, ammeter.connection, batch_id)
        extra: dict[str, Any] = {}
        if scenario is not None:
            extra["scenario"] = scenario.name
        if isinstance(sampled, FaultInjectingAmmeter):
            # Save this in the file, so nobody mistakes a simulated result for
            # a real measurement later.
            extra["simulation"] = sampled.describe()
        if extra:
            result = dataclasses.replace(result, metadata={**result.metadata, **extra})
        if not save:
            return result
        try:
            path = self.store.save(result)
        except ResultStorageError as e:
            # Still return the result. Losing a long run just because the disk
            # is full would be worse.
            logger.error("Result %s was NOT saved: %s", result.id, e)
            return dataclasses.replace(result, save_error=str(e))
        logger.info("Saved result %s", result.id)
        return dataclasses.replace(result, saved_path=path)

    def _reference(self, name: str) -> float | None:
        ammeter = self.settings.ammeters.get(name)
        return ammeter.reference_current if ammeter else None

    def _get_ammeter(self, name: str) -> TcpAmmeterClient:
        try:
            return self.ammeters[name]
        except KeyError:
            raise ConfigError(
                f"Unknown ammeter {name!r}. Available: {', '.join(sorted(self.ammeters))}"
            ) from None

    def _plan(
        self, count: int | None, duration_s: float | None, frequency_hz: float | None
    ) -> SamplingPlan:
        config = self.settings.sampling
        given = sum(value is not None for value in (count, duration_s, frequency_hz))
        if given == 0:
            return config
        # With just one value, the rest comes from the config: a new count or
        # duration keeps the config's frequency, a new frequency keeps its count.
        if given == 1:
            if frequency_hz is None:
                frequency_hz = config.frequency_hz
            else:
                count = config.count
        # Otherwise the given values replace the config, so they can't clash with it.
        return make_sampling_plan(
            count,
            duration_s,
            frequency_hz,
            config.max_consecutive_failures,
            config.late_tolerance,
            config.max_late_fraction,
        )
