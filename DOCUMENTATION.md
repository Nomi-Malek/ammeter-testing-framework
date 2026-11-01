# Documentation

A testing framework for the three ammeter emulators: a unified API, sampling at a precise rate, statistics, saved results, and the bonuses (accuracy assessment, charts, fault simulation, config-driven scenarios). Usage is in [README.md](README.md). This file covers what was fixed in the given code, what was installed, and why the framework is built the way it is.

## 1. Fixes to the given code

| File | Problem | Fix |
| --- | --- | --- |
| `main.py` | The request lines were commented out, the commands were incomplete (each emulator only answers its exact command), and the ports were 5001–5003 instead of 5000–5002. | Fixed, and later rewritten on top of the framework: ports and commands come from `config.yaml`, each emulator runs in its own process, and a readiness check replaces the fixed `sleep(5)`. |
| `src/utils/config.py` | Two versions of the code pasted into one file: `SyntaxError` on import. | One function that raises a clear `ConfigError` for every failure. |
| `src/utils/logger.py` | No handler and no level, so nothing was ever logged; creating it twice would duplicate every line. | `setup_logging()`: console and a log file, safe to call more than once. |
| `src/testing/test_framework.py` | Used `Dict` without importing it (`NameError`). | Fixed, then implemented as the framework's entry point. |
| `config/config.yaml` | The `ammeters` section was commented out; the CIRCUTOR command was missing `-current`. | Restored with the commands the emulators expect. |
| `Ammeters/base_ammeter.py` | One client that disconnected early or never sent anything crashed or blocked the emulator; the port couldn't be reused right after a run (Linux/macOS); every reading printed 2–3 lines. | A 1 s timeout per client (shorter than the client's 2 s); a bad client no longer stops the server; quick restart (`SO_REUSEADDR` on Linux/macOS, `SO_EXCLUSIVEADDRUSE` on Windows); a taken port gives a clear error; prints moved to a logger at debug level. The measurement formulas are unchanged. |
| `Ammeters/client.py` | A second, older client: no timeout, no return value, no error handling. | Deleted; everything uses `src/ammeter_client.py`. |
| Packages | No `__init__.py` files. | Added. |
| `README.md` | Listed `main.py` under `Ammeters/`, a non-existent `AmmeterTester.py`, `run_test.py` instead of `run_tests.py`, and the CIRCUTOR command without `-current`. | Corrected in place; the original sections and ammeter descriptions are kept, and usage instructions were added. |

Kept as given on purpose: the original file and folder names (`Ammeters/`, `Greenlee_Ammeter.py`, `Utils.py`, ...), so code written against the given layout still works; and `examples/run_tests.py`, which the original README marks "don't use it". It still calls `run_test()` without an ammeter name, so it fails if run on its own; nothing in the project uses it.

## 2. Installed libraries

| Library | File | Why |
| --- | --- | --- |
| `pyyaml` | `requirements.txt` | Reading `config.yaml`. |
| `matplotlib` | `requirements.txt` | Charts. Only imported when a chart is drawn. |
| `pytest` | `requirements-dev.txt` | The tests. |

`numpy`, `scipy`, `pandas` and `seaborn` were removed from the requirements: the `statistics` module covers the analysis, and the spec asks for minimal dependencies. Python 3.10 or newer.

If `pip` fails with `CERTIFICATE_VERIFY_FAILED` (an antivirus that inspects HTTPS, such as Avast), use `pip install --use-feature=truststore -r requirements.txt`.

## 3. Design decisions

**Unified API.** All three ammeters speak the same protocol: connect, send a command, read a number until the connection closes. So there is one class, `TcpAmmeterClient`, and a new ammeter is just an entry in `config.yaml`. The rest of the framework depends on a small `Ammeter` interface (`name`, `measure()`), not on TCP, so tests and the fault simulation can use fakes in its place.

**Errors.** One exception hierarchy under `AmmeterFrameworkError` (config, connection, timeout, invalid response, storage, visualization), always raised with the original cause attached (`raise ... from e`). Network errors are retried with a growing wait; an invalid reply is not, since asking again would give the same reply. Each exchange has one overall deadline, so a server that sends one byte at a time can't hang the client.

