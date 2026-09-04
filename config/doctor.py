"""Read-only environment diagnostics and opt-in game-window capture CLI."""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import struct
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .runtime import (
    ConfigError,
    REQUIRED_TEMPLATES,
    ensure_data_layout,
    load_runtime_config,
    runtime_paths,
    validate_profile_name,
    validate_template_profile,
)


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass(frozen=True)
class DiagnosticReport:
    checks: tuple[DiagnosticCheck, ...]
    binding: dict[str, Any] | None = None
    capture_path: str | None = None

    @property
    def ok(self) -> bool:
        return all(item.ok or not item.blocking for item in self.checks)


def _check_imports(modules: Iterable[str]) -> DiagnosticCheck:
    missing: list[str] = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:  # import failures are the diagnostic result
            missing.append(f"{module} ({type(exc).__name__})")
    if missing:
        return DiagnosticCheck("Python 依赖", False, "缺少或无法加载：" + ", ".join(missing))
    return DiagnosticCheck("Python 依赖", True, "基础依赖均可导入")


def _check_emergency_hotkey(hotkey: str, *, blocking: bool) -> DiagnosticCheck:
    """Register and immediately remove the emergency hotkey without input."""

    registration: Any = None
    keyboard_module: Any = None
    try:
        keyboard_module = importlib.import_module("keyboard")
        registration = keyboard_module.add_hotkey(
            hotkey,
            lambda: None,
            suppress=False,
        )
        keyboard_module.remove_hotkey(registration)
        registration = None
    except Exception as exc:
        if registration is not None and keyboard_module is not None:
            try:
                keyboard_module.remove_hotkey(registration)
            except Exception:
                pass
        return DiagnosticCheck(
            "紧急停止热键",
            False,
            f"无法注册 {hotkey!r}：{type(exc).__name__}: {exc}",
            blocking=blocking,
        )
    return DiagnosticCheck("紧急停止热键", True, f"{hotkey!r} 可注册并已立即移除")


def _check_calibration(
    *,
    data_root: Path,
    profile: str,
    blocking: bool,
    client_size: tuple[int, int] | None = None,
    dpi: int | None = None,
) -> DiagnosticCheck:
    try:
        if client_size is None or dpi is None:
            calibration = validate_template_profile(data_root, profile)
        else:
            calibration = validate_template_profile(
                data_root,
                profile,
                client_size=client_size,
                dpi=dpi,
            )
        detail = (
            f"{len(calibration.calibrated)}/{len(REQUIRED_TEMPLATES)} 个必需模板已校准"
            if calibration.ready
            else f"{len(calibration.calibrated)}/{len(REQUIRED_TEMPLATES)}；"
            + "；".join(calibration.errors)
        )
        return DiagnosticCheck("模板校准", calibration.ready, detail, blocking=blocking)
    except (ConfigError, OSError) as exc:
        return DiagnosticCheck(
            "模板校准",
            False,
            f"{type(exc).__name__}: {exc}",
            blocking=blocking,
        )


def _binding_as_dict(binding: Any) -> dict[str, Any]:
    rect = binding.client_rect
    return {
        "hwnd": binding.hwnd,
        "title": binding.title,
        "process_id": binding.process_id,
        "process_name": binding.process_name,
        "client_rect": {
            "left": rect.left,
            "top": rect.top,
            "width": rect.width,
            "height": rect.height,
        },
        "dpi": binding.dpi,
        "dpi_scale": binding.dpi_scale,
    }


