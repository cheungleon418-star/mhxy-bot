"""Compatibility facade for the canonical runtime data-directory helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional, Union

from config.runtime import (
    DATA_DIR_ENV,
    RuntimePaths,
    ensure_data_layout,
    resolve_data_dir as _resolve_data_dir,
    runtime_paths,
)


PathLike = Union[str, os.PathLike[str]]
APP_DIR_NAME = "MHXY_Bot"


def resolve_data_dir(
    cli_data_dir: Optional[PathLike] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    local_app_data: Optional[PathLike] = None,
) -> Path:
    """Resolve CLI > MHXY_BOT_DATA_DIR > LocalAppData."""

    if local_app_data is None:
        return _resolve_data_dir(cli_data_dir, environ=environ)
    env = dict(os.environ if environ is None else environ)
    env["LOCALAPPDATA"] = str(local_app_data)
    return _resolve_data_dir(cli_data_dir, environ=env)


def ensure_runtime_dirs(
    data_dir: Optional[PathLike] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    local_app_data: Optional[PathLike] = None,
) -> RuntimePaths:
    """Create the canonical private data layout and first-run config."""

    root = resolve_data_dir(
        data_dir,
        environ=environ,
        local_app_data=local_app_data,
    )
    return ensure_data_layout(root)


__all__ = [
    "APP_DIR_NAME",
    "DATA_DIR_ENV",
    "RuntimePaths",
    "ensure_runtime_dirs",
    "resolve_data_dir",
    "runtime_paths",
]
