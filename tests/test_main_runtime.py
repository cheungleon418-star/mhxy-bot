from __future__ import annotations

import numpy as np
import pytest
import threading
import types
import sys

from core.actions import SafeActionExecutor
from core.frame_source import ReplayFrameSource
import main as main_module
from main import AutoBot
from core.actions import ActionIntent, ActionKind
from modules.tasks import TreasureState


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def click(self, x, y, *, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))


class SequenceDetector:
    def __init__(self, detections):
        self._detections = list(detections)

    def detect_many(self, frame, names, **_kwargs):
        index = min(frame.sequence - 1, len(self._detections) - 1)
        return dict(self._detections[index])


def blank_frames(count: int):
    return [np.zeros((80, 120, 3), dtype=np.uint8) for _ in range(count)]


def test_integrated_tick_is_dry_run_and_emits_no_os_input(tmp_path):
    backend = RecordingBackend()
    executor = SafeActionExecutor(dry_run=True, backend=backend)
    detector = SequenceDetector([
        {"world_hud_anchor": True},
        {"world_hud_anchor": True},
        {"world_hud_anchor": True},
    ])
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=ReplayFrameSource(blank_frames(3)),
        detector=detector,
        executor=executor,
        register_hotkeys=False,
    )
    bot.detection_names = ("world_hud_anchor",)

    assert bot.tick_once() is None
    assert bot.tick_once() is None
    result = bot.tick_once()

    assert result is not None
    assert result.intent.key == "i"
    assert result.reason == "dry_run"
    assert backend.calls == []
    assert result.intent.key != "f9"
    bot.close()


def test_live_mode_requires_explicit_arm_even_with_injected_frames(tmp_path):
    with pytest.raises(RuntimeError, match="explicit per-run arming"):
        AutoBot(
            dry_run=False,
            armed=False,
            data_dir=str(tmp_path),
            frame_source=ReplayFrameSource(blank_frames(1)),
            register_hotkeys=False,
        )


def test_armed_live_mode_rejects_replay_frame_source(tmp_path):
    with pytest.raises(RuntimeError, match="LiveWindowFrameSource"):
        AutoBot(
            dry_run=False,
            armed=True,
            data_dir=str(tmp_path),
            frame_source=ReplayFrameSource(blank_frames(1)),
            register_hotkeys=False,
        )


def test_injected_executor_cannot_bypass_dry_run(tmp_path):
    executor = SafeActionExecutor(dry_run=False, backend=RecordingBackend())
    executor.arm()
    with pytest.raises(ValueError, match="must match"):
        AutoBot(
            dry_run=True,
            data_dir=str(tmp_path),
            frame_source=ReplayFrameSource(blank_frames(1)),
            executor=executor,
            register_hotkeys=False,
        )


def test_named_pause_and_stop_controls_are_safe(tmp_path):
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=ReplayFrameSource(blank_frames(1)),
        detector=SequenceDetector([{}]),
        register_hotkeys=False,
    )
    bot.set_paused(True)
    assert bot.paused is True
    bot.set_paused(False)
    assert bot.paused is False
    assert bot.executor.paused is False
    bot.stop()
    assert bot.running is False
    assert bot.executor.stopped is True
    bot.close()


def test_negative_postcondition_requires_stable_absence(tmp_path):
    detector = SequenceDetector([
        {"backpack_open": True},
        {"backpack_open": True},
        {"backpack_open": True},
        {},
        {},
        {},
    ])
    source = ReplayFrameSource(blank_frames(6))
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=source,
        detector=detector,
        register_hotkeys=False,
    )
    bot.detection_names = ("backpack_open",)
    observations = [bot._observe(source.capture()) for _ in range(3)]
    assert bot._postcondition_satisfied("backpack_closed", observations[-1]) is False

    first_miss = bot._observe(source.capture())
    assert bot._postcondition_satisfied("backpack_closed", first_miss) is False
    bot._observe(source.capture())
    third_miss = bot._observe(source.capture())
    assert bot._postcondition_satisfied("backpack_closed", third_miss) is True
    bot.close()


def test_ui_cli_arguments_are_forwarded_without_ui_flag(monkeypatch, tmp_path):
    received = []
    fake_launcher = types.SimpleNamespace(main=lambda argv: received.extend(argv) or 0)
    monkeypatch.setitem(sys.modules, "launcher", fake_launcher)

    result = main_module.main([
        "--ui", "--data-dir", str(tmp_path), "--profile", "pc_test"
    ])

    assert result == 0
    assert received == ["--data-dir", str(tmp_path), "--profile", "pc_test"]


