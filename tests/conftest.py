"""Helpers for all the tests: a fake clock, fake ammeters, broken TCP servers
and the real emulators on free ports.

The tests never use the ports or folders from config/config.yaml, so they
don't clash with a running main.py and don't leave files in the project.
"""
import copy
import random
import socket
import threading
from pathlib import Path
from typing import Callable

import pytest
import yaml

from Ammeters.Circutor_Ammeter import CircutorAmmeter
from Ammeters.Entes_Ammeter import EntesAmmeter
from Ammeters.Greenlee_Ammeter import GreenleeAmmeter
from src.ammeter_client import TcpAmmeterClient
from src.exceptions import AmmeterConnectionError
from src.results import TestResult, make_result
from src.sampling import collect_samples
from src.settings import AmmeterSettings, ConnectionSettings, SamplingPlan
from src.utils.config import load_config


class FakeClock:
    """A clock that only moves when the code sleeps or a fake reading takes time.

    A 60 second run finishes in milliseconds, with exactly the same timing
    every time.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeAmmeter:
    """Returns 0, 1, 2 and so on.

    Each reading takes `cost` fake seconds, and the readings in fail_at fail.
    """

    def __init__(self, clock: FakeClock, *, name: str = "fake", cost: float = 0.0,
                 fail_at=()) -> None:
        self.name = name
        self.clock = clock
        self.cost = cost
        self.fail_at = set(fail_at)
        self.calls = 0

    def measure(self) -> float:
        index = self.calls
        self.calls += 1
        self.clock.now += self.cost
        if index in self.fail_at:
            raise AmmeterConnectionError("simulated failure")
        return float(index)


class GaussianAmmeter:
    """Random readings around `mean` (normal distribution), the same for the same seed."""

    def __init__(self, clock: FakeClock, *, name: str, mean: float, rel_std: float,
                 fail_at=(), seed: int = 1) -> None:
        self.name = name
        self.clock = clock
        self.mean = mean
        self.rel_std = rel_std
        self.fail_at = set(fail_at)
        self.calls = 0
        self.random = random.Random(seed)

    def measure(self) -> float:
        index = self.calls
        self.calls += 1
        self.clock.now += 0.004
        if index in self.fail_at:
            raise AmmeterConnectionError("simulated failure")
        return self.random.gauss(self.mean, self.mean * self.rel_std)


def sample(ammeter, count: int = 10, frequency_hz: float = 10.0, max_failures: int = 5,
           clock: FakeClock | None = None):
    """Run collect_samples() on a fake clock."""
    clock = clock or getattr(ammeter, "clock", None) or FakeClock()
    plan = SamplingPlan(count, frequency_hz, max_failures)
    return collect_samples(ammeter, plan, clock=clock.time, sleep=clock.sleep)


def result_of(ammeter, count: int = 10, batch_id: str | None = None, **kwargs) -> TestResult:
    """A full TestResult from a fake ammeter, with no TCP and no files."""
    run = sample(ammeter, count, **kwargs)
    return make_result(run, AmmeterSettings(run.ammeter, 5000, "CMD"), ConnectionSettings(),
                       batch_id)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_server() -> Callable[[Callable[[socket.socket], None]], int]:
    """Starts TCP servers that answer every request with handler(conn).

    Gives a function that starts a server and returns its port. We use it to
    send the client replies that a real ammeter should never send.
    """
    servers: list[socket.socket] = []

    def start(handler: Callable[[socket.socket], None]) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        servers.append(server)

        def serve() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    # The server was closed at the end of the test.
                    return
                with conn:
                    try:
                        conn.recv(1024)
                        handler(conn)
                    except OSError:
                        pass

        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()[1]

    yield start
    for server in servers:
        server.close()


def client_for(port: int, *, timeout: float = 0.5, retries: int = 2, backoff: float = 0.05,
               command: str = "CMD") -> TcpAmmeterClient:
    return TcpAmmeterClient(
        AmmeterSettings("fake", port, command),
        ConnectionSettings("127.0.0.1", timeout, retries, backoff),
    )


EMULATOR_COMMANDS = {
    "greenlee": "MEASURE_GREENLEE -get_measurement",
    "entes": "MEASURE_ENTES -get_data",
    "circutor": "MEASURE_CIRCUTOR -get_measurement -current",
}


def emulator_client(name: str, port: int) -> TcpAmmeterClient:
    """A client for one of the test emulators, with the normal connection settings."""
    return TcpAmmeterClient(AmmeterSettings(name, port, EMULATOR_COMMANDS[name]),
                            ConnectionSettings())


@pytest.fixture(scope="session")
def emulator_ports() -> dict[str, int]:
    """The three real emulators, started once for all the tests, on free ports."""
    ports = {"greenlee": free_port(), "entes": free_port(), "circutor": free_port()}
    classes = {"greenlee": GreenleeAmmeter, "entes": EntesAmmeter, "circutor": CircutorAmmeter}
    for name, port in ports.items():
        threading.Thread(target=classes[name](port).start_server, daemon=True).start()
    for port in ports.values():
        client_for(port).wait_until_ready(5.0)
    return ports


@pytest.fixture
def config_file(tmp_path: Path, emulator_ports: dict[str, int]) -> Path:
    """The project config, but with the test emulators' ports and temporary folders."""
    config = copy.deepcopy(load_config())
    for name, port in emulator_ports.items():
        config["ammeters"][name]["port"] = port
    config["result_management"]["directory"] = str(tmp_path / "runs")
    config["analysis"]["visualization"]["directory"] = str(tmp_path / "plots")
    # Not testing the machine's clock here (see main_config_file in test_end_to_end.py).
    config["testing"]["sampling"]["max_late_fraction"] = 1.0
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path
