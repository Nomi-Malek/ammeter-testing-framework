"""Starts the ammeter emulators and runs a test on them.

    python main.py                       all ammeters, sampling from the config
    python main.py --scenario NAME       a named scenario from the config
    python main.py --list-scenarios
"""
import argparse
import logging
import sys

from src.emulators import start_emulators, wait_for_ammeters
from src.exceptions import AmmeterFrameworkError, VisualizationError
from src.results import TestResult
from src.sampling import RunStatus
from src.settings import Scenario
from src.testing.test_framework import AmmeterTestFramework
from src.utils.logger import setup_logging
from src.utils.paths import display_path

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Start the ammeter emulators, sample them, analyse, save and plot the results.",
    )
    parser.add_argument("--config", help="config file (default: config/config.yaml)")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--scenario", help="run a named scenario from the config")
    choice.add_argument("--list-scenarios", action="store_true", help="list the scenarios and exit")
    parser.add_argument("--no-plots", action="store_true", help="do not draw charts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    try:
        framework = AmmeterTestFramework(args.config)
        scenario = framework.get_scenario(args.scenario) if args.scenario else None
    except AmmeterFrameworkError as e:
        logger.error("%s", e)
        return 1

    if args.list_scenarios:
        print(format_scenarios(framework))
        return 0

    names = list(scenario.ammeters) if scenario else list(framework.ammeters)
    ammeters = {name: framework.ammeters[name] for name in names}
    emulators = start_emulators(ammeters)
    try:
        ready = wait_for_ammeters(ammeters)
        results = run_tests(framework, scenario, ready)
    finally:
        # Also on ctrl+c or an error, so no emulator is left running.
        emulators.stop()

    show_results(framework, results)
    if results and framework.plots_enabled(scenario) and not args.no_plots:
        save_chart(framework, results)

    all_ready = len(ready) == len(ammeters)
    simulated = scenario is not None and scenario.simulation is not None
    return exit_code(results, all_ready, simulated)


def run_tests(
    framework: AmmeterTestFramework, scenario: Scenario | None, ready: list[str]
) -> list[TestResult]:
    if scenario is None:
        return list(framework.run_all(ready).values())
    logger.info("Scenario %s: %s", scenario.name, scenario.description or "(no description)")
    return list(framework.run_scenario(scenario.name, only=ready).values())


def show_results(framework: AmmeterTestFramework, results: list[TestResult]) -> None:
    # The reports are the program's output, so they go to stdout with print.
    # The log (stderr and the log file) is for what happened along the way.
    for result in results:
        print(f"\n{result.report()}")
    if results:
        print(f"\nAccuracy assessment\n{framework.accuracy_report(results)}")

    not_saved = [r.id for r in results if r.saved_path is None]
    if not_saved:
        logger.warning("%d result(s) were NOT saved: %s", len(not_saved), ", ".join(not_saved))


def save_chart(framework: AmmeterTestFramework, results: list[TestResult]) -> None:
    try:
        path = framework.plot(results)
    except VisualizationError as e:
        # The results are already saved, so a missing chart isn't a failed test.
        logger.warning("Chart not created: %s", e)
    else:
        print(f"\nChart saved: {display_path(path)}")


def exit_code(results: list[TestResult], all_ready: bool, simulated: bool) -> int:
    """0 if everything went fine, 1 otherwise."""
    if not all_ready or any(r.saved_path is None for r in results):
        return 1
    if simulated:
        # With simulated faults, failed and late readings are exactly what we
        # expect. Only an interrupted run counts as a failure.
        ok = all(r.run.status is not RunStatus.INTERRUPTED for r in results)
    else:
        # A real run has to finish, without failed readings, and on time.
        ok = all(
            r.run.status is RunStatus.COMPLETED and r.run.failed_count == 0 and r.run.timing_ok
            for r in results
        )
    return 0 if ok else 1


def format_scenarios(framework: AmmeterTestFramework) -> str:
    scenarios = framework.settings.scenarios
    if not scenarios:
        return "No scenarios in the config."
    default_plan = framework.settings.sampling
    lines = []
    for s in scenarios.values():
        plan = s.sampling or default_plan
        faults = f", faults: {s.simulation}" if s.simulation else ""
        lines.append(
            f"{s.name:<18}{s.description}\n{'':<18}ammeters: {', '.join(s.ammeters)}; "
            f"{plan.count} readings at {plan.frequency_hz:g} Hz{faults}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
