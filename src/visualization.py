"""Charts for the results, saved as one PNG per batch.

For each ammeter there's a readings-over-time chart and a histogram. Below
them, the ammeters are compared to each other, and there's a timing chart.

matplotlib is only imported when we actually draw, so the rest works without
it. We use Figure directly instead of pyplot, so no window opens and nothing
stays open after saving. That also makes it work on a server.
"""
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from src.exceptions import VisualizationError
from src.results import TestResult
from src.settings import PLOT_TYPES

# One color per ammeter, always in the same order, so an ammeter has the same
# color in every chart. The first three are easy to tell apart even with
# color blindness.
SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6")
# Red is only for failed readings.
FAILURE_COLOR = "#d03b3b"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#fcfcfb"

_PER_AMMETER = ("timeseries", "histogram")
_ACROSS = ("comparison", "timing")


def color_map(names: Sequence[str]) -> dict[str, str]:
    """Gives each ammeter a color, in config order."""
    return {name: SERIES_COLORS[i % len(SERIES_COLORS)] for i, name in enumerate(names)}


def plot_results(
    results: Sequence[TestResult],
    path: Path,
    *,
    plot_types: Sequence[str] = PLOT_TYPES,
    colors: dict[str, str] | None = None,
    title: str | None = None,
) -> Path:
    """Draws all the charts for these results into one PNG file.

    The folder is created if it's missing. Like the results, the image goes to
    a temporary file first, so a failed save doesn't leave half an image.

    Raises:
        VisualizationError: No matplotlib, nothing to draw, or the file can't be written.
    """
    if not results:
        raise VisualizationError("No results to plot")
    unknown = [p for p in plot_types if p not in PLOT_TYPES]
    if unknown:
        raise VisualizationError(f"Unknown plot type(s) {unknown}; available: {list(PLOT_TYPES)}")
    figure_class = _figure_class()
    colors = colors or color_map([r.ammeter for r in results])

    per_ammeter = [p for p in _PER_AMMETER if p in plot_types]
    across = [p for p in _ACROSS if p in plot_types and len(results) > 1]
    # Comparing needs two ammeters, but timing is useful for one too.
    if len(results) == 1 and "timing" in plot_types:
        across.append("timing")
    rows = (len(results) if per_ammeter else 0) + (1 if across else 0)
    columns = max(len(per_ammeter), len(across), 1)
    if rows == 0:
        raise VisualizationError(f"Nothing to draw with plot types {list(plot_types)}")

    figure = figure_class(figsize=(6.2 * columns, 3.1 * rows + 0.7), facecolor=SURFACE)
    grid = figure.add_gridspec(rows, columns)
    row = 0
    if per_ammeter:
        for result in results:
            color = colors.get(result.ammeter, SERIES_COLORS[0])
            for column, kind in enumerate(per_ammeter):
                axes = _axes(figure, grid[row, column])
                if kind == "timeseries":
                    _draw_timeseries(axes, result, color)
                else:
                    _draw_histogram(axes, result, color)
            row += 1
    for column, kind in enumerate(across):
        span = grid[row, column] if len(across) > 1 or columns == 1 else grid[row, :]
        axes = _axes(figure, span)
        if kind == "comparison":
            _draw_comparison(axes, results, colors)
        else:
            _draw_timing(axes, results, colors)

    figure.suptitle(title or _default_title(results), color=INK, fontsize=12, x=0.01, ha="left")
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    return _save(figure, Path(path))


def _figure_class() -> Any:
    try:
        from matplotlib.figure import Figure
    except ImportError as e:
        raise VisualizationError(
            "matplotlib is not installed, so charts cannot be drawn. "
            "Install it with: pip install -r requirements.txt"
        ) from e
    return Figure


def _axes(figure: Any, spec: Any) -> Any:
    axes = figure.add_subplot(spec)
    axes.set_facecolor(SURFACE)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(AXIS)
    axes.tick_params(colors=INK_SECONDARY, labelsize=8)
    axes.grid(True, color=GRID, linewidth=0.6)
    axes.set_axisbelow(True)
    return axes


def _label(axes: Any, title: str, xlabel: str, ylabel: str) -> None:
    axes.set_title(title, color=INK, fontsize=10, loc="left")
    axes.set_xlabel(xlabel, color=INK_SECONDARY, fontsize=8)
    axes.set_ylabel(ylabel, color=INK_SECONDARY, fontsize=8)


def _draw_timeseries(axes: Any, result: TestResult, color: str) -> None:
    run = result.run
    ok = [s for s in run.samples if s.ok]
    failed = [s for s in run.samples if not s.ok]
    axes.plot(
        [s.started_s for s in ok], [s.value for s in ok],
        color=color, linewidth=1.5, marker="o", markersize=3.5, label="reading",
    )
    mean = result.analysis.current.mean
    if mean is not None:
        axes.axhline(mean, color=INK_SECONDARY, linewidth=1, linestyle="--", label=f"mean {mean:.4g}")
    if failed:
        # Failed readings have no value, so mark them with an x at the bottom.
        axes.plot(
            [s.started_s for s in failed], [0.02] * len(failed),
            linestyle="none", marker="x", markersize=8, markeredgewidth=2,
            color=FAILURE_COLOR, transform=axes.get_xaxis_transform(),
            label=f"failed ({len(failed)})", clip_on=False,
        )
    status = run.status.value
    _label(
        axes,
        f"{run.ammeter}: readings over time ({run.successful_count}/{run.plan.count} ok, {status})",
        "time since start (s)",
        "current (A)",
    )
    axes.legend(fontsize=7, frameon=False, loc="upper right")


