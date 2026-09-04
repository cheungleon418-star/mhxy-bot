# -*- coding: utf-8 -*-
"""Safe PyQt5 launcher for the single-window treasure-map prototype."""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor, QFont, QPalette
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config.runtime import (
    ConfigError,
    REQUIRED_TEMPLATES,
    ensure_data_layout,
    import_calibration_bundle,
    load_runtime_config,
    resolve_data_dir,
    validate_profile_name,
    validate_template_profile,
)


LOGGER = logging.getLogger(__name__)


class ModeConfig:
    """Visible modes; only treasure_map is verified in the first release."""

    MODES = (
        ("treasure_map", "藏宝图挖掘", True),
        ("quest", "师门任务（未验证）", False),
        ("ghost", "捉鬼任务（未验证）", False),
        ("escort", "押镖（未验证）", False),
        ("dungeon", "副本（未验证）", False),
        ("story", "主线任务（未验证）", False),
    )


@dataclass(frozen=True)
class LaunchOptions:
    data_dir: Path
    config_path: Path | None
    profile: str
    max_maps: int
    dry_run: bool
    armed: bool


class SignalLogHandler(logging.Handler):
    """Forward Python logging to the GUI through a thread-safe Qt signal."""

    def __init__(self, signal: Any) -> None:
        super().__init__()
        self.signal = signal

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.signal.emit(self.format(record))
        except Exception:
            self.handleError(record)


class BotWorker(QObject):
    log = pyqtSignal(str)
    status = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, options: LaunchOptions) -> None:
        super().__init__()
        self.options = options
        self.bot: Any = None
        self._runner_thread: threading.Thread | None = None
        self._control_lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._pause_requested = False

    @pyqtSlot()
    def run(self) -> None:
        """Start Python work while leaving the QThread event loop responsive."""

        if self._runner_thread is not None:
            return
        self._runner_thread = threading.Thread(
            target=self._run_bot,
            name="mhxy-bot-runtime",
            daemon=True,
        )
        self._runner_thread.start()

    def _run_bot(self) -> None:
        handler = SignalLogHandler(self.log)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        try:
            self.status.emit("干运行中" if self.options.dry_run else "实机运行中")
            self.log.emit(
                f"启动藏宝图：profile={self.options.profile}，max_maps={self.options.max_maps}，"
                f"模式={'DRY-RUN' if self.options.dry_run else 'LIVE'}"
            )
            bot = self._build_bot()
            with self._control_lock:
                self.bot = bot
                pause_requested = self._pause_requested
                stop_requested = self._stop_requested.is_set()
            if pause_requested:
                bot.paused = True
            if stop_requested:
                bot.running = False
            run_signature = inspect.signature(bot.run)
            run_kwargs: dict[str, Any] = {}
            if "max_maps" in run_signature.parameters:
                run_kwargs["max_maps"] = self.options.max_maps
            bot.run("treasure_map", **run_kwargs)
            runtime_status = bot.status() if callable(getattr(bot, "status", None)) else {}
            state = str(runtime_status.get("state", getattr(getattr(bot, "machine", None), "state", "")))
            if hasattr(getattr(bot, "machine", None), "state"):
                state = getattr(bot.machine.state, "value", str(bot.machine.state))
            detail = runtime_status.get("last_error") or runtime_status.get("last_outcome") or state
            if state in {"error_stop", "disconnect_stop"}:
                self.finished.emit(False, f"任务异常停止：{detail}")
            elif state == "completed":
                self.finished.emit(True, "藏宝图任务已完成")
            elif self._stop_requested.is_set():
                self.finished.emit(True, "用户已停止任务")
            else:
                self.finished.emit(False, f"运行核心意外退出：{detail or 'unknown state'}")
        except Exception as exc:
            LOGGER.exception("Bot worker failed")
            self.log.emit(traceback.format_exc())
            self.finished.emit(False, f"{type(exc).__name__}: {exc}")
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()

    def _build_bot(self) -> Any:
        """Lazy-load the runtime and require its safety-aware constructor."""

        from main import AutoBot

        signature = inspect.signature(AutoBot)
        accepts_any = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        required_safety = {"dry_run", "armed"}
        missing = required_safety.difference(signature.parameters)
        if missing and not accepts_any:
            raise RuntimeError(
                "当前运行核心没有安全参数 dry_run/armed，启动器拒绝运行旧输入逻辑"
            )

        candidates: dict[str, Any] = {
            "enable_ui": False,
            "dry_run": self.options.dry_run,
            "armed": self.options.armed,
            "config_path": str(self.options.config_path) if self.options.config_path else None,
            "data_dir": str(self.options.data_dir),
            "profile": self.options.profile,
            "max_maps": self.options.max_maps,
        }
        kwargs = {
            key: value
            for key, value in candidates.items()
            if accepts_any or key in signature.parameters
        }
        return AutoBot(**kwargs)

    @pyqtSlot(bool)
    def set_paused(self, paused: bool) -> None:
        with self._control_lock:
            self._pause_requested = paused
            bot = self.bot
        if bot is None:
            return
        setter = getattr(bot, "set_paused", None)
        if callable(setter):
            setter(paused)
        else:
            bot.paused = paused

    @pyqtSlot()
    def request_stop(self) -> None:
        self._stop_requested.set()
        with self._control_lock:
            bot = self.bot
        if bot is None:
            return
        stopper = getattr(bot, "stop", None)
        if callable(stopper):
            stopper()
        else:
            bot.running = False


