"""The framework's errors.

They all inherit from AmmeterFrameworkError, so one except can catch any of them.

    AmmeterFrameworkError
    ├── ConfigError
    ├── AmmeterConnectionError      network problem, worth retrying
    │   └── AmmeterTimeoutError     the ammeter didn't answer in time
    ├── InvalidResponseError        the ammeter answered, but not with a number
    ├── ResultStorageError          a result can't be saved, found or read
    └── VisualizationError          a chart can't be drawn or saved
"""


class AmmeterFrameworkError(Exception):
    """Base class for all the framework's errors."""


class ConfigError(AmmeterFrameworkError):
    """The config file is missing, can't be read, or has bad values."""


class AmmeterConnectionError(AmmeterFrameworkError):
    """Couldn't reach the ammeter, or the connection broke.

    This can be temporary, so the client retries it.
    """


class AmmeterTimeoutError(AmmeterConnectionError):
    """The ammeter didn't accept the connection or didn't reply in time."""


class InvalidResponseError(AmmeterFrameworkError):
    """The ammeter replied, but the reply isn't a valid reading.

    Asking again would give the same reply, so there's no retry.
    """


class ResultStorageError(AmmeterFrameworkError):
    """A result file can't be written, found, or understood."""


class VisualizationError(AmmeterFrameworkError):
    """A chart can't be drawn (for example, no matplotlib) or saved."""