def test_different_input_cannot_abandon_unconfirmed_postcondition(tmp_path):
    source = ReplayFrameSource(blank_frames(2))
    source.capture()  # make AutoBot's first transaction use a newer tick id
    executor = SafeActionExecutor(dry_run=True, backend=RecordingBackend())
    executor.begin_tick(1)
    executor.execute(ActionIntent(
        ActionKind.KEY,
        key="i",
        postcondition="never_confirmed",
        metadata={"action_id": "first_action", "attempt": 1},
    ))
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=source,
        detector=SequenceDetector([{}, {}]),
        executor=executor,
        register_hotkeys=False,
    )
    bot.machine.tick = lambda _obs: ActionIntent(
        ActionKind.KEY,
        key="m",
        postcondition="map_panel_anchor",
        metadata={"action_id": "different_action", "attempt": 1},
    )

    result = bot.tick_once()

    assert result is not None
    assert result.reason == "postcondition_pending"
    assert bot.machine.state is TreasureState.ERROR_STOP
    assert bot.running is False
    assert executor.pending_postcondition is not None
    bot.close()


def test_icon_disappearance_confirms_use_gate_before_close_action(tmp_path):
    source = ReplayFrameSource(blank_frames(4))
    source.capture()
    executor = SafeActionExecutor(dry_run=True, backend=RecordingBackend())
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=source,
        detector=SequenceDetector([
            {"backpack_open": True},
            {"backpack_open": True},
            {"backpack_open": True},
            {"backpack_open": True},
        ]),
        executor=executor,
        register_hotkeys=False,
    )
    bot.detection_names = ("backpack_open", "treasure_map_icon")
    bot.machine._enter(TreasureState.VERIFY_COMMITTED, 0.0)
    bot.machine._pre_use_icon_visible = True
    bot.machine._map_use_accepted = True
    executor.begin_tick(1)
    executor.execute(ActionIntent(
        ActionKind.CLICK,
        target=(1, 1),
        postcondition="map_consumed_or_marker_visible",
        metadata={"action_id": "use_treasure_map", "attempt": 1},
    ))

    assert bot.tick_once() is None
    assert bot.tick_once() is None
    result = bot.tick_once()

    assert result is not None
    assert result.accepted
    assert result.intent.key == "escape"
    assert bot.machine.processed_maps == 1
    assert bot.machine.state is TreasureState.OPEN_MAP
    bot.close()


def test_stable_death_preemption_invalidates_navigation_gate(tmp_path):
    source = ReplayFrameSource(blank_frames(4))
    source.capture()
    executor = SafeActionExecutor(dry_run=True, backend=RecordingBackend())
    detector = SequenceDetector([
        {"world_hud_anchor": True, "death_return_scene_anchor": True},
        {"world_hud_anchor": True, "death_return_scene_anchor": True},
        {"world_hud_anchor": True, "death_return_scene_anchor": True},
        {"world_hud_anchor": True, "death_return_scene_anchor": True},
    ])
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=source,
        detector=detector,
        executor=executor,
        register_hotkeys=False,
    )
    bot.detection_names = ("world_hud_anchor", "death_return_scene_anchor")
    bot.machine._enter(TreasureState.NAVIGATE, 0.0)
    executor.begin_tick(1)
    executor.execute(ActionIntent(
        ActionKind.CLICK,
        target=(1, 1),
        postcondition="dig_interact_prompt",
        metadata={"action_id": "navigate_to_marker", "attempt": 1},
    ))

    bot.tick_once()
    bot.tick_once()
    result = bot.tick_once()

    assert result is None
    assert bot.machine.state is TreasureState.DEATH_SCENE_WAIT
    assert executor.pending_postcondition is None
    assert bot.running is True
    bot.close()


def test_pause_request_during_detection_vetoes_the_pending_tick(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingDetector:
        def detect_many(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(5)
            return {"world_hud_anchor": True}

    backend = RecordingBackend()
    bot = AutoBot(
        dry_run=True,
        data_dir=str(tmp_path),
        frame_source=ReplayFrameSource(blank_frames(1)),
        detector=BlockingDetector(),
        executor=SafeActionExecutor(dry_run=True, backend=backend),
        register_hotkeys=False,
    )
    bot.detection_names = ("world_hud_anchor",)
    tick_result: list[object] = []
    tick_thread = threading.Thread(target=lambda: tick_result.append(bot.tick_once()))
    tick_thread.start()
    assert entered.wait(5)

    pause_thread = threading.Thread(target=lambda: bot.set_paused(True))
    pause_thread.start()
    assert bot._pause_requested.wait(5)
    release.set()
    tick_thread.join(5)
    pause_thread.join(5)

    assert not tick_thread.is_alive()
    assert not pause_thread.is_alive()
    assert tick_result == [None]
    assert bot.machine.state is TreasureState.READY
    assert bot.paused is True
    assert backend.calls == []
    bot.close()
