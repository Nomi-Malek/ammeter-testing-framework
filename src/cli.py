"""Command line for looking at saved results.

    python -m src.cli list [--ammeter NAME] [--status STATUS] [--batch BATCH_ID] [--limit N]
    python -m src.cli show RESULT_ID
    python -m src.cli compare RESULT_ID RESULT_ID [RESULT_ID ...]
    python -m src.cli accuracy [--batch BATCH_ID]
    python -m src.cli plot [--batch BATCH_ID | RESULT_ID ...] [--output FILE.png]

It only reads saved results, so no ammeter has to be running. The only
command that writes a file is plot.
"""
import argparse
import logging
import sys
from pathlib import Path

from src.accuracy import accuracy_report, assess
from src.exceptions import AmmeterFrameworkError, ConfigError, ResultStorageError
from src.results import TestResult, compare_results
from src.sampling import RunStatus
from src.settings import Settings, load_settings
from src.storage import ResultStore
from src.utils.config import load_config
from src.utils.paths import display_path
from src.visualization import color_map, plot_results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="config file (default: config/config.yaml)")
    commands = parser.add_subparsers(dest="command", required=True)

    list_cmd = commands.add_parser("list", help="list stored results, newest first")
    list_cmd.add_argument("--ammeter", help="only this ammeter")
    list_cmd.add_argument("--status", choices=[s.value for s in RunStatus], help="only this status")
    list_cmd.add_argument("--batch", help="only results of this batch ID")
    list_cmd.add_argument("--limit", type=int, default=20, help="at most N results (default 20)")

    show_cmd = commands.add_parser("show", help="show the full report of one result")
    show_cmd.add_argument("result_id")

    compare_cmd = commands.add_parser("compare", help="compare results; the first is the baseline")
    compare_cmd.add_argument("result_ids", nargs="+", metavar="result_id")

    accuracy_cmd = commands.add_parser(
        "accuracy", help="precision, reliability and bias of a batch (default: the latest batch)"
    )
    accuracy_cmd.add_argument("--batch", help="batch ID")

    plot_cmd = commands.add_parser(
        "plot", help="save a chart of a batch (default: the latest batch) or of chosen results"
    )
    plot_cmd.add_argument("result_ids", nargs="*", metavar="result_id")
    plot_cmd.add_argument("--batch", help="batch ID")
    plot_cmd.add_argument("--output", help="PNG file (default: <plots directory>/<batch or result ID>.png)")
    return parser


def _batch_results(
    store: ResultStore, batch_id: str | None, order: list[str]
) -> list[TestResult]:
    """The results of one batch, in config order. Without a batch ID, the latest batch."""
    if batch_id is None:
        latest = next((r for r in store.list() if r.batch_id), None)
        if latest is None:
            raise ResultStorageError(f"No batch results found in {store.directory}")
        batch_id = latest.batch_id
    results = store.list(batch_id=batch_id)
    if not results:
        raise ResultStorageError(f"No results with batch ID {batch_id!r}")
    rank = {name: i for i, name in enumerate(order)}
    return sorted(results, key=lambda r: (rank.get(r.ammeter, len(rank)), r.ammeter))


def list_results(args: argparse.Namespace, settings: Settings, store: ResultStore) -> None:
    results = store.list(ammeter=args.ammeter, status=args.status, batch_id=args.batch)
    if not results:
        print(f"No results found in {store.directory}")
        return
    shown = results[: max(args.limit, 0)]
    print(f"{'id':<44}{'status':<12}{'ok':>7}{'mean (A)':>12}  batch")
    for r in shown:
        ok = f"{r.run.successful_count}/{r.run.plan.count}"
        mean = r.analysis.current.mean
        mean_text = "n/a" if mean is None else f"{mean:.5g}"
        simulated = "  [simulated]" if "simulation" in r.metadata else ""
        print(f"{r.id:<44}{r.run.status.value:<12}{ok:>7}{mean_text:>12}  {r.batch_id or '-'}{simulated}")
    if len(results) > len(shown):
        print(f"... {len(results) - len(shown)} more (use --limit)")


def show_result(args: argparse.Namespace, settings: Settings, store: ResultStore) -> None:
    print(store.load(args.result_id).report())


def compare(args: argparse.Namespace, settings: Settings, store: ResultStore) -> None:
    print(compare_results([store.load(rid) for rid in args.result_ids]))


def show_accuracy(args: argparse.Namespace, settings: Settings, store: ResultStore) -> None:
    results = _batch_results(store, args.batch, list(settings.ammeters))
    level = settings.analysis.confidence_level
    tolerance = settings.analysis.bias_tolerance
    references = {name: a.reference_current for name, a in settings.ammeters.items()}
    assessments = [assess(r, references.get(r.ammeter), level, tolerance) for r in results]
    print(f"Batch {results[0].batch_id}")
    print(accuracy_report(assessments, level))


def plot(args: argparse.Namespace, settings: Settings, store: ResultStore) -> None:
    if args.result_ids and args.batch:
        raise ConfigError("plot: give result IDs or --batch, not both")
    if args.result_ids:
        results = [store.load(rid) for rid in args.result_ids]
        name = results[0].id
    else:
        results = _batch_results(store, args.batch, list(settings.ammeters))
        name = results[0].batch_id
    output = args.output or settings.visualization.directory / f"{name}.png"
    path = plot_results(
        results,
        Path(output),
        plot_types=settings.visualization.plot_types,
        colors=color_map(list(settings.ammeters)),
    )
    print(f"Chart saved: {display_path(path)}")


COMMANDS = {
    "list": list_results,
    "show": show_result,
    "compare": compare,
    "accuracy": show_accuracy,
    "plot": plot,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Warnings (like a skipped broken file) go to the screen. No log file here.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        settings = load_settings(load_config(args.config))
        store = ResultStore(settings.results_dir)
        COMMANDS[args.command](args, settings, store)
    except AmmeterFrameworkError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
