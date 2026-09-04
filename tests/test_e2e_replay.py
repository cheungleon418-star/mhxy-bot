from __future__ import annotations

import json

import cv2
import numpy as np

from config.runtime import ensure_data_layout
from core.actions import SafeActionExecutor
from core.frame_source import CapturedFrame, ReplayFrameSource
from main import AutoBot
from modules.tasks import TreasureState


class RejectRecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def click(self, *args, **kwargs) -> None:
        self.calls.append(("click", args, kwargs))

    def press(self, *args, **kwargs) -> None:
        self.calls.append(("press", args, kwargs))

    def hotkey(self, *args, **kwargs) -> None:
        self.calls.append(("hotkey", args, kwargs))


def test_synthetic_images_replay_through_detector_state_and_dry_executor(tmp_path):
    """Exercise the full happy path without shipping or reading game images."""

    paths = ensure_data_layout(tmp_path / "private")
    profile_dir = paths.profile_templates("default")
    profile_dir.mkdir(parents=True, exist_ok=True)
    signal_names = (
        "world_hud_anchor",
        "backpack_open",
        "treasure_map_icon",
        "map_panel_anchor",
        "quest_target_marker",
        "dig_interact_prompt",
        "reward_dialog",
        "reward_confirm_button",
        "task_panel_anchor",
    )
    rng = np.random.default_rng(20260904)
    templates: dict[str, np.ndarray] = {}
    rules = {}
    for name in signal_names:
        template = rng.integers(1, 255, size=(7, 7, 3), dtype=np.uint8)
        templates[name] = template
        filename = f"{name}.png"
        assert cv2.imwrite(str(profile_dir / filename), template)
        rules[name] = {
            "file": filename,
            "calibrated": True,
            "threshold": 0.999,
            "roi": [0.0, 0.0, 1.0, 1.0],
            "scales": [1.0],
            "stable_frames": 1,
        }
    (profile_dir / "manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "profile": "default",
            "coordinate_space": "normalized_client",
            "client_size": [128, 96],
            "dpi": 96,
            "templates": rules,
        }),
        encoding="utf-8",
    )

    positions = {
        name: (3 + (index % 4) * 28, 3 + (index // 4) * 28)
        for index, name in enumerate(signal_names)
    }

    def image_with(*signals: str) -> np.ndarray:
        image = np.zeros((96, 128, 3), dtype=np.uint8)
        for signal in signals:
            x, y = positions[signal]
            image[y : y + 7, x : x + 7] = templates[signal]
        return image

    phases = [
        (("world_hud_anchor",), 3),
        (("world_hud_anchor", "backpack_open", "treasure_map_icon"), 4),
        (("world_hud_anchor", "backpack_open"), 3),
        (("world_hud_anchor",), 3),
        (("world_hud_anchor", "map_panel_anchor", "quest_target_marker"), 4),
        (("world_hud_anchor", "dig_interact_prompt"), 4),
        (("world_hud_anchor", "reward_dialog", "reward_confirm_button"), 3),
        (("world_hud_anchor",), 3),
    ]
    frames: list[CapturedFrame] = []
    sequence = 0
    for signals, count in phases:
        for _ in range(count):
            sequence += 1
            frames.append(CapturedFrame(
                image=image_with(*signals),
                timestamp=sequence * 0.25,
                sequence=sequence,
            ))

    backend = RejectRecordingBackend()
    executor = SafeActionExecutor(dry_run=True, backend=backend)
    bot = AutoBot(
        dry_run=True,
        data_dir=str(paths.root),
        max_maps=1,
        frame_source=ReplayFrameSource(frames),
        executor=executor,
        register_hotkeys=False,
    )

    while not bot.machine.stopped:
        bot.tick_once()

    assert bot.machine.state is TreasureState.COMPLETED
    assert bot.machine.processed_maps == 1
    assert bot.machine.success_count == 1
    assert bot.machine.death_count == 0
    assert backend.calls == []
    assert all(
        getattr(result.intent, "key", None) != "f9"
        for result in [bot.last_action]
        if result is not None
    )
    bot.close()
