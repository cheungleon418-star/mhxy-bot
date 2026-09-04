"""Runtime configuration and private data-directory helpers.

The repository only contains safe examples.  Machine-specific configuration,
screenshots and calibrated templates live below ``%LOCALAPPDATA%\\MHXY_Bot``
unless the caller explicitly chooses another directory.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence


DATA_DIR_ENV = "MHXY_BOT_DATA_DIR"
DEFAULT_PROFILE = "default"
CONFIG_FILENAME = "config.json"
MANIFEST_FILENAME = "manifest.json"

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "default_config.json"
DEFAULT_MANIFEST_PATH = PACKAGE_DIR / "template_manifest.json"

REQUIRED_TEMPLATES: tuple[str, ...] = (
    "world_hud_anchor",
    "backpack_open",
    "treasure_map_icon",
    "map_panel_anchor",
    "quest_target_marker",
    "dig_interact_prompt",
    "combat_hud_anchor",
    "death_return_scene_anchor",
    "task_panel_anchor",
    "active_treasure_task",
    "no_active_treasure_task",
    "reward_dialog",
    "reward_confirm_button",
    "captcha_dialog",
    "disconnect_dialog",
)


class ConfigError(ValueError):
    """Raised when a runtime configuration or calibration bundle is unsafe."""


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved paths for data which must never be stored in Git."""

    root: Path
    config: Path
    templates: Path
    captures: Path
    logs: Path
    diagnostics: Path

    def profile_templates(self, profile: str) -> Path:
        return self.templates / validate_profile_name(profile)


@dataclass(frozen=True)
class CalibrationStatus:
    profile: str
    ready: bool
    manifest_path: Path
    errors: tuple[str, ...]
    calibrated: tuple[str, ...]


def validate_profile_name(profile: str) -> str:
    profile = profile.strip()
    if not profile:
        raise ConfigError("配置档名称不能为空")
    if profile in {".", ".."} or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in profile):
        raise ConfigError("配置档名称只能包含字母、数字、连字符和下划线")
    return profile


