"""Saves results as JSON files, one file per result.

A result is first written to a temporary file and only then renamed to
<id>.json, so a crash can't leave a half-written file behind.

When loading, a missing required field gives a clear error, while missing
optional fields get defaults so older files still load. The statistics are
always calculated again from the samples, so the samples are what counts.
"""
import contextlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis import analyze_run
from src.exceptions import ResultStorageError
from src.results import SCHEMA_VERSION, TestResult
from src.sampling import RunStatus, Sample, SamplingRun
from src.settings import SamplingPlan

logger = logging.getLogger(__name__)

# The ID becomes the file name, so only safe characters are allowed. That also
# blocks an ID like "../../secret" from reaching files outside the folder.
_VALID_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_REQUIRED = object()


class ResultStore:
    """Saves, loads and lists the results in one folder."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def path_for(self, result_id: str) -> Path:
        if not _VALID_ID.match(result_id):
            raise ResultStorageError(
                f"Invalid result ID {result_id!r}: only letters, digits, '_' and '-' are allowed"
            )
        return self.directory / f"{result_id}.json"

    def save(self, result: TestResult) -> Path:
        """Writes the result to <directory>/<id>.json and returns the path.

        Raises:
            ResultStorageError: The file already exists, or can't be written.
        """
        path = self.path_for(result.id)
        try:
            # nan or inf would make the file invalid JSON, so refuse them here.
            text = json.dumps(to_dict(result), indent=2, allow_nan=False)
        except ValueError as e:
            raise ResultStorageError(f"Result {result.id} cannot be stored as JSON: {e}") from e

        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise ResultStorageError(f"Result file already exists: {path}")
            # The temp file goes in the same folder, because the rename is only
            # safe inside one disk. mkstemp gives it a random name, so two saves
            # at the same time never clash.
            fd, temp_name = tempfile.mkstemp(
                dir=self.directory, prefix=f".{result.id}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write(text)
                    file.flush()
                    # Make sure the data is really on disk before renaming.
                    os.fsync(file.fileno())
                os.replace(temp_name, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name)
                raise
        except OSError as e:
            raise ResultStorageError(f"Cannot save result {result.id} to {path}: {e}") from e

        logger.debug("Saved result %s to %s", result.id, path)
        return path

    def load(self, result_id: str) -> TestResult:
        """Raises ResultStorageError for an unknown ID or a bad file."""
        path = self.path_for(result_id)
        if not path.is_file():
            raise ResultStorageError(f"No result with ID {result_id!r} in {self.directory}")
        return self._load_path(path)

    def list(
        self,
        *,
        ammeter: str | None = None,
        status: str | None = None,
        batch_id: str | None = None,
        since: datetime | None = None,
    ) -> list[TestResult]:
        """All the saved results that match the filters, newest first.

        A broken file is skipped with a warning, so one bad file doesn't hide
        all the others. A `since` without a time zone is taken as UTC, like
        all the saved times.
        """
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if not self.directory.is_dir():
            return []
        results = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                result = self._load_path(path)
            except ResultStorageError as e:
                logger.warning("Skipping unreadable result file: %s", e)
                continue
            if ammeter is not None and result.ammeter != ammeter:
                continue
            if status is not None and result.run.status.value != status:
                continue
            if batch_id is not None and result.batch_id != batch_id:
                continue
            if since is not None and result.run.started_at < since:
                continue
            results.append(result)
        results.sort(key=lambda r: r.run.started_at, reverse=True)
        return results

    def latest(self, ammeter: str) -> TestResult | None:
        results = self.list(ammeter=ammeter)
        return results[0] if results else None

    def _load_path(self, path: Path) -> TestResult:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            raise ResultStorageError(f"Cannot read {path}: {e}") from e
        except json.JSONDecodeError as e:
            raise ResultStorageError(f"Invalid JSON in {path}: {e}") from e
        result = from_dict(data, source=str(path))
        # A copied or renamed file would show up as a second result with the same ID.
        if result.id != path.stem:
            raise ResultStorageError(
                f"ID {result.id!r} inside {path} does not match the file name"
            )
        # We loaded it from this path, so this is where it's saved.
        return TestResult(
            id=result.id, batch_id=result.batch_id, metadata=result.metadata,
            run=result.run, analysis=result.analysis, saved_path=path,
        )


# Converting between TestResult and plain JSON data.

def to_dict(result: TestResult) -> dict[str, Any]:
    run = result.run
    return {
        "schema_version": SCHEMA_VERSION,
        "id": result.id,
        "batch_id": result.batch_id,
        "metadata": result.metadata,
        "run": {
            "ammeter": run.ammeter,
            "started_at": run.started_at.isoformat(),
            "elapsed_s": run.elapsed_s,
            "status": run.status.value,
            "stop_reason": run.stop_reason,
            "overruns": run.overruns,
            "plan": {
                "count": run.plan.count,
                "frequency_hz": run.plan.frequency_hz,
                "max_consecutive_failures": run.plan.max_consecutive_failures,
                "late_tolerance": run.plan.late_tolerance,
                "max_late_fraction": run.plan.max_late_fraction,
            },
            "samples": [asdict(sample) for sample in run.samples],
        },
        # Only here for people reading the file. It's calculated again when loading.
        "analysis": asdict(result.analysis),
    }


def from_dict(data: Any, source: str = "<data>") -> TestResult:
    """Builds a TestResult back from what to_dict() wrote.

    Raises:
        ResultStorageError: A required field is missing, a value has the wrong
            type, or the file is from a newer version than this code knows.
    """
    try:
        return _from_dict(data, source)
    except (TypeError, ValueError) as e:
        raise ResultStorageError(f"Invalid result data in {source}: {e}") from e


def _from_dict(data: Any, source: str) -> TestResult:
    root = _mapping(data, "result", source)
    version = _get(root, "schema_version", source)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ResultStorageError(f"Invalid schema_version {version!r} in {source}")
    if version > SCHEMA_VERSION:
        raise ResultStorageError(
            f"{source} uses result format version {version}, but this code reads up to "
            f"version {SCHEMA_VERSION}. Update the framework to read it."
        )
    # When the format changes, older files get converted here, one version
    # at a time. For example:
    # if version == 1: root = _migrate_v1_to_v2(root); version = 2

    run_data = _mapping(_get(root, "run", source), "run", source)
    plan_data = _mapping(_get(run_data, "plan", source, "run."), "run.plan", source)
    plan = SamplingPlan(
        count=int(_get(plan_data, "count", source, "run.plan.")),
        frequency_hz=float(_get(plan_data, "frequency_hz", source, "run.plan.")),
        max_consecutive_failures=int(_get(plan_data, "max_consecutive_failures", source,
                                          "run.plan.", default=5)),
        late_tolerance=float(_get(plan_data, "late_tolerance", source, "run.plan.", default=0.5)),
        max_late_fraction=float(_get(plan_data, "max_late_fraction", source, "run.plan.",
                                     default=0.1)),
    )
    samples = []
    for i, item in enumerate(_get(run_data, "samples", source, "run.")):
        sample = _mapping(item, f"run.samples[{i}]", source)
        where = f"run.samples[{i}]."
        value = _get(sample, "value", source, where, default=None)
        samples.append(Sample(
            index=int(_get(sample, "index", source, where)),
            scheduled_s=float(_get(sample, "scheduled_s", source, where)),
            started_s=float(_get(sample, "started_s", source, where)),
            duration_s=float(_get(sample, "duration_s", source, where)),
            value=None if value is None else float(value),
            error=_get(sample, "error", source, where, default=None),
        ))
    run = SamplingRun(
        ammeter=str(_get(run_data, "ammeter", source, "run.")),
        plan=plan,
        started_at=datetime.fromisoformat(_get(run_data, "started_at", source, "run.")),
        elapsed_s=float(_get(run_data, "elapsed_s", source, "run.")),
        status=RunStatus(_get(run_data, "status", source, "run.")),
        stop_reason=_get(run_data, "stop_reason", source, "run.", default=None),
        overruns=int(_get(run_data, "overruns", source, "run.", default=0)),
        samples=tuple(samples),
    )
    return TestResult(
        id=str(_get(root, "id", source)),
        batch_id=_get(root, "batch_id", source, default=None),
        metadata=_mapping(_get(root, "metadata", source, default={}), "metadata", source),
        run=run,
        analysis=analyze_run(run),
    )


def _get(data: dict, key: str, source: str, where: str = "", default: Any = _REQUIRED) -> Any:
    """Gets a field from the file.

    Without a default the field is required, and a missing one gives a clear
    error. With a default it's optional, so older files still load.
    """
    if key in data:
        return data[key]
    if default is _REQUIRED:
        raise ResultStorageError(f"Missing field '{where}{key}' in {source}")
    return default


def _mapping(value: Any, name: str, source: str) -> dict:
    if not isinstance(value, dict):
        raise ResultStorageError(f"'{name}' must be an object in {source}, got {type(value).__name__}")
    return value
