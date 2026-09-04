from __future__ import annotations

import json
import time

import numpy as np
import pytest

from core.actions import ActionIntent, ActionKind, SafeActionExecutor
from core.frame_source import LiveWindowFrameSource, ReplayFrameSource
from core.paths import ensure_runtime_dirs, resolve_data_dir
from core.runtime_config import load_runtime_config
from core.windows import ClientRect, WindowBinding
from core import windows as windows_module


class FakeBackend:
    def __init__(self):
        self.calls = []

    def click(self, x, y, *, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))

    def press(self, key):
        self.calls.append(("press", key))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))


class FakeBinding:
    hwnd = 11
    process_id = 12
    dpi = 96
    client_rect = ClientRect(100, 200, 800, 600)

    @property
    def client_size(self):
        return self.client_rect.width, self.client_rect.height

    def refreshed(self):
        return self

    def local_to_screen(self, x, y, reference_size=None):
        if reference_size:
            x *= 2
            y *= 2
        return int(x + 100), int(y + 200)

    def activate(self):
        return True


def live_context(tick_id: int):
    return {
        "capture_timestamp": time.monotonic(),
        "capture_sequence": tick_id,
        "capture_hwnd": FakeBinding.hwnd,
        "capture_process_id": FakeBinding.process_id,
        "capture_client_size": (800, 600),
        "capture_dpi": FakeBinding.dpi,
    }


def test_data_dir_precedence_and_runtime_layout(tmp_path):
    env_dir = tmp_path / "env"
    local = tmp_path / "local"
    explicit = tmp_path / "explicit"
    env = {"MHXY_BOT_DATA_DIR": str(env_dir), "LOCALAPPDATA": str(local)}

    assert resolve_data_dir(explicit, environ=env) == explicit.resolve()
    assert resolve_data_dir(environ=env) == env_dir.resolve()
    assert resolve_data_dir(environ={"LOCALAPPDATA": str(local)}) == (local / "MHXY_Bot").resolve()

    paths = ensure_runtime_dirs(explicit)
    for directory in (paths.root, paths.templates, paths.captures, paths.logs, paths.diagnostics):
        assert directory.is_dir()
    assert paths.config.is_file()


def test_runtime_config_merges_user_override_and_selects_profile(tmp_path):
    override = tmp_path / "override.json"
    override.write_text(
        json.dumps({"treasure_map": {"max_maps": 23}}, ensure_ascii=False),
        encoding="utf-8",
    )

    config = load_runtime_config(override, profile="laptop_1080p")

    assert config["treasure_map"]["max_maps"] == 23
    assert config["treasure_map"]["poll_interval_seconds"] == 0.2
    assert config["profile"] == "laptop_1080p"
    assert config["safety"]["armed"] is False


def test_replay_source_is_deterministic_and_does_not_alias_input():
    original = np.full((4, 5, 3), 17, dtype=np.uint8)
    source = ReplayFrameSource([original], clock=lambda: 42.0)

    frame = source.capture()
    frame.image[0, 0] = 99
    assert frame.timestamp == 42.0
    assert frame.sequence == 1
    assert np.all(original[0, 0] == 17)
    with pytest.raises(StopIteration):
        source.capture()


def test_live_source_captures_exact_bound_client_region_with_fake_backend():
    class CaptureBackend:
        def __init__(self):
            self.regions = []

        def grab(self, region):
            self.regions.append(region)
            return np.zeros((region["height"], region["width"], 4), dtype=np.uint8)

    binding = WindowBinding(
        hwnd=1,
        title="test",
        process_id=2,
        process_name="game.exe",
        client_rect=ClientRect(11, 22, 30, 40),
    )
    backend = CaptureBackend()
    source = LiveWindowFrameSource(
        binding, backend=backend, refresh_binding=False, clock=lambda: 8.5
    )

    frame = source.capture()

    assert backend.regions == [{"left": 11, "top": 22, "width": 30, "height": 40}]
    assert frame.image.shape == (40, 30, 3)
    assert frame.timestamp == 8.5
    assert frame.binding == binding