**Config.** `config.yaml` is checked once at start-up, and every problem is reported together, including unknown keys, so a typo like `timeout_second` isn't silently ignored.

**Sampling.** Any two of count, duration and frequency (the third is calculated; three that don't match are an error). Reading *i* is scheduled at `start + i × interval`, so a slow reading doesn't push the later ones back; a reading that starts late is taken at once and counted as an overrun, never as a catch-up burst. A failed reading is recorded and sampling goes on; after too many failures in a row the run stops. Ctrl+C keeps what was collected. All ammeters are sampled in parallel, one thread each. The clock is a parameter, so the tests use a fake one.

**Timing check.** The spec asks for precise timing, so a run is checked against its rate. A reading is *late* when it starts more than half an interval after its time (`late_tolerance: 0.5`, so 50 ms at 10 Hz); if more than 10% of the readings are late (`max_late_fraction: 0.1`), the report says "TIMING NOT MET" with the numbers and `main.py` exits with code 1. Both values are in `config.yaml` and saved with each result. Runs with simulated faults don't fail on timing, since slow readings are what they simulate. The tolerance is relative to the interval because a fixed number of milliseconds would be strict at 1 Hz and meaningless at 50 Hz. Note that the "overruns" count is different: it counts every reading that started even slightly late.

**Emulators in their own processes.** `main.py` starts each emulator as a separate process (`src/emulators.py`), because a real ammeter is a separate device and not part of the program that tests it. Each log line is passed back to the main process as one line of JSON (level, logger name, message, and the traceback if there is one), so the lines reach the same console and log file, a "port taken" error shows up there, and a traceback stays one record. Each emulator exits when its input from the main process closes: when `main.py` finishes, on Ctrl+C, or even if `main.py` is killed. Measured with `precision_study` (200 readings at 50 Hz), 1–4.5% of the readings were late with processes, and about the same with threads, so on this machine the separate processes didn't change the timing; `precision_study` meets 50 Hz either way. The tests still use threads, which is faster.

**Analysis.** The standard `statistics` module: mean, median, sample standard deviation (n − 1), min, max, range, CV and standard error of the mean. Values that can't be calculated (e.g. std of one reading) are `None`, never an error. The same statistics on reading time and lateness measure how steady the timing is. Nothing is filtered out, and rounding happens only in the report.

**Results.** One JSON file per result, with a readable unique ID (`20261101T114023Z_entes_f0f366fb`: time, ammeter, random part), metadata (times, ammeter, connection settings, Python and OS, but not the computer or user name), and a `schema_version`. Files are written to a temporary file and then renamed, so a crash never leaves half a file. When loading, the statistics are calculated again from the readings. If saving fails, the result is still returned and reported with a clear warning. `python -m src.cli` lists, shows, compares, assesses and plots saved results.

**Output.** Reports go to standard output; log messages go to standard error and to `results/logs/`.

## 4. Bonuses

**Accuracy assessment.** The emulators don't measure one real circuit: each one draws random values from its own formula, so their means are different by design (about 0.38 A, 68.75 A and 0.030 A). Accuracy, meaning closeness to the true current, can only be measured against a known value, so `config.yaml` has an optional `reference_current` (for the emulators, the expected value of their formula), and the bias is reported against it. Precision needs no reference: a confidence interval for the mean (t distribution; the t values come from a table and a formula, without scipy) and its width relative to the mean, which can be compared across ammeters.

**Most reliable ammeter.** Two ammeters are compared step by step:
1. A real bias rules an ammeter out: the reference is outside its interval *and* the bias is bigger than `bias_tolerance` (5% in the config). Without the tolerance, with enough readings even a 1% bias would count. A meter that always answers but reads 26% high is less reliable than one that sometimes fails but is right.
2. Then the success rate, except when the failures were simulated: every ammeter got the same chance of failing, so a difference there is just luck.
3. Then the precision of the mean.

A difference that could be chance counts as a tie, and the next step decides. An ammeter is named "most reliable" only when it's clearly better than every other one, with the step that decided it ("because its mean is more precise (+/-6.8% vs +/-8.0%)"); otherwise the report says "No clear winner". With 20 readings that's the usual answer, and it's the honest one.

The t interval assumes a roughly normal sample mean. Greenlee's formula (voltage / resistance, resistance down to 0.1 Ω) has a long tail, and simulating its formula 2,000 times showed how often a 95% interval really contains the true mean:

| Emulator | 20 readings | 200 readings |
| --- | --- | --- |
| Greenlee | 55% | 78% |
| ENTES | 94% | 94% |
| CIRCUTOR | 94% | 95% |

So when the readings are strongly skewed, the report warns, and a reference outside the interval is marked `?` ("may be a sampling effect") instead of `*`.

**Charts.** One PNG per run: readings over time (failed readings marked) and a histogram per ammeter, the spread of each ammeter relative to its own mean, and reading time and lateness. Drawn with matplotlib's `Figure` directly, so no window opens and it works on a server.

**Fault simulation.** `FaultInjectingAmmeter` wraps any ammeter and adds timeouts, dropped connections, invalid replies, slow readings, spikes, noise or a constant bias, raising the same exceptions as the real client. A seed makes the faults repeatable. Results of simulated runs are marked as simulated in the file and in every report.

**Scenarios.** Named tests in `config.yaml` (ammeters, sampling, faults), run with `python main.py --scenario NAME`.

## 5. Testing

153 pytest tests (`python -m pytest`, under a minute): config checks, the client against servers that send broken replies, sampling timing on a fake clock (including a run that only reaches 5 Hz of the planned 10 Hz), statistics, saving and loading (including broken and older files), the accuracy assessment (including a biased meter that never fails), the simulation, the charts, the emulator processes (logs as JSON, a traceback as one record, a taken port, stopping), and end to end against the real emulators. The tests start their own emulators on free ports and write only to temporary folders. The CI runs only the short `quick_check` scenario as a demo: a high-rate scenario would sometimes miss its timing on a busy CI machine for no real reason.

Checks done before submitting: a clean virtual environment with only the requirements files runs the tests, `main.py` (also from another folder) and the CLI; the code imports exactly the listed libraries and nothing unused; and four bugs were planted one at a time (population instead of sample std, a catch-up burst, overwriting a result, retrying an invalid reply), and the tests caught all four. Development was on Windows with Python 3.12; `.github/workflows/tests.yml` runs the tests on Windows, Linux and macOS with Python 3.10–3.13. The first CI run found a problem that didn't show up on the development machine: on the GitHub Windows and macOS machines, `localhost` resolves to IPv6 (`::1`) first, while the emulators listen only on IPv4, so every reading waited about 2 seconds for the IPv6 attempt to fail and then timed out. The client now connects to `127.0.0.1` (the `host` in `config.yaml`).

## 6. Known limitations

- For Greenlee, the confidence interval is unreliable even with 200 readings (see the table above); the report warns, but can't fix it.
- At 95%, about 1 run in 20 marks a bias that isn't there, and a difference in precision can also look real by chance.
- Injected spikes (the `noisy_spikes` scenario) make the readings skewed, so a mean pushed off by them gets the label "may be a sampling effect, not a bias" instead of being flagged. From one sample, a long natural tail (like Greenlee's) and injected spikes look the same.
- On GitHub's virtual Mac machines a sleep returns 35–80 ms late, so a real run there can end with "TIMING NOT MET" even at 10 Hz. That's the timing check reporting correctly, not a bug; the CI demo on Mac is allowed to fail for this reason, and the tests that check the whole chain don't judge the machine's clock (the timing check itself is tested on a fake clock).
- The ranking compares the emulators' random formulas, not real hardware.
- Simulated network faults are raised above the client, so they don't go through its retries.
- `ResultStore.list()` reads every file each time: fine for hundreds of results, not for many thousands.
- The emulators read the command with a single `recv()` (original code), so they assume it arrives in one piece.

## 7. Sample results

`sample_results/` holds three real runs, made with `sample_results/config.yaml` (the project config, writing to `sample_results/`): the default run (20 readings each, which ends in "No clear winner"), `precision_study` (200 readings each at 50 Hz, timing met) and `flaky_network` (simulated faults, seed 42). Each has its report (`.txt`), its saved results (`runs/`) and its chart (`plots/`). Browse them with `python -m src.cli --config sample_results/config.yaml list`.
