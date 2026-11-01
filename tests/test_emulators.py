"""Tests for running the emulators in their own processes, like real devices."""
import logging
import os
import socket
import subprocess
import sys
import time

from src.emulators import forward_logs, start_emulators, wait_for_ammeters
from src.utils.paths import PROJECT_ROOT
from tests.conftest import emulator_client, free_port


def wait_for_exit(process, seconds=10.0):
    deadline = time.monotonic() + seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return process.poll()


def test_emulators_run_in_their_own_processes():
    ammeters = {"entes": emulator_client("entes", free_port())}
    emulators = start_emulators(ammeters)
    try:
        assert wait_for_ammeters(ammeters) == ["entes"]
        assert ammeters["entes"].measure() > 0
        assert emulators.processes["entes"].pid != os.getpid()
    finally:
        emulators.stop()
    # It stopped by itself once its input was closed (exit code 0), not by being killed.
    assert emulators.processes["entes"].returncode == 0


def test_emulator_logs_reach_the_main_process(caplog):
    caplog.set_level(logging.DEBUG)
    ammeters = {"greenlee": emulator_client("greenlee", free_port())}
    emulators = start_emulators(ammeters)
    try:
        wait_for_ammeters(ammeters)
        ammeters["greenlee"].measure()
        # The logs arrive through a pipe, so give them a moment.
        deadline = time.monotonic() + 5
        while "connected by" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        emulators.stop()
    assert "is running on port" in caplog.text
    assert "connected by" in caplog.text
    assert any(r.name.startswith("Ammeters") and r.levelno == logging.DEBUG
               for r in caplog.records)


def test_an_exception_arrives_as_one_record_with_its_traceback(caplog):
    # A separate Python process that sets up logging like an emulator does,
    # then logs an exception.
    code = (
        "import logging\n"
        "from src.emulators import setup_emulator_logging\n"
        "setup_emulator_logging()\n"
        "try:\n"
        "    1 / 0\n"
        "except ZeroDivisionError:\n"
        "    logging.getLogger('Ammeters').exception('something broke')\n"
    )
    child = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                           capture_output=True, text=True, timeout=30)
    caplog.set_level(logging.DEBUG)
    forward_logs("greenlee", child.stderr.splitlines())
    records = [r for r in caplog.records if r.name == "Ammeters"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert records[0].levelno == logging.ERROR
    assert message.startswith("something broke")
    assert "Traceback (most recent call last)" in message and "ZeroDivisionError" in message


def test_a_line_that_is_not_json_is_shown_as_an_error(caplog):
    caplog.set_level(logging.DEBUG)
    forward_logs("entes", ["Fatal Python error: something went very wrong"])
    [record] = caplog.records
    assert record.levelno == logging.ERROR and record.name == "Ammeters.entes"
    assert "Fatal Python error" in record.getMessage()


def test_a_taken_port_is_reported_in_the_main_process(caplog):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        emulators = start_emulators({"circutor": emulator_client("circutor", port)})
        try:
            exit_code = wait_for_exit(emulators.processes["circutor"])
            time.sleep(0.2)
        finally:
            emulators.stop()
    assert exit_code not in (None, 0)
    assert any(r.levelno == logging.ERROR and "cannot listen on port" in r.getMessage()
               for r in caplog.records)