def test_window_binding_coordinate_conversion_with_reference_size():
    binding = WindowBinding(
        hwnd=1,
        title="test",
        process_id=2,
        process_name="game.exe",
        client_rect=ClientRect(100, 200, 800, 600),
        dpi=120,
    )

    assert binding.local_to_screen(200, 150, reference_size=(400, 300)) == (500, 500)
    assert binding.screen_to_local(500, 500, reference_size=(400, 300)) == (200, 150)
    assert binding.dpi_scale == 1.25


def test_window_discovery_has_safe_non_windows_fallback(monkeypatch):
    monkeypatch.setattr(windows_module, "IS_WINDOWS", False)

    assert windows_module.list_windows() == []
    with pytest.raises(windows_module.WindowBindingError):
        windows_module.bind_game_window(process_names=("game.exe",))


def test_exact_hwnd_still_has_to_match_configured_identity(monkeypatch):
    candidate = WindowBinding(
        hwnd=9,
        title="Unrelated window",
        process_id=10,
        process_name="other.exe",
        client_rect=ClientRect(0, 0, 640, 480),
    )
    monkeypatch.setattr(windows_module, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_module, "_binding_for_hwnd", lambda _hwnd: candidate)

    with pytest.raises(windows_module.WindowBindingError, match="process names"):
        windows_module.bind_game_window(hwnd=9, process_names=("game.exe",))
    with pytest.raises(windows_module.WindowBindingError, match="title"):
        windows_module.bind_game_window(hwnd=9, title_contains="梦幻西游")


def test_refresh_rejects_reused_hwnd_from_another_process(monkeypatch):
    original = WindowBinding(
        hwnd=9,
        title="梦幻西游",
        process_id=10,
        process_name="game.exe",
        client_rect=ClientRect(0, 0, 640, 480),
        expected_process_names=("game.exe",),
        expected_title_contains="梦幻西游",
    )
    reused = WindowBinding(
        hwnd=9,
        title="梦幻西游",
        process_id=11,
        process_name="game.exe",
        client_rect=ClientRect(0, 0, 640, 480),
    )
    monkeypatch.setattr(windows_module, "_binding_for_hwnd", lambda _hwnd: reused)

    with pytest.raises(windows_module.WindowBindingError, match="identity changed"):
        original.refreshed()


def test_window_discovery_refuses_ambiguous_matching_windows(monkeypatch):
    candidates = [
        WindowBinding(
            hwnd=9 + index,
            title="梦幻西游",
            process_id=20 + index,
            process_name="game.exe",
            client_rect=ClientRect(0, 0, 800, 600),
        )
        for index in range(2)
    ]
    monkeypatch.setattr(windows_module, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_module, "list_windows", lambda: candidates)

    with pytest.raises(windows_module.WindowBindingError, match="Multiple game windows") as exc:
        windows_module.bind_game_window(process_names=("game.exe",), title_contains="梦幻")

    assert "HWND=9" in str(exc.value)
    assert "HWND=10" in str(exc.value)


def test_executor_is_dry_run_by_default_and_never_calls_backend():
    backend = FakeBackend()
    executor = SafeActionExecutor(backend=backend)
    executor.arm()
    executor.begin_tick()

    result = executor.execute(ActionIntent(ActionKind.KEY, key="a"))

    assert result.accepted
    assert not result.performed
    assert result.reason == "dry_run"
    assert backend.calls == []


def test_live_executor_requires_arm_blocks_f9_and_allows_one_action_per_tick():
    backend = FakeBackend()
    executor = SafeActionExecutor(dry_run=False, backend=backend, binding=FakeBinding())

    executor.begin_tick()
    assert executor.execute(ActionIntent(ActionKind.KEY, key="a")).reason == "not_armed"
    executor.arm()
    executor.begin_tick()
    assert executor.execute(ActionIntent(ActionKind.KEY, key="f9")).reason == "forbidden_key"
    executor.begin_tick()
    performed = executor.execute(
        ActionIntent(
            ActionKind.CLICK,
            target=(5, 7),
            postcondition="panel_open",
            metadata=live_context(3),
        )
    )
    assert performed.performed
    assert backend.calls == [("click", 105, 207, "left", 1)]

    duplicate = executor.execute(ActionIntent(ActionKind.KEY, key="b"))
    assert duplicate.reason == "one_action_per_tick"
    executor.begin_tick()
    blocked = executor.execute(ActionIntent(ActionKind.KEY, key="b"))
    assert blocked.reason == "postcondition_pending"
    assert executor.confirm_postcondition("wrong") is False
    assert executor.confirm_postcondition("panel_open") is True
    executor.begin_tick()
    assert executor.execute(
        ActionIntent(ActionKind.INTERACT, metadata=live_context(5))
    ).performed
    assert backend.calls[-1] == ("press", "space")


