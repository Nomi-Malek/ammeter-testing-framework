"""Tests for the charts."""
import sys
from unittest import mock

import pytest

from src.exceptions import VisualizationError
from src.visualization import plot_results
from tests.conftest import FakeAmmeter, FakeClock, result_of

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def results():
    return [
        result_of(FakeAmmeter(FakeClock(), name="a", cost=0.003, fail_at={3}), count=20),
        result_of(FakeAmmeter(FakeClock(), name="b", cost=0.005), count=20),
        result_of(FakeAmmeter(FakeClock(), name="dead", fail_at=range(100)), count=20,
                  max_failures=3),
    ]


def test_dashboard_is_written_into_a_new_directory(tmp_path, results):
    path = plot_results(results, tmp_path / "new" / "dir" / "batch.png")
    assert path.read_bytes()[:8] == PNG_SIGNATURE
    assert not list(path.parent.glob("*.tmp"))


def test_single_result_and_one_plot_type(tmp_path, results):
    assert plot_results(results[:1], tmp_path / "one.png", plot_types=["timeseries"]).exists()


def test_unwritable_path(tmp_path, results):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(VisualizationError, match="Cannot save"):
        plot_results(results, blocker / "x.png")


def test_nothing_to_draw(tmp_path, results):
    with pytest.raises(VisualizationError, match="No results"):
        plot_results([], tmp_path / "x.png")
    with pytest.raises(VisualizationError, match="Unknown plot type"):
        plot_results(results, tmp_path / "x.png", plot_types=["pie"])


def test_missing_matplotlib_gives_a_clear_error(tmp_path, results):
    with mock.patch.dict(sys.modules, {"matplotlib.figure": None}):
        with pytest.raises(VisualizationError, match="not installed"):
            plot_results(results, tmp_path / "x.png")
