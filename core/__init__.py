"""Safe runtime primitives for capture, detection and input execution.

Exports are lazy so state-machine and configuration code can be imported for
diagnostics even before optional image/input dependencies are installed.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "ActionIntent": (".actions", "ActionIntent"),
    "ActionKind": (".actions", "ActionKind"),
    "ActionResult": (".actions", "ActionResult"),
    "SafeActionExecutor": (".actions", "SafeActionExecutor"),
    "DetectionResult": (".detection", "DetectionResult"),
    "TemplateDetector": (".detection", "TemplateDetector"),
    "TemplateManifest": (".detection", "TemplateManifest"),
    "TemplateRule": (".detection", "TemplateRule"),
    "CapturedFrame": (".frame_source", "CapturedFrame"),
    "FrameSource": (".frame_source", "FrameSource"),
    "LiveWindowFrameSource": (".frame_source", "LiveWindowFrameSource"),
    "ReplayFrameSource": (".frame_source", "ReplayFrameSource"),
    "save_frame": (".frame_source", "save_frame"),
    "RuntimePaths": (".paths", "RuntimePaths"),
    "ensure_runtime_dirs": (".paths", "ensure_runtime_dirs"),
    "resolve_data_dir": (".paths", "resolve_data_dir"),
    "load_runtime_config": (".runtime_config", "load_runtime_config"),
    "ClientRect": (".windows", "ClientRect"),
    "WindowBinding": (".windows", "WindowBinding"),
    "WindowBindingError": (".windows", "WindowBindingError"),
    "bind_game_window": (".windows", "bind_game_window"),
    "list_windows": (".windows", "list_windows"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))