def test_equal_tick_id_cannot_reset_one_action_budget():
    backend = FakeBackend()
    executor = SafeActionExecutor(dry_run=False, backend=backend, binding=FakeBinding())
    executor.arm()
    executor.begin_tick(7)
    assert executor.execute(
        ActionIntent(ActionKind.KEY, key="a", metadata=live_context(7))
    ).performed

    executor.begin_tick(7)
    duplicate = executor.execute(ActionIntent(ActionKind.KEY, key="b"))

    assert duplicate.reason == "one_action_per_tick"
    assert backend.calls == [("press", "a")]


def test_emergency_stop_disarms_and_blocks_future_input():
    backend = FakeBackend()
    executor = SafeActionExecutor(dry_run=False, backend=backend)
    executor.arm()
    executor.emergency_stop()
    executor.begin_tick()

    result = executor.execute(ActionIntent(ActionKind.KEY, key="a"))

    assert result.reason == "emergency_stop"
    assert not executor.armed
    assert backend.calls == []


def test_executor_rejects_out_of_client_click_and_disarms_on_error():
    backend = FakeBackend()
    executor = SafeActionExecutor(dry_run=False, backend=backend, binding=FakeBinding())
    executor.arm()
    executor.begin_tick()

    result = executor.execute(
        ActionIntent(
            ActionKind.CLICK,
            target=(9999, 9999),
            description="unsafe target",
            metadata=live_context(1),
        )
    )

    assert not result.accepted
    assert result.reason.startswith("backend_error:")
    assert not executor.armed
    assert backend.calls == []


def test_executor_aborts_if_window_resizes_after_capture():
    class ResizedBinding:
        hwnd = 7
        process_id = 8
        dpi = 96
        client_rect = ClientRect(100, 200, 900, 600)

        @property
        def client_size(self):
            return self.client_rect.width, self.client_rect.height

        def refreshed(self):
            return self

        def activate(self):
            return True

        def local_to_screen(self, x, y, reference_size=None):
            return 100 + int(x), 200 + int(y)

    backend = FakeBackend()
    executor = SafeActionExecutor(
        dry_run=False,
        backend=backend,
        binding=ResizedBinding(),
        clock=lambda: 10.2,
    )
    executor.arm()
    executor.begin_tick(1)
    result = executor.execute(ActionIntent(
        ActionKind.CLICK,
        target=(5, 7),
        metadata={
            "capture_timestamp": 10.0,
            "capture_sequence": 1,
            "capture_hwnd": 7,
            "capture_process_id": 8,
            "capture_client_size": (800, 600),
            "capture_dpi": 96,
        },
    ))

    assert result.reason.startswith("backend_error:")
    assert "size changed" in result.reason
    assert not executor.armed
    assert backend.calls == []


def test_live_executor_requires_complete_current_capture_context():
    backend = FakeBackend()
    executor = SafeActionExecutor(dry_run=False, backend=backend, binding=FakeBinding())
    executor.arm()
    executor.begin_tick(3)

    result = executor.execute(ActionIntent(ActionKind.KEY, key="a"))

    assert result.reason.startswith("backend_error:")
    assert "complete capture context" in result.reason
    assert executor.faulted
    assert not executor.armed
    assert backend.calls == []


def test_diagnostic_stop_intent_disarms_without_input():
    backend = FakeBackend()
    executor = SafeActionExecutor(dry_run=False, backend=backend)
    executor.arm()
    executor.begin_tick()

    result = executor.execute(
        ActionIntent(ActionKind.DIAGNOSTIC, metadata={"capture": True, "stop": True})
    )

    assert result.reason == "diagnostic_stop"
    assert not executor.armed
    assert backend.calls == []
