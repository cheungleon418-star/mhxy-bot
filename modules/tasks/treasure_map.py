# -*- coding: utf-8 -*-
"""Non-blocking, single-window treasure-map workflow.

The module performs no capture or input. A caller supplies one observation per
frame and executes at most the single ActionIntent returned by ``tick``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import time
from typing import Any, Callable, Mapping, Optional, Sequence

try:  # Remain importable while runtime core is being bootstrapped.
    from core.actions import ActionIntent
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    @dataclass(frozen=True)
    class ActionIntent:  # type: ignore[no-redef]
        kind: str
        target: Optional[tuple[int, int]] = None
        key: Optional[str] = None
        keys: tuple[str, ...] = ()
        button: str = "left"
        description: str = ""
        postcondition: Optional[str] = None
        metadata: Mapping[str, Any] = field(default_factory=dict)


class TreasureState(str, Enum):
    READY = "ready"
    OPEN_BACKPACK = "open_backpack"
    SELECT_MAP = "select_map"
    VERIFY_COMMITTED = "verify_committed"
    OPEN_MAP = "open_map"
    FIND_MARKER = "find_marker"
    NAVIGATE = "navigate"
    DIG = "dig"
    DIG_ACTION_PENDING = "dig_action_pending"
    CLASSIFY = "classify"
    COMBAT_WAIT = "combat_wait"
    DEATH_SCENE_WAIT = "death_scene_wait"
    DEATH_RECONCILE = "death_reconcile"
    DEATH_CLOSE_BACKPACK = "death_close_backpack"
    DEATH_OPEN_TASK_PANEL = "death_open_task_panel"
    DEATH_TASK_CHECK = "death_task_check"
    DEATH_CLOSE_TASK_PANEL = "death_close_task_panel"
    CLOSE_MAP_FOR_RETRY = "close_map_for_retry"
    CAPTCHA_PAUSE = "captcha_pause"
    DISCONNECT_STOP = "disconnect_stop"
    ERROR_STOP = "error_stop"
    COMPLETED = "completed"


TERMINAL_STATES = {
    TreasureState.DISCONNECT_STOP,
    TreasureState.ERROR_STOP,
    TreasureState.COMPLETED,
}

DEATH_STATES = {
    TreasureState.DEATH_SCENE_WAIT,
    TreasureState.DEATH_RECONCILE,
    TreasureState.DEATH_CLOSE_BACKPACK,
    TreasureState.DEATH_OPEN_TASK_PANEL,
    TreasureState.DEATH_TASK_CHECK,
    TreasureState.DEATH_CLOSE_TASK_PANEL,
}


@dataclass(frozen=True)
class TreasureObservation:
    """A single frame of task facts.

    Detection values may be booleans, mappings, or DetectionResult-like
    objects. Point-bearing detections expose ``center`` or ``point``.
    ``task_active=None`` means the task panel was not read.
    """

    timestamp: float = field(default_factory=time.time)
    detections: Mapping[str, Any] = field(default_factory=dict)
    inventory_signature: Optional[str] = None
    task_active: Optional[bool] = None
    resume_requested: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TreasureMapPolicy:
    stable_frames: int = 3
    max_retries: int = 3
    action_retry_seconds: float = 2.0
    ready_timeout: float = 15.0
    open_backpack_timeout: float = 8.0
    select_map_timeout: float = 5.0
    verify_committed_timeout: float = 10.0
    verification_probe_seconds: float = 3.0
    open_map_timeout: float = 8.0
    find_marker_timeout: float = 10.0
    navigate_timeout: float = 45.0
    dig_timeout: float = 8.0
    classify_timeout: float = 30.0
    combat_timeout: float = 240.0
    death_timeout: float = 60.0
    reconcile_timeout: float = 15.0
    reward_settle_seconds: float = 0.5

    def __post_init__(self) -> None:
        if self.stable_frames < 2:
            raise ValueError("stable_frames must be at least 2")
        if self.max_retries < 1:
            raise ValueError("max_retries must be positive")
        for name, value in vars(self).items():
            if name not in {"stable_frames", "max_retries"} and value <= 0:
                raise ValueError(f"{name} must be positive")


_SIGNALS = {
    "world_hud_anchor", "backpack_open", "treasure_map_icon",
    "map_panel_anchor", "quest_target_marker", "dig_interact_prompt",
    "combat_hud_anchor", "death_return_scene_anchor", "reward_dialog",
    "reward_confirm_button", "captcha_dialog",
    "disconnect_dialog", "map_consumed", "dig_success", "dig_failed",
    "task_panel_anchor", "active_treasure_task", "no_active_treasure_task",
}


def _is_visible(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("detected", "visible", "matched"):
            if key in value:
                return bool(value[key])
        if "confidence" in value:
            return float(value["confidence"]) > 0.0
        return bool(value)
    for attr in ("detected", "visible", "matched"):
        if hasattr(value, attr):
            return bool(getattr(value, attr))
    if hasattr(value, "confidence"):
        return float(getattr(value, "confidence")) > 0.0
    return bool(value)


def _reported_consecutive(value: Any) -> int:
    raw = value.get("consecutive", 1) if isinstance(value, Mapping) else getattr(value, "consecutive", 1)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _point(value: Any) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        candidate = value.get("center", value.get("point", value.get("target")))
    else:
        candidate = getattr(value, "center", getattr(value, "point", None))
    if candidate is None and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        candidate = value
    if candidate is None:
        return None
    try:
        return int(round(float(candidate[0]))), int(round(float(candidate[1])))
    except (IndexError, TypeError, ValueError):
        return None


class _SignalTracker:
    def __init__(self) -> None:
        self.present = {name: 0 for name in _SIGNALS}
        self.absent = {name: 0 for name in _SIGNALS}

    def update(self, observation: TreasureObservation) -> None:
        for name in _SIGNALS | set(observation.detections):
            value = observation.detections.get(name)
            if _is_visible(value):
                self.present[name] = max(self.present.get(name, 0) + 1, _reported_consecutive(value))
                self.absent[name] = 0
            else:
                self.present[name] = 0
                self.absent[name] = self.absent.get(name, 0) + 1

    def visible(self, observation: TreasureObservation, name: str) -> bool:
        return _is_visible(observation.detections.get(name))

    def stable(self, name: str, frames: int) -> bool:
        return self.present.get(name, 0) >= frames

    def stably_absent(self, name: str, frames: int) -> bool:
        return self.absent.get(name, 0) >= frames


def _coerce_observation(value: Any, fallback_time: Callable[[], float]) -> TreasureObservation:
    if isinstance(value, TreasureObservation):
        return value
    if isinstance(value, Mapping):
        detections = value.get("detections", value.get("matches", value.get("signals", {})))
        detections = dict(detections) if isinstance(detections, Mapping) else {}
        for name in _SIGNALS:
            if name in value:
                detections[name] = value[name]
        metadata = value.get("metadata", {})
        metadata = metadata if isinstance(metadata, Mapping) else {}
        return TreasureObservation(
            timestamp=float(value.get("timestamp", fallback_time())),
            detections=detections,
            inventory_signature=value.get("inventory_signature", value.get("inventory_revision")),
            task_active=value.get("task_active", metadata.get("task_active")),
            resume_requested=bool(value.get("resume_requested", False)),
            metadata=metadata,
        )
    detections = getattr(value, "detections", getattr(value, "matches", {}))
    detections = detections if isinstance(detections, Mapping) else {}
    metadata = getattr(value, "metadata", {})
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return TreasureObservation(
        timestamp=float(getattr(value, "timestamp", fallback_time())),
        detections=detections,
        inventory_signature=getattr(value, "inventory_signature", getattr(value, "inventory_revision", None)),
        task_active=getattr(value, "task_active", metadata.get("task_active")),
        resume_requested=bool(getattr(value, "resume_requested", False)),
        metadata=metadata,
    )


class TreasureMapStateMachine:
    """Deterministic controller for one game window."""

    def __init__(
        self,
        max_maps: int = 10,
        policy: Optional[TreasureMapPolicy] = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_maps < 1:
            raise ValueError("max_maps must be positive")
        self.max_maps = max_maps
        self.policy = policy or TreasureMapPolicy()
        self._clock = clock
        self.state = TreasureState.READY
        self.checkpoint = TreasureState.READY
        self.processed_maps = 0
        self.success_count = 0
        self.death_count = 0
        self.map_committed = False
        self.last_error: Optional[str] = None
        self.last_outcome: Optional[str] = None

        now = float(clock())
        self._state_entered_at = now
        self._last_timestamp = now
        self._seen_observation = False
        self._last_action_at: Optional[float] = None
        self._last_action_id: Optional[str] = None
        self._action_attempts: dict[str, int] = {}
        self._state_attempts_total = 0
        self._recovery_attempts = 0
        self._signals = _SignalTracker()
        self._pre_use_inventory_signature: Optional[str] = None
        self._pre_use_icon_visible = False
        self._map_use_accepted = False
        self._inventory_change_frames = 0
        self._task_active_frames = 0
        self._task_inactive_frames = 0
        self._verification_probe_sent = False
        self._reward_seen = False
        self._reward_seen_at: Optional[float] = None
        self._paused_state = TreasureState.READY
        self._resume_requested = False
        self._death_latched = False
        self._death_inventory_has_map: Optional[bool] = None
        self._death_destination: Optional[TreasureState] = None
        self._death_completion_reason: Optional[str] = None
        self._suspended_at: Optional[float] = None
        self._captcha_context: Optional[dict[str, Any]] = None

    @property
    def stopped(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def running(self) -> bool:
        return not self.stopped and self.state is not TreasureState.CAPTCHA_PAUSE

    def resume(self) -> None:
        self._resume_requested = True

    def suspend(self, timestamp: Optional[float] = None) -> None:
        """Freeze timeout/cooldown clocks while the outer runtime is paused."""

        if self._suspended_at is None:
            self._suspended_at = float(self._clock() if timestamp is None else timestamp)

    def resume_suspended(self, timestamp: Optional[float] = None) -> None:
        """Resume clocks without re-entering or resetting the current state."""

        if self._suspended_at is None:
            return
        now = float(self._clock() if timestamp is None else timestamp)
        paused_for = max(0.0, now - self._suspended_at)
        self._state_entered_at += paused_for
        if self._last_action_at is not None:
            self._last_action_at += paused_for
        if self._reward_seen_at is not None:
            self._reward_seen_at += paused_for
        if self._seen_observation:
            self._last_timestamp += paused_for
        self._suspended_at = None

    def acknowledge_action(
        self,
        intent: ActionIntent,
        *,
        accepted: bool,
        timestamp: Optional[float] = None,
        reason: str = "",
    ) -> None:
        """Feed execution acceptance back to transitions that require it.

        In particular, a combat result may only be attributed to a treasure
        after the dig/interact action was accepted by the executor.
        """

        action_id = str(getattr(intent, "metadata", {}).get("action_id", ""))
        now = self._last_timestamp if timestamp is None else float(timestamp)
        if action_id == "use_treasure_map":
            if not accepted:
                self.last_error = f"map_use_action_rejected:{reason or 'unknown'}"
                self._enter(TreasureState.ERROR_STOP, now)
            else:
                self._map_use_accepted = True
            return
        if action_id != "dig_treasure" or self.state is not TreasureState.DIG_ACTION_PENDING:
            return
        if accepted:
            self._enter(TreasureState.CLASSIFY, now)
            return
        self.last_error = f"dig_action_rejected:{reason or 'unknown'}"
        self._enter(TreasureState.ERROR_STOP, now)

    def abort(self, reason: str, timestamp: Optional[float] = None) -> None:
        """Move to a terminal error state after an executor/runtime fault."""

        self.last_error = str(reason)
        now = self._last_timestamp if timestamp is None else float(timestamp)
        self._enter(TreasureState.ERROR_STOP, now)

    def cancel_unexecuted_action(
        self, intent: ActionIntent, timestamp: Optional[float] = None
    ) -> None:
        """Roll back intent-dependent transitions after an intentional control stop."""

        action_id = str(getattr(intent, "metadata", {}).get("action_id", ""))
        now = self._last_timestamp if timestamp is None else float(timestamp)
        if action_id == "use_treasure_map" and self.state is TreasureState.VERIFY_COMMITTED:
            self._pre_use_inventory_signature = None
            self._pre_use_icon_visible = False
            self._map_use_accepted = False
            self._enter(TreasureState.SELECT_MAP, now)
        elif action_id == "dig_treasure" and self.state is TreasureState.DIG_ACTION_PENDING:
            self._enter(TreasureState.DIG, now)
        elif (action_id == "navigate_to_treasure_marker"
              and self.state is TreasureState.NAVIGATE):
            self._enter(TreasureState.FIND_MARKER, now)
        elif action_id == "commit_verification_probe":
            self._verification_probe_sent = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "checkpoint": self.checkpoint.value,
            "processed_maps": self.processed_maps,
            "success_count": self.success_count,
            "death_count": self.death_count,
            "map_committed": self.map_committed,
            "map_use_accepted": self._map_use_accepted,
            "last_outcome": self.last_outcome,
            "last_error": self.last_error,
        }

    def tick(self, observation: Any) -> Optional[ActionIntent]:
        if self.stopped:
            return None
        obs = _coerce_observation(observation, self._clock)
        now = self._normalise_timestamp(obs.timestamp)
        if now != obs.timestamp:
            obs = TreasureObservation(now, obs.detections, obs.inventory_signature,
                                      obs.task_active, obs.resume_requested, obs.metadata)
        self._signals.update(obs)
        self._update_semantic_counters(obs)
        if (self._death_latched
                and self.state not in DEATH_STATES
                and self._stable_absent("death_return_scene_anchor")):
            self._death_latched = False

        if self._stable("disconnect_dialog"):
            return self._stop(TreasureState.DISCONNECT_STOP, "disconnect_detected")
        if self.state is TreasureState.CAPTCHA_PAUSE:
            return self._tick_captcha_pause(obs)
        if self._stable("captcha_dialog"):
            self._paused_state = self.state
            self._captcha_context = {
                "elapsed": self._elapsed(obs),
                "last_action_age": (
                    None if self._last_action_at is None
                    else max(0.0, obs.timestamp - self._last_action_at)
                ),
                "last_action_id": self._last_action_id,
                "action_attempts": dict(self._action_attempts),
                "state_attempts_total": self._state_attempts_total,
            }
            self._enter(TreasureState.CAPTCHA_PAUSE, now)
            return self._intent("pause", description="captcha detected", reason="captcha_detected")

        if self.state not in DEATH_STATES:
            if not self._death_latched and self._stable("death_return_scene_anchor"):
                self._death_latched = True
                # COMBAT_WAIT already holds the stage that led into combat.
                # Do not replace it with the waiting state when death arrives.
                if self.state is not TreasureState.COMBAT_WAIT:
                    self.checkpoint = self.state
                self._enter(TreasureState.DEATH_SCENE_WAIT, now)
                return None
        if self.state not in DEATH_STATES and self.state is not TreasureState.COMBAT_WAIT \
                and self._stable("combat_hud_anchor"):
            self.checkpoint = self.state
            self._enter(TreasureState.COMBAT_WAIT, now)
            return None

        # A single unconfirmed critical frame suppresses input but does not
        # change state, preventing false positives from causing clicks.
        if self._visible_any(obs, "disconnect_dialog", "captcha_dialog"):
            return None
        if (not self._death_latched
                and self.state not in DEATH_STATES
                and self._visible_any(obs, "death_return_scene_anchor")):
            return None
        if (self.state not in DEATH_STATES and self.state is not TreasureState.COMBAT_WAIT
                and self._visible_any(obs, "combat_hud_anchor")):
            return None

        handler = {
            TreasureState.READY: self._tick_ready,
            TreasureState.OPEN_BACKPACK: self._tick_open_backpack,
            TreasureState.SELECT_MAP: self._tick_select_map,
            TreasureState.VERIFY_COMMITTED: self._tick_verify_committed,
            TreasureState.OPEN_MAP: self._tick_open_map,
            TreasureState.FIND_MARKER: self._tick_find_marker,
            TreasureState.NAVIGATE: self._tick_navigate,
            TreasureState.DIG: self._tick_dig,
            TreasureState.DIG_ACTION_PENDING: self._tick_dig_action_pending,
            TreasureState.CLASSIFY: self._tick_classify,
            TreasureState.COMBAT_WAIT: self._tick_combat_wait,
            TreasureState.DEATH_SCENE_WAIT: self._tick_death_scene_wait,
            TreasureState.DEATH_RECONCILE: self._tick_death_reconcile,
            TreasureState.DEATH_CLOSE_BACKPACK: self._tick_death_close_backpack,
            TreasureState.DEATH_OPEN_TASK_PANEL: self._tick_death_open_task_panel,
            TreasureState.DEATH_TASK_CHECK: self._tick_death_task_check,
            TreasureState.DEATH_CLOSE_TASK_PANEL: self._tick_death_close_task_panel,
            TreasureState.CLOSE_MAP_FOR_RETRY: self._tick_close_map_for_retry,
        }.get(self.state)
        return handler(obs) if handler else None

    def _tick_ready(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self.processed_maps >= self.max_maps:
            return self._complete("maximum_maps_processed")
        if self._elapsed(obs) >= self.policy.ready_timeout:
            return self._fail(obs, "world_or_ui_not_ready")
        overlay_handled, overlay_intent = self._normalize_startup_overlay(obs)
        if overlay_handled:
            return overlay_intent
        if self._stable("world_hud_anchor"):
            if self._stable("backpack_open"):
                self._enter(TreasureState.SELECT_MAP, obs.timestamp)
                return None
            if not self._stable_absent("backpack_open"):
                return None
            self._enter(TreasureState.OPEN_BACKPACK, obs.timestamp)
            return self._attempt_action(obs, "key", key="i", description="open backpack",
                                        postcondition="backpack_open")
        return None

    def _tick_open_backpack(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.open_backpack_timeout:
            return self._fail(obs, "backpack_open_timeout")
        overlay_handled, overlay_intent = self._normalize_startup_overlay(obs)
        if overlay_handled:
            return overlay_intent
        if self._stable("backpack_open"):
            self._enter(TreasureState.SELECT_MAP, obs.timestamp)
            return None
        if self._stable_absent("backpack_open"):
            return self._attempt_action(obs, "key", key="i", description="open backpack",
                                        postcondition="backpack_open")
        return None

    def _tick_select_map(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        overlay_handled, overlay_intent = self._normalize_startup_overlay(obs)
        if overlay_handled:
            if self._elapsed(obs) >= self.policy.select_map_timeout:
                return self._fail(obs, "unexpected_overlay_while_selecting_map")
            return overlay_intent
        if self._stable_absent("backpack_open"):
            self._enter(TreasureState.OPEN_BACKPACK, obs.timestamp)
            return self._attempt_action(obs, "key", key="i", description="restore backpack",
                                        postcondition="backpack_open")
        if not self._stable("backpack_open"):
            return None
        if self._stable("treasure_map_icon"):
            target = self._point_any(obs, "treasure_map_icon")
            if target is not None:
                self._pre_use_inventory_signature = obs.inventory_signature
                self._pre_use_icon_visible = True
                self._enter(TreasureState.VERIFY_COMMITTED, obs.timestamp)
                return self._attempt_action(obs, "click", target=target,
                                            description="use treasure map",
                                            postcondition="map_consumed_or_marker_visible",
                                            action_id="use_treasure_map", attempt_limit=1)
        if self._elapsed(obs) >= self.policy.select_map_timeout:
            if self._stable_absent("treasure_map_icon"):
                return self._complete("no_more_treasure_maps")
            return self._fail(obs, "treasure_map_icon_has_no_click_point")
        return None

    def _normalize_startup_overlay(
        self, obs: TreasureObservation
    ) -> tuple[bool, Optional[ActionIntent]]:
        """Close one positively identified blocking panel, or wait on unknown UI."""

        reward_visible = self._stable_any("reward_dialog", "reward_confirm_button")
        task_visible = self._stable("task_panel_anchor")
        map_visible = self._stable("map_panel_anchor")
        if sum((reward_visible, task_visible, map_visible)) > 1:
            return True, self._fail(obs, "startup_overlay_conflict")
        if reward_visible:
            if self._stable("reward_confirm_button"):
                target = self._point_any(obs, "reward_confirm_button")
                if target is not None:
                    return True, self._attempt_action(
                        obs,
                        "click",
                        target=target,
                        description="close leftover reward dialog",
                        postcondition="reward_dialog_closed",
                        action_id="normalize_reward_dialog",
                    )
            return True, None
        if task_visible:
            return True, self._attempt_action(
                obs,
                "key",
                key="j",
                description="close leftover task panel",
                postcondition="task_panel_closed",
                action_id="normalize_task_panel",
            )
        if map_visible:
            return True, self._attempt_action(
                obs,
                "key",
                key="m",
                description="close leftover map panel",
                postcondition="map_panel_closed",
                action_id="normalize_map_panel",
            )
        all_absent = all(
            self._stable_absent(name)
            for name in (
                "reward_dialog",
                "reward_confirm_button",
                "task_panel_anchor",
                "map_panel_anchor",
            )
        )
        return (False, None) if all_absent else (True, None)

    def _tick_verify_committed(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        reason = self._commit_evidence(obs)
        if reason:
            self._mark_committed(reason)
            if self._stable("map_panel_anchor"):
                self._enter(TreasureState.FIND_MARKER, obs.timestamp)
                return None
            self._enter(TreasureState.OPEN_MAP, obs.timestamp)
            return self._tick_open_map(obs)
        if (not self._verification_probe_sent
                and self._elapsed(obs) >= self.policy.verification_probe_seconds
                and self._stable_absent("map_panel_anchor")):
            self._verification_probe_sent = True
            return self._intent("key", key="m", description="open map to verify treasure marker",
                                postcondition="map_consumed_or_marker_visible",
                                reason="commit_verification_probe",
                                extra={
                                    "action_id": "commit_verification_probe",
                                    "supersedes_postconditions": [
                                        "map_consumed_or_marker_visible"
                                    ],
                                })
        if self._elapsed(obs) >= self.policy.verify_committed_timeout:
            return self._fail(obs, "map_consumption_unconfirmed")
        return None

    def _tick_open_map(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._stable("map_panel_anchor"):
            self._enter(TreasureState.FIND_MARKER, obs.timestamp)
            return None
        if self._elapsed(obs) >= self.policy.open_map_timeout:
            return self._fail(obs, "map_panel_open_timeout")
        if self._stable("backpack_open"):
            return self._attempt_action(obs, "key", key="escape",
                                        description="close backpack before opening map",
                                        postcondition="backpack_closed",
                                        action_id="close_backpack_before_map")
        if not self._stable_absent("backpack_open"):
            return None
        if not self._stable_absent("map_panel_anchor"):
            return None
        return self._attempt_action(obs, "key", key="m", description="open world map",
                                    postcondition="map_panel_anchor", action_id="open_world_map")

    def _tick_find_marker(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._stable("quest_target_marker"):
            target = self._point_any(obs, "quest_target_marker")
            if target is not None:
                self._enter(TreasureState.NAVIGATE, obs.timestamp)
                return self._attempt_action(obs, "click", target=target,
                                            description="navigate to treasure marker",
                                            postcondition="dig_interact_prompt",
                                            action_id="navigate_to_treasure_marker")
        if self._elapsed(obs) >= self.policy.find_marker_timeout:
            if self._recovery_attempts >= self.policy.max_retries:
                return self._fail(obs, "treasure_marker_not_found")
            self._recovery_attempts += 1
            if self._stable_absent("map_panel_anchor"):
                self._enter(TreasureState.OPEN_MAP, obs.timestamp)
                return None
            if not self._stable("map_panel_anchor"):
                return None
            self._enter(TreasureState.CLOSE_MAP_FOR_RETRY, obs.timestamp)
            return self._attempt_action(
                obs, "key", key="m", description="close map before marker retry",
                postcondition="map_panel_closed", action_id="close_map_for_marker_retry"
            )
        return None

    def _tick_close_map_for_retry(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._stable_absent("map_panel_anchor"):
            self._enter(TreasureState.OPEN_MAP, obs.timestamp)
            return None
        if self._elapsed(obs) >= self.policy.open_map_timeout:
            return self._fail(obs, "map_panel_close_timeout")
        if self._stable("map_panel_anchor"):
            return self._attempt_action(
                obs, "key", key="m", description="close map before marker retry",
                postcondition="map_panel_closed", action_id="close_map_for_marker_retry"
            )
        return None

    def _tick_navigate(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._stable("dig_interact_prompt"):
            self._enter(TreasureState.DIG, obs.timestamp)
            return None
        if self._elapsed(obs) >= self.policy.navigate_timeout:
            if self._recovery_attempts >= self.policy.max_retries:
                return self._fail(obs, "navigation_timeout")
            self._recovery_attempts += 1
            self._enter(TreasureState.OPEN_MAP, obs.timestamp)
            return self._intent(
                "none",
                description="navigation timed out; reset pending navigation gate",
                reason="navigation_timeout_recovery",
                extra={"supersedes_postconditions": ["dig_interact_prompt"]},
            )
        return None

    def _tick_dig(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._stable("dig_interact_prompt"):
            self._enter(TreasureState.DIG_ACTION_PENDING, obs.timestamp)
            return self._attempt_action(obs, "interact", description="dig treasure",
                                        postcondition="reward_or_combat_or_death",
                                        action_id="dig_treasure", attempt_limit=1)
        if self._elapsed(obs) >= self.policy.dig_timeout:
            if self._recovery_attempts >= self.policy.max_retries:
                return self._fail(obs, "dig_prompt_lost")
            self._recovery_attempts += 1
            self._enter(TreasureState.OPEN_MAP, obs.timestamp)
            return None
        return None

    def _tick_dig_action_pending(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        # Main acknowledges the emitted interact action synchronously. Reaching
        # this handler means execution feedback was lost, so never guess that a
        # dig occurred.
        if self._elapsed(obs) >= self.policy.action_retry_seconds:
            return self._fail(obs, "dig_action_ack_timeout")
        return None

    def _tick_classify(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        # A visible but unclickable/stuck modal must still end in a bounded,
        # diagnostic stop rather than bypassing this timeout forever.
        if self._elapsed(obs) >= self.policy.classify_timeout:
            return self._fail(obs, "dig_result_timeout")
        if self._stable_any("dig_success", "reward_dialog"):
            if not self._reward_seen:
                self._reward_seen = True
                self._reward_seen_at = obs.timestamp
        if self._stable("reward_confirm_button"):
            target = self._point_any(obs, "reward_confirm_button")
            if target is not None:
                self._reward_seen = True
                self._reward_seen_at = self._reward_seen_at or obs.timestamp
                return self._attempt_action(obs, "click", target=target,
                                            description="confirm treasure reward",
                                            postcondition="reward_dialog_closed",
                                            action_id="confirm_treasure_reward")
        # Never advance under a still-visible modal, even if a success marker
        # is simultaneously present behind or inside it.
        if self._visible_any(obs, "reward_dialog", "reward_confirm_button"):
            return None
        if self._stable("dig_success"):
            return self._finish_success(obs)
        settled = (
            self._reward_seen and self._reward_seen_at is not None
            and obs.timestamp - self._reward_seen_at >= self.policy.reward_settle_seconds
            and self._stable_absent("reward_dialog")
            and self._stable_absent("reward_confirm_button")
            and self._stable("world_hud_anchor")
        )
        if settled:
            return self._finish_success(obs)
        if self._stable("dig_failed"):
            if self._recovery_attempts >= self.policy.max_retries:
                return self._fail(obs, "dig_failed")
            self._recovery_attempts += 1
            self._enter(TreasureState.OPEN_MAP, obs.timestamp)
            return None
        return None

    def _tick_combat_wait(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        # Game-owned auto combat: intentionally issue no keys or clicks.
        if self._elapsed(obs) >= self.policy.combat_timeout:
            return self._fail(obs, "combat_timeout")
        if self._stable_absent("combat_hud_anchor"):
            if self._stable("dig_success"):
                return self._finish_success(obs)
            # A reward can be drawn over the world HUD. Let CLASSIFY close it
            # rather than treating the first visible world frame as success.
            if self._visible_any(obs, "reward_dialog", "reward_confirm_button"):
                if self._stable_any("reward_dialog", "reward_confirm_button"):
                    self._enter(TreasureState.CLASSIFY, obs.timestamp)
                return None
        if self._stable_absent("combat_hud_anchor") and self._stable("world_hud_anchor"):
            resume_state = self.checkpoint
            if resume_state is TreasureState.CLASSIFY:
                if self.map_committed:
                    return self._finish_success(obs)
                resume_state = TreasureState.CLASSIFY
            elif resume_state in {TreasureState.DIG, TreasureState.DIG_ACTION_PENDING}:
                # DIG is before an accepted interact action. Reacquire the
                # active marker/prompt instead of attributing combat as success.
                resume_state = TreasureState.OPEN_MAP if self.map_committed else TreasureState.READY
            elif resume_state not in {TreasureState.OPEN_MAP, TreasureState.FIND_MARKER,
                                      TreasureState.NAVIGATE, TreasureState.VERIFY_COMMITTED}:
                resume_state = TreasureState.CLASSIFY if self.map_committed else TreasureState.READY
            self._enter(resume_state, obs.timestamp)
        return None

    def _tick_death_scene_wait(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.death_timeout:
            return self._fail(obs, "death_scene_timeout")
        if (self._stable("death_return_scene_anchor") and self._stable("world_hud_anchor")
                and self._stable_absent("combat_hud_anchor")):
            self._enter(TreasureState.DEATH_RECONCILE, obs.timestamp)
            if self._stable("backpack_open"):
                return None
            if self._stable_absent("backpack_open"):
                return self._attempt_action(obs, "key", key="i",
                                            description="open backpack to reconcile after death",
                                            postcondition="backpack_open",
                                            action_id="death_open_backpack")
        return None

    def _tick_death_reconcile(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.reconcile_timeout:
            return self._fail(obs, "death_reconcile_timeout")
        if self._stable_absent("backpack_open"):
            return self._attempt_action(obs, "key", key="i",
                                        description="open backpack to reconcile after death",
                                        postcondition="backpack_open",
                                        action_id="death_open_backpack")
        if not self._stable("backpack_open"):
            return None
        reason = self._commit_evidence(obs)
        if not self.map_committed and reason:
            self._mark_committed(reason)
        if self._stable("treasure_map_icon"):
            self._death_inventory_has_map = True
        elif self._stable_absent("treasure_map_icon"):
            self._death_inventory_has_map = False
        else:
            return None
        self._enter(TreasureState.DEATH_CLOSE_BACKPACK, obs.timestamp)
        return self._attempt_action(
            obs, "key", key="escape", description="close backpack before task check",
            postcondition="backpack_closed", action_id="death_close_backpack"
        )

    def _tick_death_close_backpack(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.reconcile_timeout:
            return self._fail(obs, "death_backpack_close_timeout")
        if self._stable_absent("backpack_open"):
            self._enter(TreasureState.DEATH_OPEN_TASK_PANEL, obs.timestamp)
            return None
        if self._stable("backpack_open"):
            return self._attempt_action(
                obs, "key", key="escape", description="close backpack before task check",
                postcondition="backpack_closed", action_id="death_close_backpack"
            )
        return None

    def _tick_death_open_task_panel(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.reconcile_timeout:
            return self._fail(obs, "death_task_panel_open_timeout")
        if self._stable("task_panel_anchor"):
            self._enter(TreasureState.DEATH_TASK_CHECK, obs.timestamp)
            return None
        if self._stable_absent("task_panel_anchor"):
            return self._attempt_action(
                obs, "key", key="j", description="open task panel after death",
                postcondition="task_panel_anchor", action_id="death_open_task_panel"
            )
        return None

    def _tick_death_task_check(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.reconcile_timeout:
            return self._fail(obs, "death_task_status_timeout")
        if not self._stable("task_panel_anchor"):
            return None
        active = self._stable("active_treasure_task")
        inactive = self._stable("no_active_treasure_task")
        if active and inactive:
            return self._fail(obs, "death_task_status_conflict")
        if not active and not inactive:
            return None

        self._death_completion_reason = None
        if active:
            if not self.map_committed:
                self._mark_committed("treasure_task_active_after_death")
            self._death_destination = TreasureState.OPEN_MAP
        elif self.map_committed:
            self.death_count += 1
            self.last_outcome = "death"
            self._reset_current_map()
            if self.processed_maps >= self.max_maps:
                self._death_destination = None
                self._death_completion_reason = "maximum_maps_processed_after_death"
            else:
                self._death_destination = TreasureState.READY
        elif self._death_inventory_has_map is True and not self._map_use_accepted:
            self._death_destination = TreasureState.OPEN_BACKPACK
        else:
            return self._fail(obs, "death_consumption_status_ambiguous")

        self._enter(TreasureState.DEATH_CLOSE_TASK_PANEL, obs.timestamp)
        if self._stable("task_panel_anchor"):
            return self._attempt_action(
                obs, "key", key="j", description="close task panel after death check",
                postcondition="task_panel_closed", action_id="death_close_task_panel"
            )
        return None

    def _tick_death_close_task_panel(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if self._elapsed(obs) >= self.policy.reconcile_timeout:
            return self._fail(obs, "death_task_panel_close_timeout")
        if self._stable_absent("task_panel_anchor"):
            destination = self._death_destination
            completion_reason = self._death_completion_reason
            self._death_destination = None
            self._death_completion_reason = None
            self._death_inventory_has_map = None
            if completion_reason:
                return self._complete(completion_reason)
            if destination is None:
                return self._fail(obs, "death_reconcile_missing_destination")
            self._enter(destination, obs.timestamp)
            return None
        if self._stable("task_panel_anchor"):
            return self._attempt_action(
                obs, "key", key="j", description="close task panel after death check",
                postcondition="task_panel_closed", action_id="death_close_task_panel"
            )
        return None

    def _tick_captcha_pause(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        requested = self._resume_requested or obs.resume_requested
        if not requested or not self._stable_absent("captcha_dialog"):
            return None
        self._resume_requested = False
        resume_state = self._paused_state
        if resume_state in TERMINAL_STATES or resume_state is TreasureState.CAPTCHA_PAUSE:
            resume_state = TreasureState.OPEN_MAP if self.map_committed else TreasureState.READY
        context = self._captcha_context or {}
        self.state = resume_state
        self._state_entered_at = obs.timestamp - float(context.get("elapsed", 0.0))
        action_age = context.get("last_action_age")
        self._last_action_at = None if action_age is None else obs.timestamp - float(action_age)
        self._last_action_id = context.get("last_action_id")
        self._action_attempts = dict(context.get("action_attempts", {}))
        self._state_attempts_total = int(context.get("state_attempts_total", 0))
        self._captcha_context = None
        return None

    def _normalise_timestamp(self, raw: float) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = self._last_timestamp
        if not math.isfinite(value):
            value = self._last_timestamp
        # Recorded frames and live frames can use a different clock domain
        # from the constructor. Anchor all timeouts to the first observation.
        if not self._seen_observation:
            self._seen_observation = True
            self._last_timestamp = value
            self._state_entered_at = value
            return value
        value = max(value, self._last_timestamp)
        self._last_timestamp = value
        return value

    def _enter(self, state: TreasureState, timestamp: float) -> None:
        self.state = state
        self._state_entered_at = timestamp
        self._last_action_at = None
        self._last_action_id = None
        self._action_attempts = {}
        self._state_attempts_total = 0
        if state is TreasureState.VERIFY_COMMITTED:
            self._inventory_change_frames = 0
            self._verification_probe_sent = False
        if state is TreasureState.CLASSIFY:
            self._reward_seen = False
            self._reward_seen_at = None

    def _elapsed(self, obs: TreasureObservation) -> float:
        return max(0.0, obs.timestamp - self._state_entered_at)

    def _stable(self, name: str) -> bool:
        return self._signals.stable(name, self.policy.stable_frames)

    def _stable_any(self, *names: str) -> bool:
        return any(self._stable(name) for name in names)

    def _stable_absent(self, name: str) -> bool:
        return self._signals.stably_absent(name, self.policy.stable_frames)

    def _visible_any(self, obs: TreasureObservation, *names: str) -> bool:
        return any(self._signals.visible(obs, name) for name in names)

    def _point_any(self, obs: TreasureObservation, *names: str) -> Optional[tuple[int, int]]:
        for name in names:
            candidate = _point(obs.detections.get(name))
            if candidate is not None:
                return candidate
        return None

    def _update_semantic_counters(self, obs: TreasureObservation) -> None:
        if obs.task_active is True:
            self._task_active_frames += 1
            self._task_inactive_frames = 0
        elif obs.task_active is False:
            self._task_inactive_frames += 1
            self._task_active_frames = 0
        else:
            self._task_active_frames = self._task_inactive_frames = 0
        changed = (self._pre_use_inventory_signature is not None
                   and obs.inventory_signature is not None
                   and obs.inventory_signature != self._pre_use_inventory_signature)
        self._inventory_change_frames = self._inventory_change_frames + 1 if changed else 0

    def _task_status(self) -> Optional[bool]:
        if (self._task_active_frames >= self.policy.stable_frames
                or self._stable_any("active_treasure_task", "quest_target_marker")):
            return True
        if (self._task_inactive_frames >= self.policy.stable_frames
                or self._stable("no_active_treasure_task")):
            return False
        return None

    def _commit_evidence(self, obs: TreasureObservation) -> Optional[str]:
        if self.map_committed:
            return "already_committed"
        if self._stable("map_consumed"):
            return "map_consumed_signal"
        if self._inventory_change_frames >= self.policy.stable_frames:
            return "inventory_changed"
        if self._stable("quest_target_marker"):
            return "treasure_marker_visible"
        if self._task_active_frames >= self.policy.stable_frames or self._stable("active_treasure_task"):
            return "treasure_task_active"
        if (self._pre_use_icon_visible and self._stable("backpack_open")
                and self._stable_absent("treasure_map_icon")):
            return "inventory_icon_disappeared"
        return None

    def _mark_committed(self, reason: str) -> None:
        if not self.map_committed:
            self.map_committed = True
            self.processed_maps += 1
            self.last_outcome = f"committed:{reason}"

    def _reset_current_map(self) -> None:
        self.map_committed = False
        self._pre_use_inventory_signature = None
        self._pre_use_icon_visible = False
        self._map_use_accepted = False
        self._inventory_change_frames = 0
        self._verification_probe_sent = False
        self._reward_seen = False
        self._reward_seen_at = None
        self._recovery_attempts = 0
        self._death_inventory_has_map = None
        self._death_destination = None
        self._death_completion_reason = None

    def _finish_success(self, obs: TreasureObservation) -> Optional[ActionIntent]:
        if not self.map_committed:
            return self._fail(obs, "success_without_committed_map")
        self.success_count += 1
        self.last_outcome = "success"
        self._reset_current_map()
        if self.processed_maps >= self.max_maps:
            return self._complete("maximum_maps_processed")
        self._enter(TreasureState.READY, obs.timestamp)
        return None

    def _attempt_action(self, obs: TreasureObservation, kind: str, *,
                        target: Optional[tuple[int, int]] = None,
                        key: Optional[str] = None, description: str,
                        postcondition: Optional[str],
                        action_id: Optional[str] = None,
                        attempt_limit: Optional[int] = None,
                        supersedes_postconditions: Sequence[str] = ()) -> Optional[ActionIntent]:
        # Logical IDs deliberately exclude detector coordinates. Normal
        # center-point jitter must never reset retry limits or click cooldowns.
        logical_id = action_id or f"{self.state.value}:{kind}:{key or description}:{postcondition or '-'}"
        due = (self._last_action_at is None
               or self._last_action_id != logical_id
               or obs.timestamp - self._last_action_at >= self.policy.action_retry_seconds)
        if not due:
            return None
        limit = self.policy.max_retries if attempt_limit is None else max(1, int(attempt_limit))
        action_attempt = self._action_attempts.get(logical_id, 0)
        if action_attempt >= limit:
            return self._fail(obs, f"{self.state.value}_retry_exhausted")
        if self._state_attempts_total >= self.policy.max_retries * 2:
            return self._fail(obs, f"{self.state.value}_total_action_budget_exhausted")
        action_attempt += 1
        self._action_attempts[logical_id] = action_attempt
        self._state_attempts_total += 1
        self._last_action_at = obs.timestamp
        self._last_action_id = logical_id
        return self._intent(kind, target=target, key=key, description=description,
                            postcondition=postcondition,
                            reason=f"{self.state.value}_{logical_id}_attempt_{action_attempt}",
                            extra={
                                "action_id": logical_id,
                                "attempt": action_attempt,
                                "state_attempt": self._state_attempts_total,
                                "supersedes_postconditions": list(supersedes_postconditions),
                            })

    def _intent(self, kind: str, *, target: Optional[tuple[int, int]] = None,
                key: Optional[str] = None, description: str = "",
                postcondition: Optional[str] = None, reason: Optional[str] = None,
                extra: Optional[Mapping[str, Any]] = None) -> ActionIntent:
        metadata: dict[str, Any] = {
            "state": self.state.value, "processed_maps": self.processed_maps,
            "success_count": self.success_count, "death_count": self.death_count,
            "map_committed": self.map_committed,
        }
        if reason:
            metadata["reason"] = reason
        if extra:
            metadata.update(extra)
        return ActionIntent(kind=kind, target=target, key=key, description=description,
                            postcondition=postcondition, metadata=metadata)

    def _fail(self, obs: TreasureObservation, reason: str) -> ActionIntent:
        self.last_error = reason
        failed_state = self.state.value
        self._enter(TreasureState.ERROR_STOP, obs.timestamp)
        return self._intent("diagnostic", description=f"capture terminal diagnostic: {reason}",
                            reason=reason,
                            extra={"capture": True, "stop": True, "failed_state": failed_state})

    def _stop(self, state: TreasureState, reason: str) -> ActionIntent:
        self.state = state
        return self._intent("stop", description=reason, reason=reason)

    def _complete(self, reason: str) -> ActionIntent:
        self.state = TreasureState.COMPLETED
        return self._intent("stop", description=reason, reason=reason)


class TreasureMapHandler:
    """Compatibility shell while callers migrate from blocking ``run``."""

    def __init__(self, window_group: Any = None, input_sim: Any = None,
                 screen_mgr: Any = None) -> None:
        self.window_group = window_group
        self.input_sim = input_sim
        self.screen_mgr = screen_mgr
        self.machine = TreasureMapStateMachine()

    def configure(self, max_maps: int = 10,
                  policy: Optional[TreasureMapPolicy] = None) -> None:
        self.machine = TreasureMapStateMachine(max_maps=max_maps, policy=policy)

    def tick(self, observation: Any) -> Optional[ActionIntent]:
        return self.machine.tick(observation)

    def run(self, max_maps: int = 10) -> bool:
        raise RuntimeError("Blocking run() was removed; drive machine.tick(observation) instead")


treasure_map_handler: Optional[TreasureMapHandler] = None

__all__ = ["ActionIntent", "TreasureMapHandler", "TreasureMapPolicy",
           "TreasureMapStateMachine", "TreasureObservation", "TreasureState"]
