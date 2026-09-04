"""Compatibility facade for :mod:`config.runtime` configuration loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

from config.runtime import (
    DEFAULT_CONFIG_PATH,
    load_runtime_config as _load_runtime_config,
    validate_profile_name,
    validate_runtime_config,
)


PathLike = Union[str, Path]


def load_runtime_config(
    config_path: Optional[PathLike] = None,
    data_dir: Optional[PathLike] = None,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """Load canonical defaults/user config and optionally select a profile."""

    config = _load_runtime_config(config_path=config_path, data_dir=data_dir)
    if profile is not None:
        config["profile"] = validate_profile_name(profile)
        validate_runtime_config(config)
    return config


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "load_runtime_config",
    "validate_runtime_config",
]
