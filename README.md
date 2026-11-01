# Ammeter Emulators and Testing Framework

[![tests](https://github.com/Nomi-Malek/ammeter-testing-framework/actions/workflows/tests.yml/badge.svg)](https://github.com/Nomi-Malek/ammeter-testing-framework/actions/workflows/tests.yml)

This project provides emulators for different types of ammeters: Greenlee, ENTES, and CIRCUTOR. Each ammeter emulator runs on a separate thread and can respond to current measurement requests.

On top of the emulators, it provides a testing framework that samples the ammeters at a precise rate, analyses the readings, stores every result with a unique ID, and compares the ammeters' precision and reliability. Design decisions and fixes are explained in [DOCUMENTATION.md](DOCUMENTATION.md).

**Where to start reading:** run `python main.py`, then look at `src/testing/test_framework.py` (the entry point), `src/ammeter_client.py` (the unified API), `src/sampling.py` and `src/analysis.py`. The rest are the bonus parts: `accuracy.py`, `simulation.py`, `visualization.py`, and `settings.py` for the config checks.

## Project Structure

- `main.py`: Main script to start the ammeter emulators and run a test (all ammeters, or a named scenario).
- `Ammeters/`
  - `Circutor_Ammeter.py`: Emulator for the CIRCUTOR ammeter.
  - `Entes_Ammeter.py`: Emulator for the ENTES ammeter.
  - `Greenlee_Ammeter.py`: Emulator for the Greenlee ammeter.
  - `base_ammeter.py`: Base class for all ammeter emulators.
- `config/`
  - `config.yaml`: Configuration file: ammeters, connection, sampling, analysis, fault simulation and test scenarios.
- `examples/`
  - `run_tests.py`: super lazy example for run test **don't use it**. (Left as it was given. A script of your own runs from the project folder with `python -m examples.your_script`; see "From Python" below.)
- `src/`
  - `testing/`
    - `test_framework.py`: `AmmeterTestFramework`, the class to test the ammeters.
  - `ammeter_client.py`: Unified API: one TCP client for every ammeter, with timeouts and retries.
  - `sampling.py`: Sampling at a fixed rate, by count, duration and frequency.
  - `analysis.py`: Statistics: mean, median, standard deviation, min, max, CV, SEM.
  - `results.py`, `storage.py`: Test results with a unique ID and metadata, stored as JSON files.
  - `accuracy.py`: Precision, reliability and bias of each ammeter.
  - `visualization.py`: Charts (PNG).
  - `simulation.py`: Fault injection (timeouts, invalid replies, noise, bias...).
  - `emulators.py`: Starts each emulator in its own process, like a separate device.
  - `settings.py`, `exceptions.py`: Validated configuration and the framework's errors.
  - `cli.py`: Command line to list, show, compare, assess and plot stored results.
  - `utils/`
    - `config.py`: Configuration loading.
    - `logger.py`: Logging setup.
    - `Utils.py`: Utility functions, including `generate_random_float`.
- `tests/`: Automated tests (pytest).
- `sample_results/`: Sample test results: stored results, charts and the output of three runs.
- `Exam/`: The assignment.
- `results/`: Created when running: results (`runs/`), charts (`plots/`) and logs (`logs/`). Not in the repository.

## Installation

Python 3.10 or newer, on Windows, Linux or macOS.

```sh
python -m venv .venv
.venv\Scripts\activate                # Windows
source .venv/bin/activate             # Linux / macOS
pip install -r requirements.txt       # to run the framework
pip install -r requirements-dev.txt   # also needed to run the tests
```

The only dependencies are `pyyaml` and `matplotlib` (charts), plus `pytest` for the tests.

## Usage

To start the ammeter emulators and request current measurements, run the `main.py` script:

```sh
python main.py
```

It samples all three ammeters in parallel (by default 20 readings at 10 Hz, set in `config/config.yaml`), then prints a statistics report per ammeter and an accuracy assessment, saves each result to `results/runs/` and a chart to `results/plots/`. The exit code is 0 only if every reading succeeded, the timing was met, and every result was saved. The timing is met when no more than 10% of the readings start more than half an interval late (`late_tolerance` and `max_late_fraction` in the config); otherwise the report says "TIMING NOT MET" with the numbers.

The reports go to standard output and the log messages to standard error (and to `results/logs/`), so `python main.py > report.txt` saves just the reports.

### Scenarios

Named tests are defined in `config/config.yaml`, under `scenarios`: which ammeters, how to sample, and which faults to simulate.

```sh
python main.py --list-scenarios
python main.py --scenario precision_study    # 200 readings per ammeter
python main.py --scenario flaky_network      # 15% of readings fail (simulated)
python main.py --scenario biased_meter       # ENTES reads 20% high (simulated)
```

Results of simulated runs are marked as simulated in the report, the stored file and the result list. `--no-plots` skips the chart; `--config FILE` uses another config file.

### Stored results

```sh
python -m src.cli list                       # newest first; --ammeter, --status, --batch, --limit
python -m src.cli show RESULT_ID             # full report of one result
python -m src.cli compare RESULT_ID RESULT_ID
python -m src.cli accuracy                   # precision and reliability of the latest batch
python -m src.cli plot                       # chart of the latest batch
```

### From Python

```python
from src.emulators import start_emulators, wait_for_ammeters
from src.testing.test_framework import AmmeterTestFramework

# Reads config/config.yaml.
framework = AmmeterTestFramework()

# The framework tests ammeters, it doesn't start them, so start the emulators
# first. Each one runs in its own process, like a separate device.
emulators = start_emulators(framework.ammeters)
try:
    wait_for_ammeters(framework.ammeters)

    # 50 readings at the config's frequency. Anything not given comes from the config.
    result = framework.run_test("entes", count=50)
    print(result.report())

    # All the ammeters at the same time, then compare them.
    results = list(framework.run_all().values())
    print(framework.accuracy_report(results))
finally:
    emulators.stop()
```

Run a script like this from the project folder, for example `python -m examples.my_test`.

### Tests

```sh
python -m pytest                   # all tests, under a minute
python -m pytest -m "not slow"     # skip the statistical checks
```

The tests start their own emulators on free ports and write only to temporary folders. GitHub Actions runs them on Windows, Linux and macOS with Python 3.10 to 3.13 (`.github/workflows/tests.yml`).

### Sample output

Accuracy assessment from `sample_results/precision_study.txt` (200 readings per ammeter):

```
#  ammeter              ok  mean (A)    median     cv        95% CI of mean    +/-  skew read cv late max     bias
1  circutor        200/200   0.03161   0.03034  49.0%    [0.02945, 0.03377]   6.8%  0.22   85.0%   28.3ms    +4.5%
2  entes           200/200     72.72     65.32  57.3%        [66.92, 78.53]   8.0%  0.58   82.8%   26.9ms    +5.8%
3  greenlee        200/200    0.4146    0.1121 403.7%      [0.1812, 0.6480]  56.3%  9.06   82.6%   25.4ms    +9.0%
Most reliable: circutor, because its mean is more precise (+/-6.8% vs +/-8.0%).
```

The three ammeters measure different currents, so their means are not compared; precision is compared relative to each mean. Bias is measured against `reference_current` in the config. Greenlee has a few very large readings (resistance close to 0.1 Ω), so its mean is far from its median and the report warns about skew.

How "most reliable" is decided: two ammeters are compared step by step. First a real bias beyond `bias_tolerance` (5%) rules an ammeter out, then the success rate (skipped when the failures were simulated, since every ammeter got the same chance of failing), then the precision of the mean. A difference that could be chance is a tie, and the next step decides. An ammeter is named only if it's clearly better than every other one; otherwise the report says "No clear winner" (see `sample_results/default.txt`, 20 readings each). See `sample_results/` for the full output of three runs, their stored results and charts. To browse them:

```sh
python -m src.cli --config sample_results/config.yaml list
```

# Ammeter Emulators

## Greenlee Ammeter

- **Port**: 5000
- **Command**: `MEASURE_GREENLEE -get_measurement`
- **Measurement Logic**: Calculates current using voltage (1V - 10V) and resistance (0.1Ω - 100Ω).
- **Measurement method** : Ohm's Law: I = V / R

## ENTES Ammeter

- **Port**: 5001
- **Command**: `MEASURE_ENTES -get_data`
- **Measurement Logic**: Calculates current using magnetic field strength (0.01T - 0.1T) and calibration factor (500 - 2000).
- **Measurement method** : Hall Effect: I = B * K

## CIRCUTOR Ammeter

- **Port**: 5002
- **Command**: `MEASURE_CIRCUTOR -get_measurement -current`
- **Measurement Logic**: Calculates current using voltage values (0.1V - 1.0V) over a number of samples and a random time step (0.001s - 0.01s).
- **Measurement method** : Rogowski Coil Integration: I = ∫V dt
