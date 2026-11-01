"""Tests for the TCP client, with broken servers and a real emulator."""
import socket
import threading
import time

import pytest

from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from src.ammeter_client import create_ammeter, create_ammeters
from src.exceptions import (AmmeterConnectionError, AmmeterTimeoutError, ConfigError,
                            InvalidResponseError)
from src.utils.config import load_config
from tests.conftest import EMULATOR_COMMANDS, client_for, emulator_client, free_port


def test_valid_reply_with_whitespace(fake_server):
    port = fake_server(lambda conn: conn.sendall(b" 1.5\n"))
    assert client_for(port).measure() == 1.5


@pytest.mark.parametrize(
    "reply, message",
    [
        (b"", "empty reply"),
        (b"abc", "not a number"),
        (b"nan", "not a finite number"),
        (b"inf", "not a finite number"),
        (b"\xff\xfe", "not ASCII"),
        (b"1" * 100, "longer than 64 bytes"),
    ],
)
def test_invalid_reply_is_rejected_without_retry(fake_server, reply, message):
    port = fake_server(lambda conn: conn.sendall(reply))
    start = time.monotonic()
    with pytest.raises(InvalidResponseError, match=message):
        client_for(port, retries=3, backoff=2.0).measure()
    # A retry would have waited 2 seconds first. The margin is big on purpose,
    # so a slow CI machine doesn't fail this test.
    assert time.monotonic() - start < 1.5


def test_slow_server_times_out_after_retries(fake_server):
    port = fake_server(lambda conn: time.sleep(1.5))
    with pytest.raises(AmmeterTimeoutError, match="2 attempt"):
        client_for(port, timeout=0.2, retries=1).measure()


def test_trickling_reply_hits_the_total_deadline(fake_server):
    def trickle(conn: socket.socket) -> None:
        for _ in range(20):
            conn.sendall(b"1")
            time.sleep(0.05)

    with pytest.raises(AmmeterTimeoutError):
        client_for(fake_server(trickle), timeout=0.3, retries=0).measure()


def test_closed_port_is_retried_with_backoff():
    # Linux says "refused", Windows may say timeout. Both are connection errors.
    with pytest.raises(AmmeterConnectionError, match="4 attempt"):
        client_for(free_port(), timeout=0.3, retries=3, backoff=0.01).measure()


def test_timeout_error_is_a_connection_error():
    assert issubclass(AmmeterTimeoutError, AmmeterConnectionError)


def test_wait_until_ready():
    with pytest.raises(AmmeterTimeoutError):
        client_for(free_port()).wait_until_ready(0.3)
    with pytest.raises(ValueError):
        client_for(free_port()).wait_until_ready(0)


def test_retry_recovers_when_the_emulator_starts_late():
    port = free_port()
    emulator = GreenleeAmmeter(port)
    threading.Timer(
        0.15, lambda: threading.Thread(target=emulator.start_server, daemon=True).start()
    ).start()
    client = client_for(port, timeout=1.0, retries=5, backoff=0.1,
                        command=EMULATOR_COMMANDS["greenlee"])
    # The first tries are refused, and one of the retries gets through.
    value = client.measure()
    assert value > 0


def test_a_silent_client_does_not_block_the_next_reading(emulator_ports):
    port = emulator_ports["entes"]
    with socket.create_connection(("127.0.0.1", port)):
        # This connection sends nothing. The emulator gives up on it after
        # 1 second, before the next client's own 2 second timeout.
        start = time.monotonic()
        value = client_for(port, timeout=2.0, retries=0,
                           command=EMULATOR_COMMANDS["entes"]).measure()
    assert value > 0 and time.monotonic() - start < 2.0


def test_emulator_survives_bad_clients(emulator_ports):
    port = emulator_ports["greenlee"]
    # A client that connects and doesn't send anything.
    socket.create_connection(("127.0.0.1", port)).close()
    with pytest.raises(InvalidResponseError, match="did not recognise"):
        client_for(port, command="WRONG").measure()
    assert emulator_client("greenlee", port).measure() > 0


def test_clients_are_created_from_the_config():
    config = load_config()
    clients = create_ammeters(config)
    assert set(clients) == {"greenlee", "entes", "circutor"}
    assert clients["circutor"].settings.command == "MEASURE_CIRCUTOR -get_measurement -current"
    with pytest.raises(ConfigError, match="fluke"):
        create_ammeter(config, "fluke")
