"""TCP client for the ammeters.

All three models talk the same way: connect to the port, send a command, and
read the number they send back until they close the connection. Only the port
and the command are different, and both come from config.yaml, so one class
works for all of them.
"""
import logging
import math
import socket
import time
from typing import Any, Protocol

from src.exceptions import (
    AmmeterConnectionError,
    AmmeterTimeoutError,
    ConfigError,
    InvalidResponseError,
)
from src.settings import AmmeterSettings, ConnectionSettings, Settings, load_settings

logger = logging.getLogger(__name__)

# A reading is a short number, so anything longer means something is wrong.
# It also protects us from a broken server that never stops sending.
MAX_RESPONSE_BYTES = 64
RECV_CHUNK_BYTES = 1024
READY_POLL_S = 0.05


class Ammeter(Protocol):
    """Anything that can give a current reading.

    The rest of the framework only uses this, not TCP, so a fake ammeter
    can be used instead in tests and simulations.
    """

    @property
    def name(self) -> str: ...

    def measure(self) -> float:
        """Returns one reading in amperes."""
        ...


class TcpAmmeterClient:
    """Gets readings from one ammeter over TCP, with timeouts and retries."""

    def __init__(self, settings: AmmeterSettings, connection: ConnectionSettings):
        self._settings = settings
        self._connection = connection
        self._command = settings.command.encode("utf-8")

    @property
    def name(self) -> str:
        return self._settings.name

    @property
    def settings(self) -> AmmeterSettings:
        return self._settings

    @property
    def connection(self) -> ConnectionSettings:
        return self._connection

    @property
    def address(self) -> str:
        return f"{self._connection.host}:{self._settings.port}"

    def __repr__(self) -> str:
        return f"TcpAmmeterClient(name={self.name!r}, address={self.address!r})"

    def measure(self) -> float:
        """Asks for one reading and returns it in amperes.

        Network errors are retried, waiting a bit longer each time. A bad
        reply isn't retried, since asking again would give the same answer.

        Raises:
            AmmeterTimeoutError: The last attempt timed out.
            AmmeterConnectionError: The last attempt failed for another network reason.
            InvalidResponseError: The reply is empty, too long, or not a finite number.
        """
        attempts = self._connection.retries + 1
        last_error: AmmeterConnectionError | None = None

        for attempt in range(1, attempts + 1):
            try:
                raw = self._exchange()
            except AmmeterConnectionError as e:
                last_error = e
                if attempt < attempts:
                    delay = self._connection.retry_backoff_seconds * 2 ** (attempt - 1)
                    logger.warning(
                        "%s: attempt %d/%d failed (%s); retrying in %.2f s",
                        self.name, attempt, attempts, e, delay,
                    )
                    time.sleep(delay)
                continue

            value = self._parse(raw)
            logger.debug("%s: reply %r -> %r A (attempt %d)", self.name, raw, value, attempt)
            return value

        logger.error("%s: no reading after %d attempt(s): %s", self.name, attempts, last_error)
        # Raise the same type as the last error, so a timeout still looks
        # like a timeout to the caller.
        raise type(last_error)(
            f"{self.name}: no reading after {attempts} attempt(s). Last error: {last_error}"
        ) from last_error

    def wait_until_ready(self, timeout_seconds: float) -> None:
        """Waits until the ammeter accepts connections.

        Used right after starting an emulator, instead of a fixed sleep.

        Raises:
            ValueError: timeout_seconds is not positive.
            AmmeterTimeoutError: The ammeter didn't come up in time.
        """
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds!r}")

        address = (self._connection.host, self._settings.port)
        deadline = time.monotonic() + timeout_seconds
        last_error: OSError | None = None

        while True:
            remaining = deadline - time.monotonic()
            try:
                with socket.create_connection(address, timeout=max(remaining, 0.01)):
                    pass
                logger.debug("%s: ready on %s", self.name, self.address)
                return
            except OSError as e:
                last_error = e
            if time.monotonic() + READY_POLL_S >= deadline:
                break
            time.sleep(READY_POLL_S)

        raise AmmeterTimeoutError(
            f"{self.name}: not accepting connections on {self.address} "
            f"after {timeout_seconds} s. Last error: {last_error}"
        )

    def _exchange(self) -> bytes:
        """Sends the command once and returns the raw reply.

        The whole thing (connect, send, read) has to finish within
        timeout_seconds.
        """
        timeout = self._connection.timeout_seconds
        deadline = time.monotonic() + timeout
        address = (self._connection.host, self._settings.port)

        # Note: on Windows a connect to a closed local port keeps retrying for
        # about 2 seconds, so it can end up as a timeout and not as "refused".
        try:
            sock = socket.create_connection(address, timeout=timeout)
        except TimeoutError as e:
            raise AmmeterTimeoutError(
                f"could not connect to {self.address} within {timeout} s"
            ) from e
        except OSError as e:
            # Refused, unknown host and so on.
            raise AmmeterConnectionError(
                f"could not connect to {self.address}: {type(e).__name__}: {e}"
            ) from e

        with sock:
            try:
                sock.sendall(self._command)
                return self._read_until_closed(sock, deadline)
            except TimeoutError as e:
                raise AmmeterTimeoutError(
                    f"no complete reply from {self.address} within {timeout} s"
                ) from e
            except OSError as e:
                # The connection was reset or dropped in the middle.
                raise AmmeterConnectionError(
                    f"connection to {self.address} broke: {type(e).__name__}: {e}"
                ) from e

    def _read_until_closed(self, sock: socket.socket, deadline: float) -> bytes:
        # There's no length or end marker in the protocol. The ammeter just
        # closes the connection after the reply, so we read until recv()
        # returns nothing.
        chunks: list[bytes] = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("deadline passed while reading the reply")
            # Timing out each recv() alone isn't enough: a server sending one
            # byte at a time could keep us here forever.
            sock.settimeout(remaining)
            chunk = sock.recv(RECV_CHUNK_BYTES)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise InvalidResponseError(
                    f"{self.name}: reply is longer than {MAX_RESPONSE_BYTES} bytes"
                )
            chunks.append(chunk)

    def _parse(self, raw: bytes) -> float:
        if not raw:
            # The emulator closes without answering when it doesn't know the command.
            raise InvalidResponseError(
                f"{self.name}: empty reply. The ammeter probably did not recognise "
                f"the command {self._settings.command!r}"
            )
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as e:
            raise InvalidResponseError(f"{self.name}: reply is not ASCII text: {raw!r}") from e
        try:
            # float() ignores spaces and newlines around the number.
            value = float(text)
        except ValueError as e:
            raise InvalidResponseError(f"{self.name}: reply is not a number: {text!r}") from e
        # float() happily accepts "nan" and "inf", so check for those too.
        if not math.isfinite(value):
            raise InvalidResponseError(f"{self.name}: reply is not a finite number: {text!r}")
        return value


def create_ammeters(config: dict[str, Any]) -> dict[str, TcpAmmeterClient]:
    """Creates a client for every ammeter in the config.

    Raises:
        ConfigError: The config is invalid.
    """
    return clients_from_settings(load_settings(config))


def clients_from_settings(settings: Settings) -> dict[str, TcpAmmeterClient]:
    """Creates a client for every ammeter, from settings that were already checked."""
    return {
        name: TcpAmmeterClient(ammeter, settings.connection)
        for name, ammeter in settings.ammeters.items()
    }


def create_ammeter(config: dict[str, Any], name: str) -> TcpAmmeterClient:
    """Creates the client for one ammeter, by its name in the config.

    Raises:
        ConfigError: The config is invalid, or there's no ammeter with this name.
    """
    ammeters = create_ammeters(config)
    try:
        return ammeters[name]
    except KeyError:
        raise ConfigError(
            f"Unknown ammeter {name!r}. Available: {', '.join(sorted(ammeters))}"
        ) from None
