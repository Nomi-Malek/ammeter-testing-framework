"""The result of one test: an ID, metadata, the readings and their statistics."""
import platform
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from src.analysis import MeasurementStats, RunAnalysis, analyze_run
from src.sampling import SamplingRun
from src.settings import AmmeterSettings, ConnectionSettings
from src.utils.paths import display_path

# Version of the saved file format. Bump it when the format changes in a way
# older code can't read (see storage.py).
SCHEMA_VERSION = 1

_COLUMNS = ("mean", "median", "std", "min", "max", "range", "cv", "sem")
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9-]+")


@dataclass(frozen=True)
class TestResult:
    """One test result.

    saved_path and save_error tell whether saving worked. They're only for
    this run of the program and aren't written to the file.
    """

    # The name starts with "Test", so tell pytest this isn't a test class.
    __test__ = False

    id: str
    # The same for all the results that run_all() sampled together.
    batch_id: str | None
    metadata: dict[str, Any]
    run: SamplingRun
    analysis: RunAnalysis
    saved_path: Path | None = None
    save_error: str | None = None

    @property
    def ammeter(self) -> str:
        return self.run.ammeter

    def report(self) -> str:
        """A short report to read. Values are rounded only here, never in the data."""
        run, analysis = self.run, self.analysis
        rate = analysis.success_rate
        header = (
            f"{run.ammeter}: {run.status.value}, "
            f"{run.successful_count}/{run.plan.count} ok"
            + (f" ({rate:.0%})" if rate is not None else "")
            + f", {_fmt(run.achieved_frequency_hz)} Hz achieved"
            f" (planned {run.plan.frequency_hz:g} Hz), {run.overruns} overrun(s)"
        )
        if run.stop_reason:
            header += f". {run.stop_reason}"

        rows = [
            ("current (A)", analysis.current),
            ("reading time (ms)", analysis.reading_time),
            ("lateness (ms)", analysis.lateness),
        ]
        lines = [
            f"Result {self.id}" + (f" (batch {self.batch_id})" if self.batch_id else "")
            + (f", scenario {self.metadata['scenario']}" if self.metadata.get("scenario") else ""),
            header,
            f"  {'':<18}" + "".join(f"{name:>11}" for name in _COLUMNS),
        ]
        for label, stats in rows:
            lines.append(f"  {label:<18}" + "".join(_cell(stats, name) for name in _COLUMNS))
        timing_problem = run.timing_problem()
        if timing_problem:
            lines.append(timing_problem.replace("timing not met", "TIMING NOT MET", 1))
        simulation = self.metadata.get("simulation")
        if isinstance(simulation, dict):
            injected = simulation.get("injected") or {}
            counts = ", ".join(f"{k} {v}" for k, v in injected.items()) or "none"
            lines.append(
                f"SIMULATED FAULTS (profile {simulation.get('profile')!r}, "
                f"seed {simulation.get('seed')}): injected {counts}. Not a real measurement."
            )
        lines.append(self.storage_status())
        return "\n".join(lines)

    def storage_status(self) -> str:
        if self.saved_path is not None:
            return f"Saved: {display_path(self.saved_path)}"
        if self.save_error is not None:
            return (
                f"WARNING: NOT SAVED ({self.save_error}). "
                "This result exists only in this run."
            )
        return "Not saved (saving was turned off)."


def new_batch_id(now: datetime | None = None) -> str:
    """One ID for all the results of a single run_all() call."""
    now = now or datetime.now(timezone.utc)
    return f"{now:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def new_result_id(ammeter: str, started_at: datetime) -> str:
    """Start time (UTC), ammeter name and 8 random characters.

    For example 20261031T214600Z_greenlee_3f9a1c2e. It sorts by time and is
    easy to read. The time alone isn't enough, since run_all() starts all the
    ammeters in the same second.
    """
    safe_name = _UNSAFE_ID_CHARS.sub("-", ammeter).strip("-") or "ammeter"
    return f"{started_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}_{safe_name}_{uuid.uuid4().hex[:8]}"


def build_metadata(
    run: SamplingRun, ammeter: AmmeterSettings, connection: ConnectionSettings
) -> dict[str, Any]:
    """How, when and where the result was made.

    The computer name and user name are left out on purpose, because sample
    results go into a public repository.
    """
    return {
        "started_at": run.started_at.isoformat(),
        "finished_at": (run.started_at + timedelta(seconds=run.elapsed_s)).isoformat(),
        "ammeter": {
            "name": ammeter.name,
            "host": connection.host,
            "port": ammeter.port,
            "command": ammeter.command,
        },
        "connection": {
            "timeout_seconds": connection.timeout_seconds,
            "retries": connection.retries,
            "retry_backoff_seconds": connection.retry_backoff_seconds,
        },
        "environment": {
            "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}",
        },
    }


def make_result(
    run: SamplingRun,
    ammeter: AmmeterSettings,
    connection: ConnectionSettings,
    batch_id: str | None = None,
) -> TestResult:
    return TestResult(
        id=new_result_id(run.ammeter, run.started_at),
        batch_id=batch_id,
        metadata=build_metadata(run, ammeter, connection),
        run=run,
        analysis=analyze_run(run),
    )


def compare_results(results: Sequence[TestResult]) -> str:
    """A table of results side by side. The first one is the baseline, and the
    last column shows how far each mean is from it.
    """
    if not results:
        return "No results to compare."
    baseline = results[0].analysis.current.mean
    header = (
        f"{'id':<44}{'status':<12}{'ok':>7}{'mean':>12}{'median':>12}"
        f"{'std':>12}{'cv':>9}{'mean vs 1st':>14}"
    )
    lines = [header]
    for index, result in enumerate(results):
        stats = result.analysis.current
        if index == 0:
            delta = "(baseline)"
        # No percentage when there's no mean, or when the baseline is 0.
        elif stats.mean is None or not baseline:
            delta = "n/a"
        else:
            delta = f"{(stats.mean - baseline) / abs(baseline):+.1%}"
        cv = f"{stats.cv:.1%}" if stats.cv is not None else "n/a"
        ok = f"{result.run.successful_count}/{result.run.plan.count}"
        lines.append(
            f"{result.id:<44}{result.run.status.value:<12}{ok:>7}"
            f"{_fmt(stats.mean):>12}{_fmt(stats.median):>12}{_fmt(stats.std):>12}"
            f"{cv:>9}{delta:>14}"
        )
    if len({r.ammeter for r in results}) > 1:
        lines.append(
            "Note: these results come from different ammeters, which measure different "
            "currents. Their means are not comparable; compare the CV (relative spread) instead."
        )
    return "\n".join(lines)


def _cell(stats: MeasurementStats, name: str) -> str:
    value = getattr(stats, name)
    if name == "cv" and value is not None:
        return f"{value:>10.1%} "
    return f"{_fmt(value):>11}"


def _fmt(value: float | None) -> str:
    # 5 significant digits work for both 0.0462 A and 112.37 A. The # keeps
    # trailing zeros, so every number shows the same precision.
    return "n/a" if value is None else f"{value:#.5g}"