def _draw_histogram(axes: Any, result: TestResult, color: str) -> None:
    values = result.run.values
    stats = result.analysis.current
    if not values:
        axes.text(0.5, 0.5, "no successful readings", ha="center", va="center",
                  color=INK_SECONDARY, transform=axes.transAxes)
    else:
        bins = max(5, min(30, round(math.sqrt(len(values)) * 2)))
        axes.hist(values, bins=bins, color=color, edgecolor=SURFACE, linewidth=1)
        axes.axvline(stats.mean, color=INK, linewidth=1.5, label=f"mean {stats.mean:.4g}")
        axes.axvline(stats.median, color=INK, linewidth=1.5, linestyle=":",
                     label=f"median {stats.median:.4g}")
        axes.legend(fontsize=7, frameon=False, loc="upper right")
    _label(axes, f"{result.ammeter}: distribution", "current (A)", "readings")


def _draw_comparison(axes: Any, results: Sequence[TestResult], colors: dict[str, str]) -> None:
    data, labels, fills = [], [], []
    for result in results:
        mean = result.analysis.current.mean
        # Without a mean (or with a mean of 0) there's nothing to divide by.
        if mean:
            data.append([v / mean for v in result.run.values])
            labels.append(result.ammeter)
            fills.append(colors.get(result.ammeter, SERIES_COLORS[0]))
    if not data:
        axes.text(0.5, 0.5, "no data to compare", ha="center", va="center",
                  color=INK_SECONDARY, transform=axes.transAxes)
    else:
        boxes = axes.boxplot(data, patch_artist=True, widths=0.5,
                             medianprops={"color": INK, "linewidth": 1.5},
                             flierprops={"markersize": 4, "markeredgecolor": INK_SECONDARY})
        for patch, fill in zip(boxes["boxes"], fills):
            patch.set_facecolor(fill)
            patch.set_edgecolor(SURFACE)
        axes.set_xticks(range(1, len(labels) + 1), labels)
        axes.axhline(1, color=INK_SECONDARY, linewidth=1, linestyle="--")
    _label(axes, "Spread compared: each reading / its ammeter's mean",
           "", "reading / mean (1 = mean)")


def _draw_timing(axes: Any, results: Sequence[TestResult], colors: dict[str, str]) -> None:
    # Two boxes per ammeter: reading time (filled) and lateness (striped).
    # The color tells the ammeter, the stripes tell which measure it is.
    positions, data, fills, hatches = [], [], [], []
    for index, result in enumerate(results):
        samples = result.run.samples
        if not samples:
            continue
        base = index * 3 + 1
        fill = colors.get(result.ammeter, SERIES_COLORS[0])
        positions += [base, base + 1]
        data += [[s.duration_s * 1000 for s in samples], [s.lateness_s * 1000 for s in samples]]
        fills += [fill, fill]
        hatches += ["", "////"]
    if not data:
        axes.text(0.5, 0.5, "no readings", ha="center", va="center",
                  color=INK_SECONDARY, transform=axes.transAxes)
    else:
        boxes = axes.boxplot(data, positions=positions, patch_artist=True, widths=0.8,
                             medianprops={"color": INK, "linewidth": 1.5},
                             flierprops={"markersize": 4, "markeredgecolor": INK_SECONDARY})
        for patch, fill, hatch in zip(boxes["boxes"], fills, hatches):
            patch.set_facecolor(fill if not hatch else SURFACE)
            patch.set_edgecolor(fill)
            patch.set_hatch(hatch)
        axes.set_xticks([i * 3 + 1.5 for i in range(len(results))], [r.ammeter for r in results])
        from matplotlib.patches import Patch

        axes.legend(
            handles=[
                Patch(facecolor=INK_SECONDARY, label="reading time"),
                Patch(facecolor=SURFACE, edgecolor=INK_SECONDARY, hatch="////", label="lateness"),
            ],
            fontsize=7, frameon=False, loc="upper right",
        )
    _label(axes, "Timing consistency", "", "milliseconds")


def _default_title(results: Sequence[TestResult]) -> str:
    first = results[0]
    if len(results) == 1:
        return f"Result {first.id}"
    batch = first.batch_id if all(r.batch_id == first.batch_id for r in results) else None
    names = ", ".join(r.ammeter for r in results)
    return f"Batch {batch}: {names}" if batch else f"Results: {names}"


def _save(figure: Any, path: Path) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as file:
                figure.savefig(file, format="png", dpi=110, facecolor=SURFACE)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
    except OSError as e:
        raise VisualizationError(f"Cannot save the chart to {path}: {e}") from e
    return path
