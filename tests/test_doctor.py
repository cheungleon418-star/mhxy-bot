from __future__ import annotations

from types import SimpleNamespace

import config.doctor as doctor
from config.doctor import DiagnosticCheck, DiagnosticReport, format_report


def test_nonblocking_calibration_warning_does_not_fail_capture_operation() -> None:
    report = DiagnosticReport(
        checks=(
            DiagnosticCheck("截图", True, "已保存"),
            DiagnosticCheck("模板校准", False, "0/12", blocking=False),
        )
    )

    assert report.ok
    assert "[警告] 模板校准" in format_report(report)


def test_blocking_diagnostic_failure_still_returns_failure() -> None:
    report = DiagnosticReport(
        checks=(DiagnosticCheck("窗口", False, "未找到", blocking=True),)
    )

    assert not report.ok
    assert "[失败] 窗口" in format_report(report)


def test_capture_cli_returns_success_when_only_warning_is_uncalibrated(
    monkeypatch,
) -> None:
    report = DiagnosticReport(
        checks=(DiagnosticCheck("模板校准", False, "未校准", blocking=False),),
        capture_path="private/capture.png",
    )
    monkeypatch.setattr(doctor, "run_doctor", lambda **_kwargs: report)

    assert doctor.main(["--capture"]) == 0


def test_live_calibration_uses_bound_client_geometry(monkeypatch, tmp_path) -> None:
    received = {}

    def fake_validate(data_root, profile, **kwargs):
        received.update(kwargs)
        return SimpleNamespace(ready=True, calibrated=doctor.REQUIRED_TEMPLATES, errors=())

    monkeypatch.setattr(doctor, "validate_template_profile", fake_validate)
    result = doctor._check_calibration(
        data_root=tmp_path,
        profile="pc",
        blocking=True,
        client_size=(1280, 960),
        dpi=120,
    )

    assert result.ok
    assert received == {"client_size": (1280, 960), "dpi": 120}


def test_emergency_hotkey_probe_registers_and_removes_without_suppression(monkeypatch) -> None:
    calls = []

    class FakeKeyboard:
        def add_hotkey(self, key, callback, *, suppress):
            calls.append(("add", key, suppress))
            return "registration"

        def remove_hotkey(self, registration):
            calls.append(("remove", registration))

    real_import = doctor.importlib.import_module

    def fake_import(name):
        if name == "keyboard":
            return FakeKeyboard()
        return real_import(name)

    monkeypatch.setattr(doctor.importlib, "import_module", fake_import)
    result = doctor._check_emergency_hotkey("f11", blocking=True)

    assert result.ok
    assert calls == [("add", "f11", False), ("remove", "registration")]
