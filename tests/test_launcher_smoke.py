from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from launcher import LauncherWindow  # noqa: E402


def test_launcher_constructs_offscreen_with_safe_defaults(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = LauncherWindow(data_dir=str(tmp_path / "private"))
    try:
        assert window.dry_run_check.isChecked()
        assert window.mode_combo.currentData() == "treasure_map"
        assert window.start_btn.isEnabled()
        assert "未校准" in window.calibration_label.text()
    finally:
        window.close()
        app.processEvents()
