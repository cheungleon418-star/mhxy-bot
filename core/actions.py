"""Intent-based input execution with explicit live arming and safety gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import threading
import time
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .windows import WindowBinding


logger = logging.getLogger(__name__)


class ActionKind(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    KEY = "key"
    HOTKEY = "hotkey"
    INTERACT = "interact"
    DIAGNOSTIC = "diagnostic"
    PAUSE = "pause"
    STOP = "stop"
    NONE = "none"


@dataclass(frozen=True)
class ActionIntent:
    kind: Union[ActionKind, str]
    target: Optional[Tuple[int, int]] = None
    key: Optional[str] = None
    keys: Tuple[str, ...] = ()
    button: str = "left"
    description: str = ""
    postcondition: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActionKind):
            object.__setattr__(self, "kind", ActionKind(str(self.kind).lower()))
        object.__setattr__(self, "keys", tuple(self.keys))


@dataclass(frozen=True)
class ActionResult:
    intent: ActionIntent
    accepted: bool
    performed: bool
    reason: str
    timestamp: float
    tick_id: int
    awaiting_postcondition: Optional[str] = None


@dataclass(frozen=True)
class PendingPostcondition:
    name: str
    intent: ActionIntent
    since: float


class InputBackend(Protocol):
    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None: ...

    def press(self, key: str) -> None: ...

    def hotkey(self, *keys: str) -> None: ...


class PyAutoGUIBackend:
    """Lazy adapter so importing core actions never initializes GUI input."""

    def __init__(self) -> None:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        self._api = pyautogui

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self._api.click(x=x, y=y, button=button, clicks=clicks)

    def press(self, key: str) -> None:
        self._api.press(key)

    def hotkey(self, *keys: str) -> None:
        self._api.hotkey(*keys)


class SafeActionExecutor:
    """Execute at most one intent per tick after an explicit live arm.

    Dry-run is the constructor default and never instantiates an OS input
    backend.  Live mode needs both ``dry_run=False`` and a call to :meth:`arm`.
    A pending postcondition blocks later input until the observer confirms it.
    """

    def __init__(
        self,
        *,
        dry_run: bool = True,
        binding: Optional[WindowBinding] = None,
        backend: Optional[InputBackend] = None,
        forbidden_keys: Sequence[str] = ("f9",),
        clock: Callable[[], float] = time.monotonic,
        max_frame_age_seconds: float = 1.0,
    ) -> None:
        self.dry_run = bool(dry_run)
        self.binding = binding
        self._backend = backend
        self._forbidden_keys = {key.casefold() for key in forbidden_keys}
        self._clock = clock
        self._max_frame_age_seconds = max(0.05, float(max_frame_age_seconds))
        self._armed = False
        self._paused = False
        self._emergency_stop = threading.Event()
        self._backend_fault = threading.Event()
        self._lock = threading.RLock()
        self._tick_id = 0
        self._used_this_tick = False
        self._pending: Optional[PendingPostcondition] = None
        self._hotkey_handle: Any = None
        self._last_capture_sequence = 0

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._emergency_stop.is_set()

    @property
    def faulted(self) -> bool:
        return self._backend_fault.is_set()

    @property
    def pending_postcondition(self) -> Optional[PendingPostcondition]:
        return self._pending

    @property
    def pending_age(self) -> Optional[float]:
        if self._pending is None:
            return None
        return max(0.0, self._clock() - self._pending.since)

    @property
    def current_tick(self) -> int:
        return self._tick_id

    def arm(self) -> bool:
        """Explicitly permit live input; dry-run remains non-performing."""

        with self._lock:
            if self.stopped:
                raise RuntimeError("Cannot arm after emergency stop; reset it while disarmed first")
            self._armed = True
            self._paused = False
            return not self.dry_run

    def disarm(self) -> None:
        with self._lock:
            self._armed = False

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._armed = False

    def resume(self) -> None:
        """Leave pause state without implicitly granting live-input authority."""

        with self._lock:
            if self.stopped:
                raise RuntimeError("Cannot resume after emergency stop")
            self._paused = False

    def emergency_stop(self) -> None:
        """Immediately make all future input ineligible."""

        with self._lock:
            self._emergency_stop.set()
            self._armed = False
            self._paused = False
            logger.warning("Emergency stop activated; live input is disarmed")

    def reset_emergency_stop(self) -> None:
        with self._lock:
            if self._armed:
                raise RuntimeError("Disarm before resetting emergency stop")
            self._emergency_stop.clear()

    def register_emergency_hotkey(self, key: str = "f11") -> bool:
        """Register a global hotkey when the optional keyboard backend works."""

        try:
            import keyboard

            self._hotkey_handle = keyboard.add_hotkey(key, self.emergency_stop)
            return True
        except Exception as exc:  # permission/support varies across hosts
            logger.warning("Could not register emergency hotkey %s: %s", key, exc)
            return False

    def unregister_emergency_hotkey(self) -> None:
        if self._hotkey_handle is None:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self._hotkey_handle)
        except Exception:
            logger.debug("Could not unregister emergency hotkey", exc_info=True)
        finally:
            self._hotkey_handle = None

    def begin_tick(self, tick_id: Optional[int] = None) -> int:
        with self._lock:
            if tick_id is None:
                tick_id = self._tick_id + 1
            if tick_id < self._tick_id:
                raise ValueError("tick_id cannot move backwards")
            if tick_id == self._tick_id:
                return self._tick_id
            self._tick_id = int(tick_id)
            self._used_this_tick = False
            return self._tick_id

    def confirm_postcondition(self, name: str, satisfied: bool = True) -> bool:
        """Clear the matching input gate only after positive observation."""

        with self._lock:
            if self._pending is None or self._pending.name != name:
                return False
            if not satisfied:
                return False
            self._pending = None
            return True

    def abandon_postcondition(self, name: Optional[str] = None) -> bool:
        """Explicitly clear a failed/timed-out gate before retrying a state."""

        with self._lock:
            if self._pending is None:
                return False
            if name is not None and self._pending.name != name:
                return False
            self._pending = None
            return True

    def _result(
        self,
        intent: ActionIntent,
        accepted: bool,
        performed: bool,
        reason: str,
    ) -> ActionResult:
        return ActionResult(
            intent=intent,
            accepted=accepted,
            performed=performed,
            reason=reason,
            timestamp=self._clock(),
            tick_id=self._tick_id,
            awaiting_postcondition=self._pending.name if self._pending else None,
        )

    def _contains_forbidden_key(self, intent: ActionIntent) -> bool:
        values = []
        if intent.key:
            values.append(intent.key)
        values.extend(intent.keys)
        return any(value.casefold() in self._forbidden_keys for value in values)

    def _screen_target(self, intent: ActionIntent) -> Tuple[int, int]:
        if intent.target is None:
            raise ValueError(f"{intent.kind.value} requires a target")
        x, y = intent.target
        if self.binding is None:
            raise RuntimeError("Client-coordinate click requires a WindowBinding")
        if intent.metadata.get("coordinate_space") == "screen":
            screen_x, screen_y = int(x), int(y)
        else:
            reference = intent.metadata.get("reference_size")
            if reference is not None:
                reference = tuple(reference)
            screen_x, screen_y = self.binding.local_to_screen(x, y, reference_size=reference)
        rect = self.binding.client_rect
        if not (rect.left <= screen_x < rect.right and rect.top <= screen_y < rect.bottom):
            raise ValueError("Click target falls outside the bound client area")
        return screen_x, screen_y

    def _get_backend(self) -> InputBackend:
        if self._backend is None:
            self._backend = PyAutoGUIBackend()
        return self._backend

    def _perform(self, intent: ActionIntent) -> None:
        if self.binding is None:
            raise RuntimeError("Live input requires a WindowBinding")
        required_context = {
            "capture_timestamp",
            "capture_sequence",
            "capture_hwnd",
            "capture_process_id",
            "capture_client_size",
            "capture_dpi",
        }
        missing = sorted(required_context - set(intent.metadata))
        if missing:
            raise RuntimeError("Live input requires complete capture context: " + ", ".join(missing))
        captured_at = intent.metadata.get("capture_timestamp")
        if captured_at is not None:
            frame_age = self._clock() - float(captured_at)
            if frame_age < -0.05 or frame_age > self._max_frame_age_seconds:
                raise RuntimeError(f"Captured frame is stale ({frame_age:.3f}s)")
        refreshed = self.binding.refreshed()
        expected_hwnd = intent.metadata.get("capture_hwnd")
        if expected_hwnd is not None and int(expected_hwnd) != int(getattr(refreshed, "hwnd", -1)):
            raise RuntimeError("Captured HWND no longer matches the input window")
        expected_pid = intent.metadata.get("capture_process_id")
        if expected_pid is not None and int(expected_pid) != int(getattr(refreshed, "process_id", -1)):
            raise RuntimeError("Captured process no longer matches the input window")
        expected_size = intent.metadata.get("capture_client_size")
        if expected_size is not None and tuple(int(v) for v in expected_size) != tuple(refreshed.client_size):
            raise RuntimeError("Game client size changed after frame capture")
        expected_dpi = intent.metadata.get("capture_dpi")
        if expected_dpi is not None and int(expected_dpi) != int(getattr(refreshed, "dpi", -1)):
            raise RuntimeError("Game DPI changed after frame capture")
        capture_sequence = intent.metadata.get("capture_sequence")
        if int(capture_sequence) != self._tick_id:
            raise RuntimeError("Captured frame sequence does not match the current tick")
        if capture_sequence is not None and int(capture_sequence) <= self._last_capture_sequence:
            raise RuntimeError("Captured frame sequence is stale or was already used")
        self.binding = refreshed
        # Focus validation applies to clicks too: without it a temporary
        # overlay covering the game could receive a screen-coordinate click.
        if not self.binding.activate():
            raise RuntimeError("Could not activate the bound game window")
        backend = self._get_backend()
        if intent.kind in {ActionKind.CLICK, ActionKind.DOUBLE_CLICK}:
            x, y = self._screen_target(intent)
            backend.click(
                x,
                y,
                button=intent.button,
                clicks=2 if intent.kind is ActionKind.DOUBLE_CLICK else 1,
            )
        elif intent.kind is ActionKind.KEY:
            if not intent.key:
                raise ValueError("key action requires key")
            backend.press(intent.key)
        elif intent.kind is ActionKind.HOTKEY:
            if not intent.keys:
                raise ValueError("hotkey action requires keys")
            backend.hotkey(*intent.keys)
        elif intent.kind is ActionKind.INTERACT:
            backend.press(intent.key or "space")
        if capture_sequence is not None:
            self._last_capture_sequence = int(capture_sequence)

    def execute(self, intent: ActionIntent, *, tick_id: Optional[int] = None) -> ActionResult:
        with self._lock:
            if not isinstance(intent, ActionIntent):
                raise TypeError("execute expects an ActionIntent")
            if tick_id is not None and tick_id != self._tick_id:
                self.begin_tick(tick_id)
            elif self._tick_id == 0:
                self.begin_tick()
            if self._used_this_tick:
                return self._result(intent, False, False, "one_action_per_tick")
            self._used_this_tick = True

            if intent.kind is ActionKind.STOP:
                self.emergency_stop()
                return self._result(intent, True, False, "stopped")
            if intent.kind is ActionKind.PAUSE:
                self.pause()
                return self._result(intent, True, False, "paused")
            if intent.kind is ActionKind.DIAGNOSTIC:
                if intent.metadata.get("stop"):
                    self.disarm()
                    return self._result(intent, True, False, "diagnostic_stop")
                return self._result(intent, True, False, "control_action")
            if intent.kind is ActionKind.NONE:
                return self._result(intent, True, False, "control_action")
            if self.stopped:
                return self._result(intent, False, False, "emergency_stop")
            if self._paused:
                return self._result(intent, False, False, "paused")
            if self._pending is not None:
                return self._result(intent, False, False, "postcondition_pending")
            if self._contains_forbidden_key(intent):
                return self._result(intent, False, False, "forbidden_key")

            if self.dry_run:
                if intent.postcondition:
                    self._pending = PendingPostcondition(intent.postcondition, intent, self._clock())
                return self._result(intent, True, False, "dry_run")
            if not self._armed:
                return self._result(intent, False, False, "not_armed")

            try:
                self._perform(intent)
            except Exception as exc:
                self._backend_fault.set()
                self._armed = False
                logger.exception("Input action failed: %s", intent.description or intent.kind.value)
                return self._result(intent, False, False, f"backend_error:{exc}")
            if intent.postcondition:
                self._pending = PendingPostcondition(intent.postcondition, intent, self._clock())
            return self._result(intent, True, True, "performed")


__all__ = [
    "ActionIntent",
    "ActionKind",
    "ActionResult",
    "InputBackend",
    "PendingPostcondition",
    "PyAutoGUIBackend",
    "SafeActionExecutor",
]