def run_doctor(
    *,
    data_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    profile: str | None = None,
    live: bool = False,
    capture: bool = False,
    initialize: bool = False,
) -> DiagnosticReport:
    """Run diagnostics without sending any mouse or keyboard input.

    ``capture=True`` is the only mode which writes a screenshot and metadata;
    they are stored in the private runtime data directory.
    """

    checks: list[DiagnosticCheck] = []
    paths = ensure_data_layout(data_dir) if initialize or capture else runtime_paths(data_dir)
    checks.append(
        DiagnosticCheck(
            "Python",
            sys.version_info[:2] == (3, 11) and struct.calcsize("P") == 8,
            f"{platform.python_implementation()} {platform.python_version()} ({struct.calcsize('P') * 8}-bit)；要求 3.11 x64",
        )
    )
    checks.append(
        DiagnosticCheck(
            "操作系统",
            sys.platform == "win32" and platform.machine().endswith("64"),
            f"{platform.system()} {platform.release()} {platform.machine()}；实机要求 Windows x64",
        )
    )
    checks.append(_check_imports(("cv2", "numpy", "mss", "pyautogui", "keyboard", "PyQt5")))

    try:
        config = load_runtime_config(config_path=config_path, data_dir=paths.root)
        selected_profile = validate_profile_name(profile or str(config.get("profile", "default")))
        checks.append(DiagnosticCheck("运行配置", True, f"配置有效；profile={selected_profile}"))
    except (ConfigError, OSError) as exc:
        checks.append(DiagnosticCheck("运行配置", False, str(exc)))
        return DiagnosticReport(tuple(checks))

    checks.append(
        DiagnosticCheck(
            "数据目录",
            paths.root.is_dir(),
            f"{paths.root}" + ("（已就绪）" if paths.root.is_dir() else "（尚未初始化，运行 bootstrap.ps1）"),
        )
    )

    # A missing/invalid calibration must not prevent the screenshot that is
    # needed to create that calibration.  Live checks bind first so profile
    # metadata can be verified against the actual client size and DPI.
    if not (live or capture):
        checks.append(
            _check_calibration(
                data_root=paths.root,
                profile=selected_profile,
                blocking=not initialize,
            )
        )

    binding = None
    binding_data = None
    capture_path = None
    if live or capture:
        emergency_key = str(config.get("safety", {}).get("emergency_stop_key", "f11"))
        checks.append(_check_emergency_hotkey(emergency_key, blocking=not capture))
        try:
            from core.windows import WindowBinding

            window_config = config["window"]
            binding = WindowBinding.bind(
                hwnd=window_config.get("preferred_hwnd"),
                process_names=tuple(window_config.get("process_names", ())),
                title_contains=window_config.get("title_contains") or None,
            )
            binding_data = _binding_as_dict(binding)
            checks.append(
                DiagnosticCheck(
                    "游戏窗口",
                    True,
                    f"HWND={binding.hwnd} {binding.process_name} {binding.client_size[0]}x{binding.client_size[1]} DPI={binding.dpi}",
                )
            )
            checks.append(
                _check_calibration(
                    data_root=paths.root,
                    profile=selected_profile,
                    blocking=not capture,
                    client_size=binding.client_size,
                    dpi=binding.dpi,
                )
            )
        except Exception as exc:
            checks.append(DiagnosticCheck("游戏窗口", False, f"{type(exc).__name__}: {exc}"))

    if binding is not None:
        try:
            from core.frame_source import LiveWindowFrameSource

            with LiveWindowFrameSource(binding) as source:
                frame = source.capture()
            checks.append(DiagnosticCheck("客户区截图", True, f"读取成功：{frame.size[0]}x{frame.size[1]} BGR"))
            if capture:
                import cv2

                paths = ensure_data_layout(paths.root)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                image_path = paths.captures / f"full-window-{stamp}.png"
                metadata_path = paths.captures / f"full-window-{stamp}.json"
                if not cv2.imwrite(str(image_path), frame.image):
                    raise OSError(f"无法写入截图：{image_path}")
                metadata = {
                    "schema_version": 1,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "profile": selected_profile,
                    "color_space": "BGR",
                    "git_commit": _git_commit(),
                    "window": binding_data,
                    "image": image_path.name,
                }
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                capture_path = str(image_path)
                checks.append(DiagnosticCheck("保存截图", True, f"{image_path}；元数据：{metadata_path.name}"))
        except Exception as exc:
            checks.append(DiagnosticCheck("客户区截图", False, f"{type(exc).__name__}: {exc}"))

    return DiagnosticReport(tuple(checks), binding=binding_data, capture_path=capture_path)


def _git_commit() -> str | None:
    """Read the commit recorded by run.ps1 without invoking Git here."""

    import os

    return os.environ.get("MHXY_BOT_GIT_SHA") or None


def format_report(report: DiagnosticReport) -> str:
    lines = []
    for item in report.checks:
        if item.ok:
            label = "通过"
        elif item.blocking:
            label = "失败"
        else:
            label = "警告"
        lines.append(f"[{label}] {item.name}: {item.detail}")
    if report.capture_path:
        lines.append(f"截图文件：{report.capture_path}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MHXY Bot 安全环境诊断（不会发送输入）")
    parser.add_argument("--data-dir", help="运行数据目录；优先级高于环境变量")
    parser.add_argument("--config", help="显式 JSON 配置文件")
    parser.add_argument("--profile", default=None, help="模板配置档名称")
    parser.add_argument("--live", action="store_true", help="只读检查游戏窗口和客户区截图")
    parser.add_argument("--capture", action="store_true", help="将一张客户区截图保存到私有 captures 目录")
    parser.add_argument("--init", action="store_true", help="初始化私有运行目录和默认配置")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_doctor(
        data_dir=args.data_dir,
        config_path=args.config,
        profile=args.profile,
        live=args.live or args.capture,
        capture=args.capture,
        initialize=args.init,
    )
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