class UtilityWorker(QObject):
    finished = pyqtSignal(bool, str)

    def __init__(self, operation: str, data_dir: Path, config_path: Path | None, profile: str) -> None:
        super().__init__()
        self.operation = operation
        self.data_dir = data_dir
        self.config_path = config_path
        self.profile = profile

    @pyqtSlot()
    def run(self) -> None:
        try:
            from config.doctor import format_report, run_doctor

            report = run_doctor(
                data_dir=self.data_dir,
                config_path=self.config_path,
                profile=self.profile,
                live=True,
                capture=self.operation == "capture",
            )
            self.finished.emit(report.ok, format_report(report))
        except Exception as exc:
            self.finished.emit(False, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class LauncherWindow(QMainWindow):
    log_signal = pyqtSignal(str)
    pause_bot_signal = pyqtSignal(bool)
    stop_bot_signal = pyqtSignal()

    def __init__(
        self,
        *,
        data_dir: str | None = None,
        config_path: str | None = None,
        profile: str | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("梦幻西游自动辅助 v2.0 - 安全实测版")
        self.setMinimumSize(820, 650)

        self._data_dir = resolve_data_dir(data_dir)
        self._config_path = Path(config_path).expanduser().resolve() if config_path else None
        self._initial_profile = profile
        self._bot_thread: QThread | None = None
        self._bot_worker: BotWorker | None = None
        self._utility_threads: list[tuple[QThread, UtilityWorker]] = []
        self._start_time: float | None = None
        self._paused = False
        self._close_requested = False

        self.log_signal.connect(self._append_log)
        self._build_ui()
        self._load_initial_config()
        self._refresh_calibration_status()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        title = QLabel("梦幻西游 · 单窗口藏宝图辅助")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #60a5fa; padding: 10px;")
        main_layout.addWidget(title)

        mode_group = QGroupBox("任务模式")
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.addWidget(QLabel("选择模式："))
        self.mode_combo = QComboBox()
        for key, label, enabled in ModeConfig.MODES:
            self.mode_combo.addItem(label, key)
            item = self.mode_combo.model().item(self.mode_combo.count() - 1)
            if item is not None:
                item.setEnabled(enabled)
        self.mode_combo.setMinimumWidth(210)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch()
        mode_layout.addWidget(QLabel("首版仅开放藏宝图；其他模式保留但不可启动"))
        main_layout.addWidget(mode_group)

        runtime_group = QGroupBox("实机与配置")
        runtime_layout = QGridLayout(runtime_group)
        runtime_layout.addWidget(QLabel("私有数据目录："), 0, 0)
        self.data_dir_edit = QLineEdit(str(self._data_dir))
        self.data_dir_edit.editingFinished.connect(self._on_data_dir_changed)
        runtime_layout.addWidget(self.data_dir_edit, 0, 1, 1, 3)
        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.clicked.connect(self._browse_data_dir)
        runtime_layout.addWidget(self.browse_btn, 0, 4)

        runtime_layout.addWidget(QLabel("模板配置档："), 1, 0)
        self.profile_combo = QComboBox()
        self.profile_combo.setEditable(True)
        self.profile_combo.addItem("default")
        self.profile_combo.currentTextChanged.connect(self._refresh_calibration_status)
        runtime_layout.addWidget(self.profile_combo, 1, 1)

        runtime_layout.addWidget(QLabel("最多处理："), 1, 2)
        self.max_maps_spin = QSpinBox()
        self.max_maps_spin.setRange(1, 999)
        self.max_maps_spin.setSuffix(" 张")
        self.max_maps_spin.setValue(10)
        runtime_layout.addWidget(self.max_maps_spin, 1, 3)

        self.doctor_btn = QPushButton("只读窗口诊断")
        self.doctor_btn.clicked.connect(lambda: self._run_utility("doctor"))
        runtime_layout.addWidget(self.doctor_btn, 2, 0)
        self.capture_btn = QPushButton("采集客户区截图")
        self.capture_btn.clicked.connect(lambda: self._run_utility("capture"))
        runtime_layout.addWidget(self.capture_btn, 2, 1)
        self.import_btn = QPushButton("导入校准包…")
        self.import_btn.clicked.connect(self._import_calibration)
        runtime_layout.addWidget(self.import_btn, 2, 2)
        self.calibration_label = QLabel("模板状态：检查中")
        runtime_layout.addWidget(self.calibration_label, 2, 3, 1, 2)
        main_layout.addWidget(runtime_group)

        safety_group = QGroupBox("运行安全")
        safety_layout = QHBoxLayout(safety_group)
        self.dry_run_check = QCheckBox("干运行（不发送鼠标和键盘输入）")
        self.dry_run_check.setChecked(True)
        self.dry_run_check.stateChanged.connect(self._on_dry_run_changed)
        safety_layout.addWidget(self.dry_run_check)
        self.arm_label = QLabel("未武装")
        self.arm_label.setStyleSheet("font-weight: bold; color: #10b981;")
        safety_layout.addWidget(self.arm_label)
        safety_layout.addStretch()
        safety_layout.addWidget(QLabel("紧急停止：F11　暂停/恢复：F12"))
        main_layout.addWidget(safety_group)

        button_layout = QHBoxLayout()
        self.start_btn = QPushButton("▶ 启动藏宝图")
        self.start_btn.setMinimumHeight(40)
        self.start_btn.setStyleSheet(
            "QPushButton {background-color:#1a56db;color:white;font-size:14px;"
            "font-weight:bold;border-radius:5px;padding:8px 20px;}"
            "QPushButton:hover {background-color:#2563eb;}"
            "QPushButton:disabled {background-color:#6b7280;}"
        )
        self.start_btn.clicked.connect(self._start_bot)
        button_layout.addWidget(self.start_btn)
        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.setMinimumHeight(40)
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        button_layout.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_bot)
        button_layout.addWidget(self.stop_btn)
        main_layout.addLayout(button_layout)

        status_frame = QFrame()
        status_frame.setFrameStyle(QFrame.StyledPanel)
        status_layout = QHBoxLayout(status_frame)
        status_layout.addWidget(QLabel("状态："))
        self.status_label = QLabel("就绪（默认干运行）")
        self.status_label.setStyleSheet("font-weight:bold;color:#10b981;")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        self.time_label = QLabel("运行时间：--:--:--")
        status_layout.addWidget(self.time_label)
        main_layout.addWidget(status_frame)

        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.log_text)
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self.log_text.clear)
        log_layout.addWidget(clear_btn)
        main_layout.addWidget(log_group)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_runtime)

    def _load_initial_config(self) -> None:
        try:
            ensure_data_layout(self._data_dir)
            config = load_runtime_config(self._config_path, self._data_dir)
            profile = self._initial_profile or str(config.get("profile", "default"))
            self.profile_combo.setCurrentText(profile)
            self.max_maps_spin.setValue(int(config["treasure_map"]["max_maps"]))
            self._log(f"数据目录：{self._data_dir}")
            commit = os.environ.get("MHXY_BOT_GIT_SHA", "unknown")
            self._log(f"代码版本：{commit}")
        except Exception as exc:
            self._log(f"配置错误：{exc}")
            self.start_btn.setEnabled(False)

    def _current_profile(self) -> str:
        return validate_profile_name(self.profile_combo.currentText())

    def _on_data_dir_changed(self) -> None:
        try:
            self._data_dir = resolve_data_dir(self.data_dir_edit.text())
            self.data_dir_edit.setText(str(self._data_dir))
            ensure_data_layout(self._data_dir)
            self._refresh_calibration_status()
            self._log(f"切换数据目录：{self._data_dir}")
        except Exception as exc:
            QMessageBox.warning(self, "数据目录无效", str(exc))

    def _browse_data_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择私有数据目录", str(self._data_dir))
        if selected:
            self.data_dir_edit.setText(selected)
            self._on_data_dir_changed()

    def _refresh_calibration_status(self, *_args: Any) -> None:
        try:
            status = validate_template_profile(self._data_dir, self._current_profile())
        except Exception as exc:
            self.calibration_label.setText(f"模板状态：配置错误（{exc}）")
            self.calibration_label.setStyleSheet("color:#ef4444;")
            return
        if status.ready:
            self.calibration_label.setText(f"模板状态：已就绪（{len(status.calibrated)} 个）")
            self.calibration_label.setStyleSheet("color:#10b981;font-weight:bold;")
        else:
            self.calibration_label.setText(
                f"模板状态：未校准（{len(status.calibrated)}/{len(REQUIRED_TEMPLATES)}）"
            )
            self.calibration_label.setStyleSheet("color:#f59e0b;font-weight:bold;")

    def _on_dry_run_changed(self, state: int) -> None:
        if state == Qt.Checked:
            self.arm_label.setText("未武装")
            self.arm_label.setStyleSheet("font-weight:bold;color:#10b981;")
            return
        try:
            status = validate_template_profile(self._data_dir, self._current_profile())
        except Exception as exc:
            status = None
            error = str(exc)
        else:
            error = "；".join(status.errors[:3])
        if status is None or not status.ready:
            QMessageBox.warning(
                self,
                "禁止实机运行",
                "必需模板尚未全部校准，不能武装真实输入。\n" + error,
            )
            self.dry_run_check.blockSignals(True)
            self.dry_run_check.setChecked(True)
            self.dry_run_check.blockSignals(False)
            return
        answer = QMessageBox.warning(
            self,
            "武装本次运行",
            "实机模式会发送鼠标和键盘输入。请确认游戏窗口已就绪，并知道 F11 可紧急停止。\n\n"
            "是否只为本次运行解除干运行？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            self.dry_run_check.blockSignals(True)
            self.dry_run_check.setChecked(True)
            self.dry_run_check.blockSignals(False)
            return
        self.arm_label.setText("本次已武装")
        self.arm_label.setStyleSheet("font-weight:bold;color:#ef4444;")

    def _start_bot(self) -> None:
        if self._bot_thread is not None:
            return
        if self.mode_combo.currentData() != "treasure_map":
            QMessageBox.warning(self, "模式不可用", "首版只允许运行藏宝图模式。")
            return
        try:
            profile = self._current_profile()
        except ConfigError as exc:
            QMessageBox.warning(self, "配置档无效", str(exc))
            return

        dry_run = self.dry_run_check.isChecked()
        if not dry_run:
            status = validate_template_profile(self._data_dir, profile)
            if not status.ready:
                QMessageBox.warning(self, "禁止实机运行", "模板状态已变化，请重新校准并武装。")
                self._reset_arm()
                return
            answer = QMessageBox.question(
                self,
                "最后确认",
                f"即将向游戏发送真实输入。\n配置档：{profile}\n最多处理：{self.max_maps_spin.value()} 张\n\n确认启动？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        options = LaunchOptions(
            data_dir=self._data_dir,
            config_path=self._config_path,
            profile=profile,
            max_maps=self.max_maps_spin.value(),
            dry_run=dry_run,
            armed=not dry_run,
        )
        thread = QThread(self)
        worker = BotWorker(options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log.connect(self._log)
        worker.status.connect(self._set_status)
        self.pause_bot_signal.connect(worker.set_paused, Qt.QueuedConnection)
        self.stop_bot_signal.connect(worker.request_stop, Qt.QueuedConnection)
        worker.finished.connect(self._on_bot_finished)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(self._release_bot_thread)
        thread.finished.connect(thread.deleteLater)
        self._bot_thread = thread
        self._bot_worker = worker

        self._set_running_controls(True)
        self._start_time = time.monotonic()
        self._timer.start(1000)
        thread.start()

    def _toggle_pause(self) -> None:
        if self._bot_worker is None:
            return
        self._paused = not self._paused
        self.pause_bot_signal.emit(self._paused)
        self.pause_btn.setText("▶ 恢复" if self._paused else "⏸ 暂停")
        self._set_status("已暂停" if self._paused else "运行中")

    def _stop_bot(self) -> None:
        if self._bot_worker is not None:
            self.stop_bot_signal.emit()
            self._set_status("正在停止…")
            self.stop_btn.setEnabled(False)

    @pyqtSlot(bool, str)
    def _on_bot_finished(self, success: bool, message: str) -> None:
        self._log(("完成：" if success else "失败：") + message)
        self._set_status("已停止" if success else "错误")
        self._set_running_controls(False)
        self._timer.stop()
        self._reset_arm()

    @pyqtSlot()
    def _release_bot_thread(self) -> None:
        self._bot_thread = None
        self._bot_worker = None
        if self._close_requested:
            QTimer.singleShot(0, self.close)

    def _set_running_controls(self, running: bool) -> None:
        self.start_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        self.mode_combo.setEnabled(not running)
        self.data_dir_edit.setEnabled(not running)
        self.browse_btn.setEnabled(not running)
        self.profile_combo.setEnabled(not running)
        self.max_maps_spin.setEnabled(not running)
        self.dry_run_check.setEnabled(not running)
        self.doctor_btn.setEnabled(not running)
        self.capture_btn.setEnabled(not running)
        self.import_btn.setEnabled(not running)

    def _reset_arm(self) -> None:
        self.dry_run_check.blockSignals(True)
        self.dry_run_check.setChecked(True)
        self.dry_run_check.blockSignals(False)
        self.arm_label.setText("未武装")
        self.arm_label.setStyleSheet("font-weight:bold;color:#10b981;")
        self._paused = False
        self.pause_btn.setText("⏸ 暂停")

    def _run_utility(self, operation: str) -> None:
        try:
            profile = self._current_profile()
        except ConfigError as exc:
            QMessageBox.warning(self, "配置档无效", str(exc))
            return
        thread = QThread(self)
        worker = UtilityWorker(operation, self._data_dir, self._config_path, profile)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda ok, text: self._on_utility_finished(operation, ok, text))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._remove_utility_thread(thread))
        self._utility_threads.append((thread, worker))
        self._log("开始只读窗口诊断…" if operation == "doctor" else "开始采集客户区截图…")
        thread.start()

    def _remove_utility_thread(self, thread: QThread) -> None:
        self._utility_threads = [item for item in self._utility_threads if item[0] is not thread]

    def _on_utility_finished(self, operation: str, ok: bool, text: str) -> None:
        self._log(text)
        self._refresh_calibration_status()
        title = "诊断完成" if operation == "doctor" else "截图采集完成"
        if ok:
            QMessageBox.information(self, title, text)
        else:
            QMessageBox.warning(self, title + "（有未通过项）", text)

    def _import_calibration(self) -> None:
        bundle, _ = QFileDialog.getOpenFileName(self, "选择私有校准包", str(self._data_dir), "ZIP 校准包 (*.zip)")
        if not bundle:
            return
        try:
            status = import_calibration_bundle(bundle, self._data_dir, self._current_profile())
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self._refresh_calibration_status()
        self._log(f"校准包已导入：{status.profile}（{len(status.calibrated)} 个模板）")
        QMessageBox.information(self, "导入成功", "校准包已保存到私有数据目录，不会进入 Git。")

    def _update_runtime(self) -> None:
        if self._start_time is None:
            return
        elapsed = int(time.monotonic() - self._start_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        self.time_label.setText(f"运行时间：{hours:02d}:{minutes:02d}:{seconds:02d}")

    def _set_status(self, text: str) -> None:
        colors = {
            "运行中": "#10b981",
            "干运行中": "#60a5fa",
            "实机运行中": "#ef4444",
            "已暂停": "#f59e0b",
            "错误": "#ef4444",
            "已停止": "#6b7280",
        }
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"font-weight:bold;color:{colors.get(text, '#9ca3af')};")

    def _log(self, message: str) -> None:
        self.log_signal.emit(str(message))

    @pyqtSlot(str)
    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

    def closeEvent(self, event: Any) -> None:
        if self._bot_thread is not None and self._bot_thread.isRunning():
            answer = QMessageBox.question(
                self,
                "确认退出",
                "任务仍在运行。是否发送停止请求并退出？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._close_requested = True
            self._stop_bot()
            event.ignore()
            return
        if any(thread.isRunning() for thread, _worker in self._utility_threads):
            QMessageBox.information(self, "请稍候", "窗口诊断或截图采集仍在进行，完成后再退出。")
            event.ignore()
            return
        event.accept()


def _configure_logging(data_dir: Path) -> None:
    paths = ensure_data_layout(data_dir)
    log_path = paths.logs / "launcher.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )


def _apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(30, 30, 40))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 230))
    palette.setColor(QPalette.Base, QColor(25, 25, 35))
    palette.setColor(QPalette.Text, QColor(220, 220, 230))
    palette.setColor(QPalette.Button, QColor(45, 45, 55))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 230))
    app.setPalette(palette)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MHXY Bot 图形启动器")
    parser.add_argument("--data-dir", help="私有运行数据目录")
    parser.add_argument("--config", help="显式 JSON 配置路径")
    parser.add_argument("--profile", help="模板配置档")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir)
    _configure_logging(data_dir)
    try:
        from core.windows import enable_dpi_awareness

        enable_dpi_awareness()
    except Exception:
        LOGGER.debug("DPI awareness could not be enabled before Qt startup", exc_info=True)

    app = QApplication(sys.argv[:1])
    _apply_dark_theme(app)
    window = LauncherWindow(data_dir=str(data_dir), config_path=args.config, profile=args.profile)
    window.show()
    return int(app.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
