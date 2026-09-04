"""DPI-aware Windows game-window discovery and client coordinates.

The module deliberately avoids importing pywin32.  On non-Windows systems it
can still be imported for replay tests; discovery simply returns no windows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import sys
from typing import Optional, Sequence, Tuple


IS_WINDOWS = sys.platform == "win32"


class WindowBindingError(RuntimeError):
    """Raised when a requested live window cannot be bound."""


@dataclass(frozen=True)
class ClientRect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_mss_region(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class WindowBinding:
    """Stable HWND identity plus its latest screen-space client rectangle."""

    hwnd: int
    title: str
    process_id: int
    process_name: str
    client_rect: ClientRect
    dpi: int = 96
    expected_process_names: Tuple[str, ...] = ()
    expected_title_contains: Optional[str] = None

    @classmethod
    def bind(
        cls,
        hwnd: Optional[int] = None,
        *,
        process_names: Sequence[str] = (),
        title_contains: Optional[str] = None,
    ) -> "WindowBinding":
        return bind_game_window(
            hwnd=hwnd,
            process_names=process_names,
            title_contains=title_contains,
        )

    @property
    def client_size(self) -> Tuple[int, int]:
        return self.client_rect.width, self.client_rect.height

    @property
    def dpi_scale(self) -> float:
        return self.dpi / 96.0

    def refreshed(self) -> "WindowBinding":
        """Return a new binding after checking the HWND is still valid."""

        current = _binding_for_hwnd(self.hwnd)
        if current is None:
            raise WindowBindingError(f"Window is no longer available: HWND {self.hwnd}")
        if current.process_id != self.process_id:
            raise WindowBindingError("Window identity changed: the HWND now belongs to another process")
        if self.process_name and current.process_name.casefold() != self.process_name.casefold():
            raise WindowBindingError("Window process name changed")
        expected_processes = {name.casefold() for name in self.expected_process_names}
        if expected_processes and current.process_name.casefold() not in expected_processes:
            raise WindowBindingError("Window no longer matches the configured process names")
        if (self.expected_title_contains
                and self.expected_title_contains.casefold() not in current.title.casefold()):
            raise WindowBindingError("Window no longer matches the configured title")
        return replace(
            current,
            expected_process_names=self.expected_process_names,
            expected_title_contains=self.expected_title_contains,
        )

    def activate(self) -> bool:
        """Ask Windows to foreground this exact HWND without synthetic clicks."""

        if not IS_WINDOWS:
            return False
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        handle = wintypes.HWND(self.hwnd)
        foregrounded = bool(user32.SetForegroundWindow(handle))
        foreground = user32.GetForegroundWindow()
        return foregrounded or (foreground is not None and int(foreground) == self.hwnd)

    def local_to_screen(
        self,
        x: float,
        y: float,
        reference_size: Optional[Tuple[int, int]] = None,
    ) -> Tuple[int, int]:
        """Convert client coordinates (optionally calibrated) to screen pixels."""

        if reference_size is not None:
            rw, rh = reference_size
            if rw <= 0 or rh <= 0:
                raise ValueError("reference_size values must be positive")
            x = x * self.client_rect.width / rw
            y = y * self.client_rect.height / rh
        return (
            self.client_rect.left + int(round(x)),
            self.client_rect.top + int(round(y)),
        )

    def screen_to_local(
        self,
        x: float,
        y: float,
        reference_size: Optional[Tuple[int, int]] = None,
    ) -> Tuple[int, int]:
        """Convert screen pixels to client or calibration coordinates."""

        local_x = x - self.client_rect.left
        local_y = y - self.client_rect.top
        if reference_size is not None:
            rw, rh = reference_size
            if self.client_rect.width <= 0 or self.client_rect.height <= 0:
                raise WindowBindingError("Window client area has no size")
            local_x = local_x * rw / self.client_rect.width
            local_y = local_y * rh / self.client_rect.height
        return int(round(local_x)), int(round(local_y))


_DPI_AWARE = False


def enable_dpi_awareness() -> bool:
    """Opt the process into physical-pixel coordinates when Windows permits."""

    global _DPI_AWARE
    if _DPI_AWARE:
        return True
    if not IS_WINDOWS:
        return False

    import ctypes

    try:
        # PER_MONITOR_AWARE_V2.  The call can legitimately fail if another UI
        # framework set awareness before us, in which case coordinates are
        # still queried consistently through the same process.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_AWARE = True
            return True
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _DPI_AWARE = True
        return True
    except (AttributeError, OSError):
        return False


def _window_title(hwnd: int) -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, len(buffer))
    return buffer.value


def _process_name(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _binding_for_hwnd(hwnd: int) -> Optional[WindowBinding]:
    if not IS_WINDOWS:
        return None

    import ctypes
    from ctypes import wintypes

    enable_dpi_awareness()
    user32 = ctypes.windll.user32
    handle = wintypes.HWND(hwnd)
    if not user32.IsWindow(handle):
        return None

    rect = wintypes.RECT()
    if not user32.GetClientRect(handle, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(handle, ctypes.byref(origin)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    try:
        dpi = int(user32.GetDpiForWindow(handle)) or 96
    except (AttributeError, OSError):
        dpi = 96
    return WindowBinding(
        hwnd=int(hwnd),
        title=_window_title(hwnd),
        process_id=int(pid.value),
        process_name=_process_name(int(pid.value)),
        client_rect=ClientRect(origin.x, origin.y, width, height),
        dpi=dpi,
    )


def list_windows(*, visible_only: bool = True) -> list[WindowBinding]:
    """Enumerate top-level windows with a non-empty client area."""

    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes

    enable_dpi_awareness()
    user32 = ctypes.windll.user32
    found: list[WindowBinding] = []

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        binding = _binding_for_hwnd(int(hwnd))
        if binding is not None:
            found.append(binding)
        return True

    user32.EnumWindows(callback, 0)
    return found


def bind_game_window(
    hwnd: Optional[int] = None,
    *,
    process_names: Sequence[str] = (),
    title_contains: Optional[str] = None,
) -> WindowBinding:
    """Bind an exact HWND or the only window matching process/title filters."""

    if not IS_WINDOWS:
        raise WindowBindingError("Live window binding is only available on Windows")
    wanted_processes = {Path(name).name.casefold() for name in process_names}
    wanted_title = title_contains.casefold() if title_contains else None
    if hwnd is not None:
        binding = _binding_for_hwnd(int(hwnd))
        if binding is None:
            raise WindowBindingError(f"Invalid or unavailable HWND: {hwnd}")
        if wanted_processes and binding.process_name.casefold() not in wanted_processes:
            raise WindowBindingError("The requested HWND does not match the configured process names")
        if wanted_title and wanted_title not in binding.title.casefold():
            raise WindowBindingError("The requested HWND does not match the configured title")
        return replace(
            binding,
            expected_process_names=tuple(sorted(wanted_processes)),
            expected_title_contains=title_contains,
        )

    matches = []
    for binding in list_windows():
        if wanted_processes and binding.process_name.casefold() not in wanted_processes:
            continue
        if wanted_title and wanted_title not in binding.title.casefold():
            continue
        matches.append(binding)
    if not matches:
        filters = f"processes={sorted(wanted_processes)}, title={title_contains!r}"
        raise WindowBindingError(f"No game window matched {filters}")
    if len(matches) > 1:
        candidates = "; ".join(
            f"HWND={item.hwnd} pid={item.process_id} process={item.process_name!r} "
            f"title={item.title!r} size={item.client_size[0]}x{item.client_size[1]}"
            for item in sorted(matches, key=lambda candidate: candidate.hwnd)
        )
        raise WindowBindingError(
            "Multiple game windows matched. Set window.preferred_hwnd to exactly one candidate: "
            + candidates
        )

    selected = matches[0]
    return replace(
        selected,
        expected_process_names=tuple(sorted(wanted_processes)),
        expected_title_contains=title_contains,
    )


__all__ = [
    "ClientRect",
    "IS_WINDOWS",
    "WindowBinding",
    "WindowBindingError",
    "bind_game_window",
    "enable_dpi_awareness",
    "list_windows",
]
