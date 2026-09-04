"""Interchangeable live-window and offline-replay frame sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Union

import cv2
import numpy as np

from .windows import WindowBinding


FrameValue = Union[np.ndarray, "CapturedFrame", str, Path]


@dataclass(frozen=True)
class CapturedFrame:
    """One BGR image and the capture context used to produce it."""

    image: np.ndarray
    timestamp: float
    sequence: int = 0
    binding: Optional[WindowBinding] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.image, np.ndarray) or self.image.ndim not in (2, 3):
            raise ValueError("CapturedFrame.image must be a 2-D or 3-D numpy array")

    @property
    def size(self) -> tuple[int, int]:
        height, width = self.image.shape[:2]
        return width, height


class FrameSource(ABC):
    """A pull-based source that yields one immutable capture record at a time."""

    @abstractmethod
    def capture(self) -> CapturedFrame:
        raise NotImplementedError

    def close(self) -> None:
        """Release source resources.  Replay sources have nothing to release."""

    def __enter__(self) -> "FrameSource":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LiveWindowFrameSource(FrameSource):
    """Capture only the DPI-aware client area of one bound HWND."""

    def __init__(
        self,
        binding: WindowBinding,
        *,
        backend: Optional[Any] = None,
        refresh_binding: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.binding = binding
        self._backend = backend
        self._owns_backend = backend is None
        self._refresh_binding = refresh_binding
        self._clock = clock
        self._sequence = 0

    def _get_backend(self) -> Any:
        if self._backend is None:
            import mss

            self._backend = mss.mss()
        return self._backend

    def capture(self) -> CapturedFrame:
        if self._refresh_binding:
            self.binding = self.binding.refreshed()
        raw = np.asarray(self._get_backend().grab(self.binding.client_rect.as_mss_region()))
        if raw.ndim == 3 and raw.shape[2] == 4:
            image = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
        elif raw.ndim == 3 and raw.shape[2] == 3:
            image = raw.copy()
        else:
            raise ValueError(f"Capture backend returned unsupported shape: {raw.shape}")
        self._sequence += 1
        return CapturedFrame(
            image=image,
            timestamp=self._clock(),
            sequence=self._sequence,
            binding=self.binding,
        )

    def capture_to(self, path: Union[str, Path]) -> Path:
        return save_frame(self.capture(), path)

    def close(self) -> None:
        if self._owns_backend and self._backend is not None:
            close = getattr(self._backend, "close", None)
            if callable(close):
                close()
        self._backend = None


class ReplayFrameSource(FrameSource):
    """Deterministic source for arrays, saved screenshots or CapturedFrames."""

    def __init__(
        self,
        frames: Iterable[FrameValue],
        *,
        loop: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._frames = list(frames)
        if not self._frames:
            raise ValueError("ReplayFrameSource requires at least one frame")
        self.loop = loop
        self._clock = clock
        self._index = 0
        self._sequence = 0

    @classmethod
    def from_directory(
        cls,
        directory: Union[str, Path],
        *,
        loop: bool = False,
    ) -> "ReplayFrameSource":
        directory = Path(directory)
        supported = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
        files = sorted(path for path in directory.iterdir() if path.suffix.lower() in supported)
        return cls(files, loop=loop)

    def reset(self) -> None:
        self._index = 0
        self._sequence = 0

    def capture(self) -> CapturedFrame:
        if self._index >= len(self._frames):
            if not self.loop:
                raise StopIteration("Replay frame sequence is exhausted")
            self._index = 0

        value = self._frames[self._index]
        self._index += 1
        self._sequence += 1

        if isinstance(value, CapturedFrame):
            return CapturedFrame(
                image=value.image.copy(),
                timestamp=value.timestamp,
                sequence=self._sequence,
                binding=value.binding,
                metadata=dict(value.metadata),
            )
        if isinstance(value, (str, Path)):
            path = Path(value)
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"Unable to decode replay frame: {path}")
            metadata = {"source_path": str(path)}
        elif isinstance(value, np.ndarray):
            image = value.copy()
            metadata = {}
        else:
            raise TypeError(f"Unsupported replay frame type: {type(value).__name__}")

        return CapturedFrame(
            image=image,
            timestamp=self._clock(),
            sequence=self._sequence,
            metadata=metadata,
        )


def save_frame(frame: Union[CapturedFrame, np.ndarray], path: Union[str, Path]) -> Path:
    """Save a BGR frame, creating only the requested parent directory."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = frame.image if isinstance(frame, CapturedFrame) else frame
    if not cv2.imwrite(str(destination), image):
        raise OSError(f"Failed to write screenshot: {destination}")
    return destination


__all__ = [
    "CapturedFrame",
    "FrameSource",
    "LiveWindowFrameSource",
    "ReplayFrameSource",
    "save_frame",
]
