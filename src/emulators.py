"""Starts the ammeter emulators, each in its own process.

A real ammeter is a separate device, not part of the program that tests it,
so each emulator runs as a separate program too. (We measured the timing
both ways, and on our machine it was about the same.)

Each emulator process:
- writes every log line to stderr as one line of JSON. The main process reads
  them and logs them as its own, so they reach the same console and log file.
  That's also how a "port taken" error gets to the main process.
- stops by itself when its stdin closes. The main process holds the other
  end, so the emulators stop when it calls stop(), when it ends, or even if
  main.py crashes.

Run one directly with: python -m src.emulators greenlee 5000
"""
import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from Ammeters.base_ammeter import AmmeterEmulatorBase
from src.ammeter_client import TcpAmmeterClient
from src.exceptions import AmmeterFrameworkError
from src.utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Which emulator plays each ammeter from config.yaml.
EMULATORS: dict[str, type[AmmeterEmulatorBase]] = {
    "greenlee": GreenleeAmmeter,
    "entes": EntesAmmeter,
    "circutor": CircutorAmmeter,
}
STARTUP_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 3.0
_LEVELS = {name: getattr(logging, name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")}


@dataclass
class EmulatorProcesses:
    """The running emulator processes. Call stop() when you're done."""

    processes: dict[str, subprocess.Popen] = field(default_factory=dict)

    def stop(self) -> None:
        for process in self.processes.values():
            # Closing stdin tells the emulator to exit by itself.
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        for name, process in self.processes.items():
            try:
                process.wait(timeout=STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                logger.warning("%s emulator didn't stop in time, killing it", name)
                process.kill()
                process.wait()


def start_emulators(ammeters: dict[str, TcpAmmeterClient]) -> EmulatorProcesses:
    """Starts an emulator process for each ammeter, on its port from the config."""
    running = EmulatorProcesses()
    for name, ammeter in ammeters.items():
        if name not in EMULATORS:
            logger.warning("%s: no emulator for this ammeter; expecting a device at %s",
                           name, ammeter.address)
            continue
        process = subprocess.Popen(
            [sys.executable, "-m", "src.emulators", name, str(ammeter.settings.port)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(target=forward_logs, args=(name, process.stderr), daemon=True,
                         name=f"{name}-emulator-log").start()
        running.processes[name] = process
    return running


def wait_for_ammeters(ammeters: dict[str, TcpAmmeterClient]) -> list[str]:
    """Returns the names of the ammeters that came up in time."""
    ready = []
    for name, ammeter in ammeters.items():
        try:
            ammeter.wait_until_ready(STARTUP_TIMEOUT_S)
        except AmmeterFrameworkError as e:
            # Skip this one and still test the others.
            logger.error("%s", e)
        else:
            ready.append(name)
    return ready


class JsonLineFormatter(logging.Formatter):
    """Writes each log record as one line of JSON: level, name, message, traceback.

    JSON keeps a multi-line traceback inside one line, so it arrives in the
    main process as one record and not as many broken lines.
    """

    def format(self, record: logging.LogRecord) -> str:
        data = {"level": record.levelname, "name": record.name, "message": record.getMessage()}
        if record.exc_info:
            data["traceback"] = self.formatException(record.exc_info)
        return json.dumps(data)


def setup_emulator_logging() -> None:
    """Logging inside an emulator process: everything, as JSON lines to stderr."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)


def forward_logs(name: str, lines) -> None:
    """Reads an emulator's log lines and logs each one as a record in this process."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            level = _LEVELS[data["level"]]
            message = data["message"]
        except (ValueError, KeyError, TypeError):
            # Not one of our JSON lines, for example a crash before logging
            # was set up. Show it as an error.
            logging.getLogger(f"Ammeters.{name}").error("%s", line)
            continue
        if data.get("traceback"):
            message = f"{message}\n{data['traceback']}"
        logging.getLogger(data.get("name", "Ammeters")).log(level, "%s", message)


def _run_emulator(name: str, port: int) -> int:
    """The emulator process itself."""
    setup_emulator_logging()

    # Exit as soon as stdin closes, which means the main process is done.
    def exit_when_stdin_closes() -> None:
        sys.stdin.read()
        os._exit(0)

    threading.Thread(target=exit_when_stdin_closes, daemon=True).start()
    try:
        # start_server only returns if it can't listen on the port.
        EMULATORS[name](port).start_server()
    except KeyboardInterrupt:
        # Ctrl+C reaches every process in the console. Just exit quietly.
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_run_emulator(sys.argv[1], int(sys.argv[2])))
