import logging
import os
import socket
import time
import random
from abc import ABC, abstractmethod

NOT_IMPLEMENTED_MSG = "Subclasses must implement this property."

# The details of each request are logged as debug. They go to the log file
# and not to the screen, where hundreds of lines would hide the report.
logger = logging.getLogger("Ammeters")

# The server handles one client at a time. Without a timeout, a client that
# connects and never sends anything would block everyone else. It must be
# shorter than connection.timeout_seconds, so the next reading still makes it.
CLIENT_TIMEOUT_S = 1.0


def _allow_quick_restart(server_socket: socket.socket) -> None:
    """Lets the server use its port again right after the previous run.

    On Linux and Mac a closed port stays busy for a while, and without
    SO_REUSEADDR you get "Address already in use". On Windows SO_REUSEADDR
    would let another program share the port, and that problem doesn't exist
    there anyway, so we use SO_EXCLUSIVEADDRUSE instead.
    """
    if os.name == "posix":
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

class AmmeterEmulatorBase(ABC):
    def __init__(self, port: int):
        self.port = port
        # Seed the random number generator for each instance
        random.seed(time.time())

    def start_server(self):
        """
        Starts the server to listen for client requests.
        The server will run indefinitely, handling one client request at a time.
        """
        name = self.__class__.__name__
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            _allow_quick_restart(s)
            try:
                s.bind(('localhost', self.port))
            except OSError as e:
                # For example, the port is already taken. A clear message is
                # better than a thread traceback, and the client will just see
                # that this ammeter isn't up.
                logger.error("%s cannot listen on port %d: %s", name, self.port, e)
                return
            s.listen()
            logger.info("%s is running on port %d", name, self.port)
            while True:
                conn, addr = s.accept()
                # One bad client (one that disconnects early or never sends)
                # shouldn't bring the whole server down.
                try:
                    with conn:
                        conn.settimeout(CLIENT_TIMEOUT_S)
                        logger.debug("%s: connected by %s", name, addr)
                        data = conn.recv(1024)
                        if data == self.get_current_command:
                            # Call the specific measure_current() method defined in subclasses
                            current = self.measure_current()
                            conn.sendall(str(current).encode('utf-8'))
                except OSError as e:
                    logger.warning("%s: connection from %s failed: %s", name, addr, e)

    @property
    @abstractmethod
    def get_current_command(self) -> bytes:
        """
        This property must be implemented by each subclass to provide the specific
        command to get the current measurement.
        """
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

    @abstractmethod
    def measure_current(self) -> float:
        """
        This method must be implemented by each subclass to provide the specific
        logic for current measurement.
        """
        raise NotImplementedError(NOT_IMPLEMENTED_MSG)

