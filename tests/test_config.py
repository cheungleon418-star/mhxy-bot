from __future__ import annotations

import json
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest

from config.runtime import (
    ConfigError,
    DEFAULT_MANIFEST_PATH,
    REQUIRED_TEMPLATES,
    ensure_data_layout,
    import_calibration_bundle,
    load_runtime_config,
    resolve_data_dir,
    validate_template_profile,
)


def test_data_dir_precedence(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    environment = tmp_path / "environment"
    local = tmp_path / "local"
    env = {"MHXY_BOT_DATA_DIR": str(environment), "LOCALAPPDATA": str(local)}

    assert resolve_data_dir(explicit, env) == explicit.resolve()
    assert resolve_data_dir(None, env) == environment.resolve()
    assert resolve_data_dir(None, {"LOCALAPPDATA": str(local)}) == (local / "MHXY_Bot").resolve()


def test_first_run_layout_and_config_are_safe(tmp_path: Path) -> None:
    paths = ensure_data_layout(tmp_path / "private")
    assert paths.config.is_file()
    for directory in (paths.templates, paths.captures, paths.logs, paths.diagnostics):
        assert directory.is_dir()

    config = load_runtime_config(data_dir=paths.root)
    assert config["mode"] == "treasure_map"
    assert config["safety"]["dry_run"] is True
    assert config["safety"]["armed"] is False
    assert config["treasure_map"]["inventory_roi"] == [0.4, 0.15, 0.6, 0.8]


def test_persisted_live_mode_is_rejected(tmp_path: Path) -> None:
    paths = ensure_data_layout(tmp_path)
    config = json.loads(paths.config.read_text(encoding="utf-8"))
    config["safety"]["dry_run"] = False
    paths.config.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigError, match="dry_run=true"):
        load_runtime_config(data_dir=paths.root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_action_retries", 0),
        ("poll_interval_seconds", 0),
        ("inventory_roi", [0.8, 0.2, 0.4, 0.4]),
    ],
)
def test_invalid_task_safety_values_are_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    paths = ensure_data_layout(tmp_path)
    config = json.loads(paths.config.read_text(encoding="utf-8"))
    config["treasure_map"][field] = value
    paths.config.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_runtime_config(data_dir=paths.root)


def test_default_manifest_blocks_live_mode_without_private_templates(tmp_path: Path) -> None:
    status = validate_template_profile(tmp_path, "default")
    assert not status.ready
    assert status.manifest_path == DEFAULT_MANIFEST_PATH
    assert status.calibrated == ()
    assert len(status.errors) >= len(REQUIRED_TEMPLATES)


def _calibrated_manifest(profile: str = "test_profile") -> dict:
    entries = {}
    for name in REQUIRED_TEMPLATES:
        entries[name] = {
            "file": f"{name}.png",
            "calibrated": True,
            "threshold": 0.9,
            "roi": [0.0, 0.0, 1.0, 1.0],
            "scales": [1.0],
            "stable_frames": 2,
        }
    return {
        "schema_version": 1,
        "profile": profile,
        "coordinate_space": "normalized_client",
        "client_size": [800, 600],
        "dpi": 96,
        "templates": entries,
    }


def test_private_calibration_bundle_import(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.zip"
    manifest = _calibrated_manifest()
    ok, encoded = cv2.imencode(".png", np.zeros((4, 4, 3), dtype=np.uint8))
    assert ok
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name in REQUIRED_TEMPLATES:
            archive.writestr(f"{name}.png", encoded.tobytes())

    status = import_calibration_bundle(bundle, tmp_path / "data", "test_profile")
    assert status.ready
    assert set(status.calibrated) == set(REQUIRED_TEMPLATES)
    assert status.manifest_path.parent == tmp_path / "data" / "templates" / "test_profile"
    assert validate_template_profile(
        tmp_path / "data", "test_profile", client_size=(800, 600), dpi=96
    ).ready
    mismatch = validate_template_profile(
        tmp_path / "data", "test_profile", client_size=(1024, 768), dpi=120
    )
    assert not mismatch.ready
    assert any("client_size" in error for error in mismatch.errors)
    assert any("dpi" in error for error in mismatch.errors)


def test_calibration_bundle_rejects_path_traversal(tmp_path: Path) -> None:
    bundle = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps(_calibrated_manifest()))
        archive.writestr("../escape.png", b"not allowed")

    with pytest.raises(ConfigError, match="不允许包含目录"):
        import_calibration_bundle(bundle, tmp_path / "data", "default")
    assert not (tmp_path / "escape.png").exists()