def resolve_data_dir(
    explicit: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve data directory using CLI > environment > LocalAppData."""

    env = os.environ if environ is None else environ
    raw = str(explicit).strip() if explicit is not None else ""
    if not raw:
        raw = env.get(DATA_DIR_ENV, "").strip()
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()

    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (Path(local_app_data) / "MHXY_Bot").resolve()
    return (Path.home() / "AppData" / "Local" / "MHXY_Bot").resolve()


def runtime_paths(data_dir: str | os.PathLike[str] | None = None) -> RuntimePaths:
    root = resolve_data_dir(data_dir)
    return RuntimePaths(
        root=root,
        config=root / CONFIG_FILENAME,
        templates=root / "templates",
        captures=root / "captures",
        logs=root / "logs",
        diagnostics=root / "diagnostics",
    )


def ensure_data_layout(data_dir: str | os.PathLike[str] | None = None) -> RuntimePaths:
    """Create private runtime directories and a safe first-run config."""

    paths = runtime_paths(data_dir)
    for directory in (
        paths.root,
        paths.templates,
        paths.captures,
        paths.logs,
        paths.diagnostics,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.config.exists():
        shutil.copyfile(DEFAULT_CONFIG_PATH, paths.config)
    return paths


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON 格式错误：{path}（第 {exc.lineno} 行）") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"JSON 顶层必须是对象：{path}")
    return value


def _deep_merge(base: MutableMapping[str, Any], override: Mapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), MutableMapping):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def validate_runtime_config(config: Mapping[str, Any]) -> None:
    if config.get("schema_version") != 1:
        raise ConfigError("仅支持 schema_version=1")
    if config.get("mode") != "treasure_map":
        raise ConfigError("首版只允许 treasure_map 模式")

    profile = config.get("profile", DEFAULT_PROFILE)
    if not isinstance(profile, str):
        raise ConfigError("profile 必须是字符串")
    validate_profile_name(profile)

    task = config.get("treasure_map")
    if not isinstance(task, Mapping):
        raise ConfigError("缺少 treasure_map 配置")
    max_maps = task.get("max_maps")
    if not isinstance(max_maps, int) or isinstance(max_maps, bool) or not 1 <= max_maps <= 999:
        raise ConfigError("treasure_map.max_maps 必须是 1 到 999 的整数")
    retries = task.get("max_action_retries")
    if not isinstance(retries, int) or isinstance(retries, bool) or not 1 <= retries <= 10:
        raise ConfigError("treasure_map.max_action_retries 必须是 1 到 10 的整数")
    poll_interval = task.get("poll_interval_seconds")
    if (not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool)
            or not 0.05 <= poll_interval <= 5):
        raise ConfigError("treasure_map.poll_interval_seconds 必须在 0.05 到 5 秒之间")
    inventory_roi = task.get("inventory_roi")
    if (not isinstance(inventory_roi, Sequence) or isinstance(inventory_roi, (str, bytes))
            or len(inventory_roi) != 4
            or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                   for value in inventory_roi)):
        raise ConfigError("treasure_map.inventory_roi 必须是四个数字")
    roi_x, roi_y, roi_width, roi_height = (float(value) for value in inventory_roi)
    if (roi_x < 0 or roi_y < 0 or roi_width <= 0 or roi_height <= 0
            or roi_x + roi_width > 1 or roi_y + roi_height > 1):
        raise ConfigError("treasure_map.inventory_roi 必须位于 0..1 的客户区范围内")
    if task.get("inventory_signature_enabled") is not False:
        raise ConfigError(
            "treasure_map.inventory_signature_enabled 首版必须为 false；"
            "未经校准的整背包图像变化不能作为消耗证据"
        )

    safety = config.get("safety")
    if not isinstance(safety, Mapping) or safety.get("dry_run") is not True:
        raise ConfigError("持久化配置必须保持 safety.dry_run=true；实机授权只对本次运行有效")

    recovery = config.get("recovery")
    if not isinstance(recovery, Mapping):
        raise ConfigError("缺少 recovery 配置")
    timeout = recovery.get("death_scene_timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 5 <= timeout <= 300:
        raise ConfigError("死亡场景超时必须在 5 到 300 秒之间")
    stable_frames = recovery.get("stable_frames")
    if (not isinstance(stable_frames, int) or isinstance(stable_frames, bool)
            or not 2 <= stable_frames <= 20):
        raise ConfigError("recovery.stable_frames 必须是 2 到 20 的整数")
    if recovery.get("disconnect_action") != "stop":
        raise ConfigError("recovery.disconnect_action 首版必须为 stop")
    if recovery.get("captcha_action") != "pause":
        raise ConfigError("recovery.captcha_action 首版必须为 pause")

    window = config.get("window")
    if not isinstance(window, Mapping):
        raise ConfigError("缺少 window 配置")
    process_names = window.get("process_names")
    if not isinstance(process_names, Sequence) or isinstance(process_names, (str, bytes)) or not process_names:
        raise ConfigError("window.process_names 必须是非空数组")
    if any(not isinstance(name, str) or not name.lower().endswith(".exe") for name in process_names):
        raise ConfigError("window.process_names 只能包含 exe 文件名")
    title_contains = window.get("title_contains")
    if not isinstance(title_contains, str):
        raise ConfigError("window.title_contains 必须是字符串")
    preferred_hwnd = window.get("preferred_hwnd")
    if (preferred_hwnd is not None
            and (not isinstance(preferred_hwnd, int) or isinstance(preferred_hwnd, bool)
                 or preferred_hwnd < 1)):
        raise ConfigError("window.preferred_hwnd 必须为空或正整数")


def load_runtime_config(
    config_path: str | os.PathLike[str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load defaults and an optional machine-local override.

    ``armed`` is deliberately reset on every load.  It is an in-memory launch
    decision and must never become a persistent setting.
    """

    config = copy.deepcopy(_read_json(DEFAULT_CONFIG_PATH))
    if config_path:
        override_path = Path(config_path).expanduser().resolve()
        config = dict(_deep_merge(config, _read_json(override_path)))
    else:
        candidate = runtime_paths(data_dir).config
        if candidate.exists():
            config = dict(_deep_merge(config, _read_json(candidate)))
    config.setdefault("safety", {})["armed"] = False
    validate_runtime_config(config)
    return config


def load_template_manifest(
    data_dir: str | os.PathLike[str] | None = None,
    profile: str = DEFAULT_PROFILE,
) -> tuple[dict[str, Any], Path]:
    profile_dir = runtime_paths(data_dir).profile_templates(profile)
    local_manifest = profile_dir / MANIFEST_FILENAME
    manifest_path = local_manifest if local_manifest.exists() else DEFAULT_MANIFEST_PATH
    return _read_json(manifest_path), manifest_path


def _validate_manifest_data(
    manifest: Mapping[str, Any],
    *,
    profile: str,
    profile_dir: Path,
    client_size: Sequence[int] | None = None,
    dpi: int | None = None,
) -> tuple[list[str], list[str]]:
    """Validate calibration identity, rules, PNGs, and live geometry."""

    errors: list[str] = []
    calibrated: list[str] = []
    if manifest.get("schema_version") != 1:
        errors.append("模板清单 schema_version 必须为 1")
    if manifest.get("profile") != profile:
        errors.append(f"模板清单 profile 必须与所选配置档一致：{profile}")
    if manifest.get("coordinate_space") != "normalized_client":
        errors.append("模板清单 coordinate_space 必须为 normalized_client")

    manifest_size = manifest.get("client_size")
    valid_size = (
        isinstance(manifest_size, Sequence)
        and not isinstance(manifest_size, (str, bytes))
        and len(manifest_size) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in manifest_size)
    )
    if not valid_size:
        errors.append("模板清单 client_size 必须是校准时的正整数 [宽, 高]")
    elif client_size is not None and tuple(manifest_size) != tuple(int(v) for v in client_size):
        errors.append(
            f"模板 client_size={list(manifest_size)} 与当前客户区={list(client_size)} 不一致"
        )

    manifest_dpi = manifest.get("dpi")
    if not isinstance(manifest_dpi, int) or isinstance(manifest_dpi, bool) or manifest_dpi <= 0:
        errors.append("模板清单 dpi 必须是校准时的正整数 DPI")
    elif dpi is not None and manifest_dpi != int(dpi):
        errors.append(f"模板 dpi={manifest_dpi} 与当前窗口 dpi={int(dpi)} 不一致")

    entries = manifest.get("templates")
    if not isinstance(entries, dict):
        errors.append("模板清单缺少 templates 对象")
        entries = {}

    for name in REQUIRED_TEMPLATES:
        entry = entries.get(name)
        if not isinstance(entry, dict):
            errors.append(f"缺少模板条目：{name}")
            continue
        filename = entry.get("file")
        if (not isinstance(filename, str) or Path(filename).name != filename
                or not filename.lower().endswith(".png")):
            errors.append(f"模板文件名不安全：{name}")
            continue
        threshold = entry.get("threshold")
        if (not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
                or not 0 < float(threshold) <= 1):
            errors.append(f"模板阈值无效：{name}")
        roi = entry.get("roi")
        if (not isinstance(roi, Sequence) or isinstance(roi, (str, bytes)) or len(roi) != 4
                or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       for value in roi)):
            errors.append(f"模板 ROI 无效：{name}")
        else:
            x, y, width, height = (float(value) for value in roi)
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                errors.append(f"模板 ROI 必须位于归一化客户区：{name}")
        scales = entry.get("scales")
        if (not isinstance(scales, Sequence) or isinstance(scales, (str, bytes)) or not scales
                or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                       or float(value) <= 0 for value in scales)):
            errors.append(f"模板缩放范围无效：{name}")
        stable = entry.get("stable_frames")
        if not isinstance(stable, int) or isinstance(stable, bool) or stable < 2:
            errors.append(f"模板连续帧规则无效：{name}")
        if entry.get("calibrated") is not True:
            errors.append(f"模板尚未校准：{name}")
            continue
        template_path = profile_dir / filename
        if not template_path.is_file() or template_path.stat().st_size == 0:
            errors.append(f"模板文件不存在：{name} ({template_path})")
            continue
        try:
            import cv2
            import numpy as np

            decoded = cv2.imdecode(np.fromfile(template_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            decoded = None
        if decoded is None or decoded.size == 0:
            errors.append(f"模板 PNG 无法解码：{name} ({template_path})")
            continue
        calibrated.append(name)
    return errors, calibrated


def validate_template_profile(
    data_dir: str | os.PathLike[str] | None = None,
    profile: str = DEFAULT_PROFILE,
    *,
    client_size: Sequence[int] | None = None,
    dpi: int | None = None,
) -> CalibrationStatus:
    profile = validate_profile_name(profile)
    paths = runtime_paths(data_dir)
    manifest, manifest_path = load_template_manifest(paths.root, profile)
    profile_dir = paths.profile_templates(profile)
    errors, calibrated = _validate_manifest_data(
        manifest,
        profile=profile,
        profile_dir=profile_dir,
        client_size=client_size,
        dpi=dpi,
    )

    return CalibrationStatus(
        profile=profile,
        ready=not errors,
        manifest_path=manifest_path,
        errors=tuple(errors),
        calibrated=tuple(calibrated),
    )


def import_calibration_bundle(
    bundle_path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
    profile: str = DEFAULT_PROFILE,
) -> CalibrationStatus:
    """Validate and import a private ZIP calibration bundle.

    A bundle contains ``manifest.json`` and PNG files at its root.  No other
    paths or file types are accepted, preventing archive traversal and keeping
    unrelated files out of the private template directory.
    """

    profile = validate_profile_name(profile)
    source = Path(bundle_path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".zip":
        raise ConfigError("校准包必须是存在的 ZIP 文件")

    paths = ensure_data_layout(data_dir)
    destination = paths.profile_templates(profile)
    destination.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mhxy-calibration-") as tmp_name:
        temporary = Path(tmp_name)
        try:
            archive = zipfile.ZipFile(source)
        except zipfile.BadZipFile as exc:
            raise ConfigError("校准包不是有效的 ZIP 文件") from exc
        with archive:
            members = archive.infolist()
            if not members:
                raise ConfigError("校准包为空")
            total_size = sum(member.file_size for member in members)
            if total_size > 100 * 1024 * 1024:
                raise ConfigError("校准包解压后不能超过 100 MiB")
            for member in members:
                member_path = Path(member.filename)
                if member.is_dir():
                    continue
                if len(member_path.parts) != 1 or member_path.name != member.filename:
                    raise ConfigError("校准包不允许包含目录")
                if member_path.name != MANIFEST_FILENAME and member_path.suffix.lower() != ".png":
                    raise ConfigError(f"校准包包含不允许的文件：{member.filename}")
                target = temporary / member_path.name
                with archive.open(member) as source_file, target.open("wb") as target_file:
                    shutil.copyfileobj(source_file, target_file)

        manifest_path = temporary / MANIFEST_FILENAME
        manifest = _read_json(manifest_path)
        errors, _calibrated = _validate_manifest_data(
            manifest, profile=profile, profile_dir=temporary
        )
        if errors:
            raise ConfigError("校准包检查失败：" + "；".join(errors))

        # Validation is complete before any destination file is changed.
        for item in temporary.iterdir():
            shutil.copy2(item, destination / item.name)

    status = validate_template_profile(paths.root, profile)
    if not status.ready:
        raise ConfigError("导入后校准检查失败：" + "；".join(status.errors))
    return status


__all__ = [
    "CalibrationStatus",
    "ConfigError",
    "DATA_DIR_ENV",
    "DEFAULT_PROFILE",
    "REQUIRED_TEMPLATES",
    "RuntimePaths",
    "ensure_data_layout",
    "import_calibration_bundle",
    "load_runtime_config",
    "load_template_manifest",
    "resolve_data_dir",
    "runtime_paths",
    "validate_profile_name",
    "validate_runtime_config",
    "validate_template_profile",
]
