"""Logging setup for the whole project.

Call setup_logging() once when the program starts. Each module then just
uses logging.getLogger(__name__), and everything ends up here.
"""
import logging
from datetime import datetime
from pathlib import Path

from src.utils.paths import PROJECT_ROOT

DEFAULT_LOG_DIR = PROJECT_ROOT / "results" / "logs"
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# Set after the first setup, so calling it again doesn't add handlers twice.
_log_file: Path | None = None


def setup_logging(
    log_dir: str | Path | None = None,
    console_level: int = logging.INFO,
) -> Path:
    """Sends the logs to the screen and to a new log file, and returns its path.

    The screen shows console_level and up, and the file keeps everything,
    including debug. The default folder is results/logs. It's fine to call
    this more than once.

    Raises:
        OSError: The log folder or file can't be created.
    """
    global _log_file
    if _log_file is not None:
        return _log_file

    directory = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    directory.mkdir(parents=True, exist_ok=True)
    # Milliseconds too, so two runs in the same second don't share a file.
    now = datetime.now()
    log_file = directory / f"{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}.log"

    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    # From other libraries we only want warnings. matplotlib, for example,
    # logs every font it looks up.
    for name in ("matplotlib", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)

    _log_file = log_file
    logging.getLogger(__name__).debug("Logging to %s", log_file)
    return log_file
