"""End-to-end tests with the real emulators: the framework, main.py and the CLI.

Results and charts go to a temporary folder.
"""
import copy
import dataclasses

import pytest
import yaml

import main as main_module
from src.cli import main as cli_main
from src.exceptions import ConfigError
from src.storage import ResultStore
from src.testing.test_framework import AmmeterTestFramework
from src.utils.config import load_config
from tests.conftest import FakeAmmeter, FakeClock, free_port, result_of


@pytest.fixture
def framework(config_file):
    return AmmeterTestFramework(config_file)


@pytest.fixture
def main_config_file(tmp_path):
    """A config with new free ports, so main.py starts its own emulator processes."""
    config = copy.deepcopy(load_config())
    for entry in config["ammeters"].values():
        entry["port"] = free_port()
    config["result_management"]["directory"] = str(tmp_path / "runs")
    config["analysis"]["visualization"]["directory"] = str(tmp_path / "plots")
    # These tests check that the whole chain works, not how exact the clock of
    # the machine running them is (GitHub's Mac machines wake up 35-80 ms late).
    # The timing check itself is tested on a fake clock in test_sampling.py.
    config["testing"]["sampling"]["max_late_fraction"] = 1.0
    path = tmp_path / "main_config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def run_main(main_config_file, monkeypatch):
    # Don't create a log file in the project, pytest handles the logging.
    monkeypatch.setattr(main_module, "setup_logging", lambda: None)
    return lambda *args: main_module.main(["--config", str(main_config_file), *args])


def test_exit_code_fails_a_run_that_missed_its_timing(tmp_path):
    on_time = dataclasses.replace(result_of(FakeAmmeter(FakeClock()), count=20),
                                  saved_path=tmp_path)
    # Each reading takes 0.2 s at 10 Hz: 5 Hz in practice.
    too_slow = dataclasses.replace(result_of(FakeAmmeter(FakeClock(), cost=0.2), count=20),
                                   saved_path=tmp_path)
    assert main_module.exit_code([on_time], all_ready=True, simulated=False) == 0
    assert main_module.exit_code([on_time, too_slow], all_ready=True, simulated=False) == 1
    # With simulated faults, missed timing is expected and doesn't fail the run.
    assert main_module.exit_code([on_time, too_slow], all_ready=True, simulated=True) == 0


def test_the_report_says_when_timing_was_not_met():
    too_slow = result_of(FakeAmmeter(FakeClock(), cost=0.2), count=20)
    report = too_slow.report()
    assert "TIMING NOT MET" in report and "19 of 20" in report
    on_time = result_of(FakeAmmeter(FakeClock()), count=20)
    assert "TIMING NOT MET" not in on_time.report()


def test_run_all_saves_one_batch(framework):
    results = framework.run_all(count=5, frequency_hz=20)
    assert set(results) == {"greenlee", "entes", "circutor"}
    assert len({r.batch_id for r in results.values()}) == 1
    assert all(r.saved_path and r.run.successful_count == 5 for r in results.values())
    assert len(framework.store.list(batch_id=results["entes"].batch_id)) == 3


def test_run_test_with_overrides_and_without_saving(framework):
    result = framework.run_test("entes", count=5, duration_s=0.5, save=False)
    assert result.run.plan.frequency_hz == 10 and result.saved_path is None
    assert "turned off" in result.report()


@pytest.mark.parametrize(
    "override, expected",
    [
        # The config says 20 readings at 10 Hz.
        ({"count": 5}, (5, 10)),
        ({"frequency_hz": 20}, (20, 20)),
        ({"duration_s": 0.5}, (5, 10)),
    ],
)
def test_one_sampling_value_takes_the_rest_from_the_config(framework, override, expected):
    result = framework.run_test("entes", save=False, **override)
    plan = result.run.plan
    assert (plan.count, plan.frequency_hz) == expected


def test_unknown_names_are_config_errors(framework):
    with pytest.raises(ConfigError, match="fluke"):
        framework.run_test("fluke")
    with pytest.raises(ConfigError, match="nope"):
        framework.run_scenario("nope")
    with pytest.raises(ConfigError, match="profile"):
        framework.run_test("entes", count=2, frequency_hz=10, fault_profile="nope")


def test_save_failure_keeps_the_result_and_warns(framework, tmp_path):
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    framework.store = ResultStore(blocker / "runs")
    result = framework.run_test("entes", count=3, frequency_hz=10)
    assert result.saved_path is None and result.save_error
    assert "WARNING: NOT SAVED" in result.report()


def test_main_default_run(run_main, config_file, tmp_path, capsys):
    assert run_main() == 0
    assert len(ResultStore(tmp_path / "runs").list()) == 3
    assert len(list((tmp_path / "plots").glob("*.png"))) == 1
    # The reports go to stdout, so `python main.py > out.txt` keeps them.
    out = capsys.readouterr().out
    assert out.count("current (A)") == 3 and "Accuracy assessment" in out


def test_main_lists_scenarios(run_main, capsys):
    assert run_main("--list-scenarios") == 0
    assert "flaky_network" in capsys.readouterr().out


def test_main_unknown_scenario_fails(run_main):
    assert run_main("--scenario", "nope") == 1


def test_flaky_network_scenario_is_marked_as_simulated(run_main, tmp_path):
    assert run_main("--scenario", "flaky_network", "--no-plots") == 0
    results = ResultStore(tmp_path / "runs").list()
    assert len(results) == 3 and not list(tmp_path.glob("plots/*.png"))
    assert all(r.metadata["scenario"] == "flaky_network" for r in results)
    assert all(r.metadata["simulation"]["seed"] == 42 for r in results)
    assert sum(r.run.failed_count for r in results) > 0
    assert "SIMULATED FAULTS" in results[0].report()


def test_slow_device_scenario_causes_overruns(run_main, tmp_path):
    assert run_main("--scenario", "slow_device", "--no-plots") == 0
    [result] = ResultStore(tmp_path / "runs").list()
    assert result.run.overruns > 0


def test_biased_meter_scenario_bias_is_found(framework):
    results = framework.run_scenario("biased_meter", save=False)
    [assessment] = framework.assess(list(results.values()))
    assert assessment.bias_significant and 0.05 < assessment.bias < 0.4


def test_cli_on_stored_results(run_main, main_config_file, capsys, tmp_path):
    assert run_main("--no-plots") == 0
    capsys.readouterr()
    def cli(*args):
        return cli_main(["--config", str(main_config_file), *args])

    assert cli("list") == 0
    listing = capsys.readouterr().out
    first_id = listing.splitlines()[1].split()[0]
    assert cli("show", first_id) == 0 and first_id in capsys.readouterr().out
    assert cli("accuracy") == 0 and "95% CI of mean" in capsys.readouterr().out
    assert cli("plot") == 0 and "Chart saved" in capsys.readouterr().out
    assert len(list((tmp_path / "plots").glob("*.png"))) == 1
    assert cli("show", "20200101T000000Z_x_00000000") == 1
