# -*- coding: utf-8 -*-
"""Safe single-window treasure-map runtime.

The runtime is observation driven: capture one frame, let the state machine
produce at most one intent, then let the executor perform at most one input.
Dry-run is the default for both the Python API and command line.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Mapping, Optional

from config.runtime import (
    REQUIRED_TEMPLATES,
    ensure_data_layout,
    load_runtime_config,
    load_template_manifest,
    validate_template_profile,
)
from core.actions import ActionIntent, ActionKind, ActionResult, SafeActionExecutor
from core.detection import DetectionResult, TemplateDetector, TemplateManifest
from core.frame_source import CapturedFrame, FrameSource, LiveWindowFrameSource, ReplayFrameSource, save_frame
from core.windows import WindowBinding, WindowBindingError, enable_dpi_awareness
from modules.tasks import TreasureMapPolicy, TreasureMapStateMachine, TreasureObservation


logger = logging.getLogger(__name__)


def configure_logging(log_dir: Path, *, level: int = logging.INFO) -> Path:
    """Configure console/file logging without writing anything on import."""

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "mhxy_bot.log"
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    if not any(getattr(handler, "_mhxy_console", False) for handler in root.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        console._mhxy_console = True  # type: ignore[attr-defined]
        root.addHandler(console)
    if not any(getattr(handler, "_mhxy_file", None) == str(log_path) for handler in root.handlers):
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler._mhxy_file = str(log_path)  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    return log_path


def _kind_value(intent: ActionIntent) -> str:
    return intent.kind.value if isinstance(intent.kind, ActionKind) else str(intent.kind)


class AutoBot:
    """Drive the first supported workflow: one treasure-map game window."""

    def __init__(
        self,
        enable_ui: bool = False,
        *,
        dry_run: bool = True,
        armed: bool = False,
        config_path: Optional[str] = None,
        profile: Optional[str] = None,
        data_dir: Optional[str] = None,
        max_maps: Optional[int] = None,
        replay_dir: Optional[str] = None,
        frame_source: Optional[FrameSource] = None,
        detector: Optional[TemplateDetector] = None,
        executor: Optional[SafeActionExecutor] = None,
        clock: Any = time.monotonic,
        register_hotkeys: bool = True,
    ) -> None:
        del enable_ui  # retained for compatibility with the old launcher API
        self.paths = ensure_data_layout(data_dir)
        configure_logging(self.paths.logs)
        self.config = load_runtime_config(config_path=config_path, data_dir=self.paths.root)
        self.profile = profile or str(self.config.get("profile", "default"))
        self.dry_run = bool(dry_run or replay_dir)
        if not self.dry_run and not armed:
            raise RuntimeError("Live mode requires explicit per-run arming")
        self.current_mode = "idle"
        self.running = True
        self._paused = False
        self._clock = clock
        self._last_frame: Optional[CapturedFrame] = None
        self._last_observation: Optional[TreasureObservation] = None
        self.last_action: Optional[ActionResult] = None
        self._pause_hotkey: Any = None
        self._presence_streaks: dict[str, int] = {}
        self._absence_streaks: dict[str, int] = {}
        self._control_lock = threading.RLock()
        self._rearm_after_pause = False
        self._pause_requested = threading.Event()
        self._stop_requested = threading.Event()

        task_config = self.config.get("treasure_map", {})
        recovery_config = self.config.get("recovery", {})
        policy = TreasureMapPolicy(
            stable_frames=int(recovery_config.get("stable_frames", 3)),
            max_retries=int(task_config.get("max_action_retries", 3)),
            death_timeout=float(recovery_config.get("death_scene_timeout_seconds", 60)),
        )
        self.machine = TreasureMapStateMachine(
            max_maps=int(max_maps if max_maps is not None else task_config.get("max_maps", 10)),
            policy=policy,
            clock=clock,
        )
        self.poll_interval = max(0.05, float(task_config.get("poll_interval_seconds", 0.2)))
        self.inventory_roi = tuple(task_config.get("inventory_roi", (0.4, 0.15, 0.6, 0.8)))
        self.inventory_signature_enabled = bool(task_config.get("inventory_signature_enabled", False))

        self.binding: Optional[WindowBinding] = None
        if frame_source is None:
            if replay_dir:
                frame_source = ReplayFrameSource.from_directory(replay_dir)
            else:
                window_config = self.config.get("window", {})
                preferred = window_config.get("preferred_hwnd")
                self.binding = WindowBinding.bind(
                    int(preferred) if preferred not in (None, "") else None,
                    process_names=tuple(window_config.get("process_names", ())),
                    title_contains=window_config.get("title_contains") or None,
                )
                frame_source = LiveWindowFrameSource(self.binding)
        else:
            self.binding = getattr(frame_source, "binding", None)
        self.frame_source = frame_source

        if not self.dry_run:
            if not isinstance(self.frame_source, LiveWindowFrameSource) or self.binding is None:
                self.frame_source.close()
                raise RuntimeError(
                    "Live mode requires a bound LiveWindowFrameSource; replay input is dry-run only"
                )
            if detector is not None:
                self.frame_source.close()
                raise RuntimeError("Live mode cannot use an injected detector")

        manifest_data, _manifest_path = load_template_manifest(self.paths.root, self.profile)
        private_template_dir = self.paths.profile_templates(self.profile)
        self.manifest = TemplateManifest.from_dict(manifest_data, base_dir=private_template_dir)
        self.detector = detector or TemplateDetector(self.manifest)
        self.detection_names = self._available_detection_names()

        if not self.dry_run:
            status = validate_template_profile(
                self.paths.root,
                self.profile,
                client_size=self.binding.client_size if self.binding else None,
                dpi=self.binding.dpi if self.binding else None,
            )
            if not status.ready:
                details = "\n - ".join(status.errors)
                self.frame_source.close()
                raise RuntimeError(f"Live mode is blocked until calibration is complete:\n - {details}")

        if executor is not None and bool(executor.dry_run) != self.dry_run:
            raise ValueError("Injected executor dry_run mode must match AutoBot dry_run mode")
        self.executor = executor or SafeActionExecutor(
            dry_run=self.dry_run,
            binding=self.binding,
            forbidden_keys=("f9",),
        )
        self.executor.disarm()
        if not self.dry_run:
            executor_binding = self.executor.binding
            if executor_binding is None or self.binding is None:
                self.frame_source.close()
                raise RuntimeError("Live executor must use the captured game-window binding")
            if (int(executor_binding.hwnd) != int(self.binding.hwnd)
                    or int(executor_binding.process_id) != int(self.binding.process_id)):
                self.frame_source.close()
                raise RuntimeError("Live executor binding does not match the capture HWND/process")

        emergency_registered = False
        if register_hotkeys:
            safety_config = self.config.get("safety", {})
            emergency_registered = self.executor.register_emergency_hotkey(
                str(safety_config.get("emergency_stop_key", "f11"))
            )
            self._register_pause_hotkey(str(safety_config.get("pause_key", "f12")))
        if not self.dry_run and not emergency_registered:
            self._unregister_pause_hotkey()
            self.frame_source.close()
            raise RuntimeError(
                "Live mode is blocked because the F11 emergency hotkey could not be registered"
            )
        if not self.dry_run and armed:
            self.executor.arm()

        commit_sha = os.environ.get("MHXY_BOT_GIT_SHA") or os.environ.get("MHXY_BOT_COMMIT", "unknown")
        logger.info(
            "Runtime ready: mode=treasure_map dry_run=%s profile=%s commit=%s data=%s",
            self.dry_run,
            self.profile,
            commit_sha,
            self.paths.root,
        )

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        value = bool(value)
        if value:
            # This event and executor disarm are intentionally outside the
            # longer capture/decision transaction lock, so a control request
            # can veto an action that has not started yet.
            self._pause_requested.set()
            was_armed = self.executor.armed
            self.executor.pause()
        with self._control_lock:
            if value == self._paused:
                return
            if value:
                self._rearm_after_pause = (
                    was_armed and not self.executor.faulted and not self.executor.stopped
                )
                self.machine.suspend(self._clock())
                # Publish paused only after input has been disarmed.
                self._paused = True
                logger.warning("Automation paused")
                return
            if self.executor.stopped:
                logger.warning("Automation cannot resume after emergency stop")
                return
            self.executor.resume()
            self.machine.resume_suspended(self._clock())
            if self.machine.state.value == "captcha_pause":
                self.machine.resume()
            if not self.dry_run and self._rearm_after_pause:
                # Restore only authority that existed before this pause. A
                # backend fault/disarm can never be turned into a fresh arm.
                self.executor.arm()
            self._rearm_after_pause = False
            self._pause_requested.clear()
            # Publish resumed only after executor state/timers are restored.
            self._paused = False
            logger.info("Automation resumed")

    def set_paused(self, paused: bool) -> None:
        """Thread-safe launcher-facing pause API."""

        self.paused = paused

    def stop(self) -> None:
        """Stop promptly and disarm input; safe to call from launcher controls."""

        self._stop_requested.set()
        self.executor.emergency_stop()
        with self._control_lock:
            self.running = False

    def _register_pause_hotkey(self, key: str) -> None:
        try:
            import keyboard

            self._pause_hotkey = keyboard.add_hotkey(key, lambda: setattr(self, "paused", not self.paused))
        except Exception as exc:
            logger.warning("Could not register pause hotkey %s: %s", key, exc)

    def _unregister_pause_hotkey(self) -> None:
        if self._pause_hotkey is None:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self._pause_hotkey)
        except Exception:
            logger.debug("Could not unregister pause hotkey", exc_info=True)
        finally:
            self._pause_hotkey = None

    def _available_detection_names(self) -> tuple[str, ...]:
        names = []
        for name, rule in self.manifest.rules.items():
            if rule.calibrated and self.manifest.template_path(name).is_file():
                names.append(name)
        missing = sorted(set(REQUIRED_TEMPLATES) - set(names))
        if missing:
            logger.warning("Uncalibrated/missing templates: %s", ", ".join(missing))
        return tuple(names)

    def _inventory_signature(self, frame: CapturedFrame, backpack_visible: bool) -> Optional[str]:
        del frame, backpack_visible
        # Whole-panel hashes react to hover/selection animation and can falsely
        # count a map as consumed. This evidence source stays disabled until a
        # slot/count-specific ROI can be calibrated from real-game captures.
        return None

    def _observe(self, frame: CapturedFrame) -> TreasureObservation:
        detections: dict[str, DetectionResult] = {}
        if self.detection_names:
            detections = self.detector.detect_many(
                frame, self.detection_names, require_consecutive=False
            )
        for name in set(self.detection_names) | set(detections):
            if name in detections:
                self._presence_streaks[name] = self._presence_streaks.get(name, 0) + 1
                self._absence_streaks[name] = 0
            else:
                self._presence_streaks[name] = 0
                self._absence_streaks[name] = self._absence_streaks.get(name, 0) + 1
        if "active_treasure_task" in detections or "quest_target_marker" in detections:
            task_active: Optional[bool] = True
        elif "no_active_treasure_task" in detections:
            task_active = False
        else:
            task_active = None
        return TreasureObservation(
            timestamp=float(frame.timestamp),
            detections=detections,
            inventory_signature=self._inventory_signature(frame, "backpack_open" in detections),
            task_active=task_active,
            metadata={"sequence": frame.sequence, "frame_size": frame.size},
        )

    def _postcondition_satisfied(self, name: str, obs: TreasureObservation) -> bool:
        visible = set(obs.detections)
        stable_frames = self.machine.policy.stable_frames

        def present(signal: str) -> bool:
            return signal in visible and self._presence_streaks.get(signal, 0) >= stable_frames

        def absent(signal: str) -> bool:
            return signal not in visible and self._absence_streaks.get(signal, 0) >= stable_frames

        if present(name):
            return True
        if name == "map_consumed_or_marker_visible":
            return any(present(signal) for signal in (
                "map_consumed", "quest_target_marker", "active_treasure_task"
            )) or (present("backpack_open") and absent("treasure_map_icon"))
        if name == "reward_or_combat_or_death":
            return any(present(signal) for signal in (
                "reward_dialog", "reward_confirm_button", "combat_hud_anchor",
                "death_return_scene_anchor", "dig_success", "dig_failed"
            ))
        if name == "map_panel_closed":
            return absent("map_panel_anchor")
        if name == "backpack_closed":
            return absent("backpack_open")
        if name == "task_panel_closed":
            return absent("task_panel_anchor")
        if name == "reward_dialog_closed":
            return absent("reward_dialog") and absent("reward_confirm_button")
        return False

    @staticmethod
    def _is_input_intent(intent: ActionIntent) -> bool:
        return intent.kind in {
            ActionKind.CLICK,
            ActionKind.DOUBLE_CLICK,
            ActionKind.KEY,
            ActionKind.HOTKEY,
            ActionKind.INTERACT,
        }

    def _attach_capture_context(self, intent: ActionIntent, frame: CapturedFrame) -> ActionIntent:
        metadata = dict(intent.metadata)
        metadata.setdefault("reference_size", frame.size)
        metadata["capture_sequence"] = frame.sequence
        metadata["capture_timestamp"] = frame.timestamp
        binding = frame.binding
        if binding is not None:
            metadata["capture_hwnd"] = binding.hwnd
            metadata["capture_process_id"] = binding.process_id
            metadata["capture_client_size"] = binding.client_size
            metadata["capture_dpi"] = binding.dpi
        return replace(intent, metadata=metadata)

    def _save_diagnostic(self, frame: CapturedFrame, intent: Optional[ActionIntent], reason: str) -> Path:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"diagnostic-{timestamp}-{frame.sequence}"
        image_path = save_frame(frame, self.paths.diagnostics / f"{stem}.png")
        payload = {
            "reason": reason,
            "state": self.machine.snapshot(),
            "frame": {"sequence": frame.sequence, "timestamp": frame.timestamp, "size": frame.size},
            "intent": None if intent is None else {
                "kind": _kind_value(intent),
                "description": intent.description,
                "metadata": dict(intent.metadata),
            },
        }
        (self.paths.diagnostics / f"{stem}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.error("Diagnostic saved: %s", image_path)
        return image_path

    def tick_once(self) -> Optional[ActionResult]:
        """Capture, decide and execute at most one intent."""

        with self._control_lock:
            if self._paused or self.executor.stopped:
                return None
            return self._tick_once_locked()

    def _tick_once_locked(self) -> Optional[ActionResult]:
        """Run one transaction while pause/resume controls are serialized."""

        if self._pause_requested.is_set() or self._stop_requested.is_set() or self.executor.stopped:
            return None
        frame = self.frame_source.capture()
        self._last_frame = frame
        observation = self._observe(frame)
        self._last_observation = observation
        if self._pause_requested.is_set() or self._stop_requested.is_set() or self.executor.stopped:
            return None

        pending = self.executor.pending_postcondition
        if pending and self._postcondition_satisfied(pending.name, observation):
            self.executor.confirm_postcondition(pending.name)
        elif pending:
            stable_frames = self.machine.policy.stable_frames
            stable_death = (
                "death_return_scene_anchor" in observation.detections
                and self._presence_streaks.get("death_return_scene_anchor", 0) >= stable_frames
            )
            stable_combat = (
                "combat_hud_anchor" in observation.detections
                and self._presence_streaks.get("combat_hud_anchor", 0) >= stable_frames
            )
            stable_captcha = (
                "captcha_dialog" in observation.detections
                and self._presence_streaks.get("captcha_dialog", 0) >= stable_frames
            )
            if stable_death or stable_captcha or stable_combat:
                logger.info(
                    "Invalidating pending postcondition %s after verified UI preemption",
                    pending.name,
                )
                self.executor.abandon_postcondition(pending.name)

        intent = self.machine.tick(observation)
        if intent is not None and self._is_input_intent(intent) and (
            self._pause_requested.is_set()
            or self._stop_requested.is_set()
            or self.executor.stopped
        ):
            self.machine.cancel_unexecuted_action(intent, observation.timestamp)
            return None
        self.executor.begin_tick(frame.sequence)
        if intent is None:
            return None

        intent = self._attach_capture_context(intent, frame)

        pending = self.executor.pending_postcondition
        same_action = False
        supersedes = set(intent.metadata.get("supersedes_postconditions", ()))
        if pending is not None and pending.name in supersedes:
            self.executor.abandon_postcondition(pending.name)
            pending = None
        if pending is not None and self._is_input_intent(intent):
            same_action = bool(
                intent.metadata.get("action_id")
                and intent.metadata.get("action_id") == pending.intent.metadata.get("action_id")
                and intent.postcondition == pending.name
            )
            retry_attempt = int(intent.metadata.get("attempt", 1))
            if same_action and retry_attempt > 1:
                self.executor.abandon_postcondition(pending.name)
        if _kind_value(intent) == ActionKind.PAUSE.value:
            # Preserve whether live authority existed before the executor's
            # PAUSE control action disarms itself.
            self.paused = True
        result = self.executor.execute(intent)
        self.last_action = result
        if not result.accepted and result.reason in {"paused", "emergency_stop"}:
            self.machine.cancel_unexecuted_action(intent, observation.timestamp)
            if result.reason == "emergency_stop":
                self.running = False
            logger.info("Action cancelled by user control: %s", result.reason)
            return result
        self.machine.acknowledge_action(
            intent,
            accepted=result.accepted and (self.dry_run or result.performed),
            timestamp=observation.timestamp,
            reason=result.reason,
        )
        logger.info(
            "Action: kind=%s accepted=%s performed=%s reason=%s state=%s",
            _kind_value(intent), result.accepted, result.performed, result.reason,
            self.machine.state.value,
        )

        if _kind_value(intent) == ActionKind.DIAGNOSTIC.value or intent.metadata.get("capture"):
            self._save_diagnostic(frame, intent, str(intent.metadata.get("reason", intent.description)))
        if _kind_value(intent) == ActionKind.STOP.value or intent.metadata.get("stop"):
            self.running = False
        if not result.accepted and self._is_input_intent(intent):
            if result.reason != "postcondition_pending" or not same_action:
                reason = f"executor_rejected:{result.reason}"
                self.machine.abort(reason, observation.timestamp)
                self.executor.disarm()
                self.running = False
                self._save_diagnostic(frame, intent, reason)
        return result

    def run(
        self,
        mode: str = "treasure_map",
        enable_ui: bool = False,
        max_maps: Optional[int] = None,
    ) -> None:
        del enable_ui
        if mode != "treasure_map":
            raise ValueError("The first release only supports treasure_map")
        if max_maps is not None and int(max_maps) != self.machine.max_maps:
            if self.machine.state.value != "ready":
                raise RuntimeError("max_maps can only be changed before the workflow starts")
            self.machine = TreasureMapStateMachine(
                max_maps=int(max_maps), policy=self.machine.policy, clock=self._clock
            )
        self.current_mode = mode
        logger.info("Treasure-map loop started")
        try:
            while self.running and not self.machine.stopped:
                if self.executor.stopped:
                    logger.warning("Executor emergency stop detected")
                    self.running = False
                    break
                if self.paused:
                    time.sleep(min(self.poll_interval, 0.2))
                    continue
                try:
                    self.tick_once()
                except StopIteration:
                    logger.info("Replay completed")
                    self.running = False
                    break
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            logger.info("Interrupted")
        except Exception as exc:
            logger.exception("Runtime stopped by error: %s", exc)
            if self._last_frame is not None:
                self._save_diagnostic(self._last_frame, None, f"runtime_error:{exc}")
            self.running = False
            raise
        finally:
            self.close()

    def close(self) -> None:
        self.running = False
        self.executor.disarm()
        self.executor.unregister_emergency_hotkey()
        self._unregister_pause_hotkey()
        self.frame_source.close()
        logger.info("Runtime closed: %s", self.machine.snapshot())

    def status(self) -> Mapping[str, Any]:
        return {
            **self.machine.snapshot(),
            "running": self.running,
            "paused": self.paused,
            "dry_run": self.dry_run,
            "armed": self.executor.armed,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MHXY safe treasure-map assistant")
    parser.add_argument("mode", nargs="?", default="treasure_map", choices=("treasure_map",))
    parser.add_argument("--arm", action="store_true", help="enable live input for this run only")
    parser.add_argument("--config", help="machine-local config.json")
    parser.add_argument("--data-dir", help="override private runtime directory")
    parser.add_argument("--profile", help="private calibration profile (defaults to config.json)")
    parser.add_argument("--replay", help="directory containing replay screenshots (always dry-run)")
    parser.add_argument("--ui", action="store_true", help="open the graphical launcher")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.ui:
        from launcher import main as launcher_main

        launcher_args: list[str] = []
        if args.data_dir:
            launcher_args.extend(("--data-dir", args.data_dir))
        if args.config:
            launcher_args.extend(("--config", args.config))
        if args.profile:
            launcher_args.extend(("--profile", args.profile))
        return int(launcher_main(launcher_args) or 0)
    try:
        enable_dpi_awareness()
        bot = AutoBot(
            dry_run=not args.arm,
            armed=args.arm,
            config_path=args.config,
            profile=args.profile,
            data_dir=args.data_dir,
            replay_dir=args.replay,
        )
        bot.run(args.mode)
        return 0 if bot.machine.state.value in {"completed", "captcha_pause"} else 1
    except (RuntimeError, ValueError, FileNotFoundError, WindowBindingError) as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
