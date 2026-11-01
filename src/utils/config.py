"""Loads the YAML config file."""
import logging
from pathlib import Path
from typing import Any

import yaml

from src.exceptions import ConfigError
from src.utils.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """Reads the config file and returns it as a dict.

    Without a path it reads config/config.yaml from the project folder.

    Raises:
        ConfigError: The file is missing, can't be read, isn't valid YAML,
            is empty, or isn't a mapping at the top level.
    """
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    logger.debug("Loading config from %s", path)

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {path.resolve()}") from e
    except UnicodeDecodeError as e:
        raise ConfigError(f"Config file is not valid UTF-8 text: {path}") from e
    except OSError as e:
        # No permission, the path is a folder, and so on.
        raise ConfigError(f"Cannot read config file {path}: {e}") from e

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if data is None:
        raise ConfigError(f"Config file is empty: {path}")
    if not isinstance(data, dict):
        raise ConfigError(
            f"Config top level must be a mapping, got {type(data).__name__}: {path}"
        )

    logger.info("Loaded config from %s", path)
    return data
