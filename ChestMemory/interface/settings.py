"""Settings, Match History, Diagnostics, and Backup/Restore dialog for ChestMemory."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.controller import ChestMemoryController
from core.statistics import format_chest_number
from detector.replay_parser import detect_default_fortnite_replay_dir


DIALOG_STYLESHEET = """
QDialog {
    background-color: #0E1117;
    color: #E6EDF3;
    font-family: "Segoe UI";
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #222B3A;
    border-radius: 10px;
    background-color: #131822;
    padding: 8px;
}
QTabBar::tab {
    background-color: #0E1117;
    color: #8B97AD;
    padding: 8px 16px;
    margin-right: 4px;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background-color: #131822;
    color: #F8FAFC;
    border: 1px solid #222B3A;
    border-bottom: none;
}
QLabel {
    color: #D8E0EE;
}
QLineEdit, QComboBox, QSpinBox {
    background-color: #0B0F17;
    color: #F1F5F9;
    border: 1px solid #253044;
    border-radius: 6px;
    padding: 6px 10px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: #F59E0B;
}
QPushButton {
    background-color: #1B2434;
    color: #E6EDF3;
    border: 1px solid #2B384F;
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #253147;
    border-color: #3D5070;
}
QPushButton#PrimaryButton {
    background-color: #D97706;
    color: #0B0E14;
    border: none;
}
QPushButton#PrimaryButton:hover {
    background-color: #F59E0B;
}
QTableWidget, QListWidget {
    background-color: #0B0F17;
    color: #E2E8F0;
    border: 1px solid #222B3A;
    border-radius: 6px;
    gridline-color: #1C2433;
}
QHeaderView::section {
    background-color: #161D2B;
    color: #94A3B8;
    border: none;
    border-bottom: 1px solid #253044;
    padding: 6px 8px;
    font-weight: 600;
}
QCheckBox {
    spacing: 8px;
    color: #D8E0EE;
}
"""


class SettingsDialog(QDialog):
    """Comprehensive dark-mode Settings, Match History, and Backup/Diagnostics dialog."""

    def __init__(
        self,
        controller: ChestMemoryController,
        parent: Optional[QWidget] = None,
        initial_tab: int = 0,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("ChestMemory — Настройки и статистика")
        self.resize(700, 520)
        self.setStyleSheet(DIALOG_STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_general_tab(), "Основные")
        self.tabs.addTab(self._build_history_tab(), "История матчей")
        self.tabs.addTab(self._build_diagnostics_tab(), "Ошибки и бэкапы")
        self.tabs.setCurrentIndex(initial_tab)
        root.addWidget(self.tabs)

        footer = QHBoxLayout()
        footer.addStretch()

        btn_cancel = QPushButton("Закрыть")
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)

        btn_save = QPushButton("Сохранить настройки")
        btn_save.setObjectName("PrimaryButton")
        btn_save.clicked.connect(self._save_settings)
        footer.addWidget(btn_save)
        root.addLayout(footer)

        self._refresh_history()
        self._refresh_diagnostics()

    # ------------------------------------------------------------------
    # Tab 1: General & Detector Settings
    # ------------------------------------------------------------------
    def _build_general_tab(self) -> QWidget:
        cfg = self.controller.config
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        # Replay directory row
        replay_row = QHBoxLayout()
        self.edit_replay_dir = QLineEdit(cfg.replay_dir)
        btn_browse_dir = QPushButton("Обзор...")
        btn_browse_dir.clicked.connect(self._browse_replay_dir)
        btn_auto_dir = QPushButton("Авто")
        btn_auto_dir.clicked.connect(
            lambda: self.edit_replay_dir.setText(str(detect_default_fortnite_replay_dir()))
        )
        replay_row.addWidget(self.edit_replay_dir, 1)
        replay_row.addWidget(btn_browse_dir)
        replay_row.addWidget(btn_auto_dir)
        form.addRow("Папка реплеев:", replay_row)

        # Detection mode
        self.combo_mode = QComboBox()
        self.combo_mode.addItem("Гибридный (Реплеи + Visual CV резерв)", "hybrid")
        self.combo_mode.addItem("Только анализ .replay файлов", "replay")
        self.combo_mode.addItem("Только компьютерное зрение (OpenCV)", "visual")
        idx = self.combo_mode.findData(cfg.detection_mode)
        self.combo_mode.setCurrentIndex(max(0, idx))
        form.addRow("Режим детектора:", self.combo_mode)

        # Oodle DLL path
        oodle_row = QHBoxLayout()
        self.edit_oodle = QLineEdit(cfg.oodle_dll_path)
        self.edit_oodle.setPlaceholderText("Автопоиск oo2core_*_win64.dll (необязательно)")
        btn_browse_oodle = QPushButton("DLL...")
        btn_browse_oodle.clicked.connect(self._browse_oodle_dll)
        oodle_row.addWidget(self.edit_oodle, 1)
        oodle_row.addWidget(btn_browse_oodle)
        form.addRow("Oodle DLL:", oodle_row)

        # Optional External Parser CLI Bridge
        self.edit_ext_cmd = QLineEdit(cfg.external_parser_cmd)
        self.edit_ext_cmd.setPlaceholderText('Опционально: "C:\\Tools\\ReplayCli.exe" "{replay}"')
        form.addRow("Внешний парсер CLI:", self.edit_ext_cmd)

        # Window opacity slider
        opacity_row = QHBoxLayout()
        self.slider_opacity = QSlider(Qt.Orientation.Horizontal)
        self.slider_opacity.setRange(55, 100)
        self.slider_opacity.setValue(int(round(cfg.window_opacity * 100)))
        self.lbl_opacity = QLabel(f"{self.slider_opacity.value()}%")
        self.slider_opacity.valueChanged.connect(lambda v: self.lbl_opacity.setText(f"{v}%"))
        opacity_row.addWidget(self.slider_opacity, 1)
        opacity_row.addWidget(self.lbl_opacity)
        form.addRow("Прозрачность окна:", opacity_row)

        # Backup retention
        self.spin_retention = QSpinBox()
        self.spin_retention.setRange(3, 100)
        self.spin_retention.setValue(cfg.backup_retention_count)
        form.addRow("Хранить бэкапов:", self.spin_retention)

        layout.addLayout(form)

        # Checkboxes
        self.chk_topmost = QCheckBox("Окно поверх других окон (Always on Top)")
        self.chk_topmost.setChecked(cfg.always_on_top)
        layout.addWidget(self.chk_topmost)

        self.chk_tray = QCheckBox("Сворачивать в системный трей при закрытии")
        self.chk_tray.setChecked(cfg.minimize_to_tray)
        layout.addWidget(self.chk_tray)

        self.chk_autoscan = QCheckBox("Автоматически сканировать новые реплеи при запуске")
        self.chk_autoscan.setChecked(cfg.auto_scan_existing)
        layout.addWidget(self.chk_autoscan)

        self.chk_autobackup = QCheckBox("Автоматическое резервное копирование после каждого матча")
        self.chk_autobackup.setChecked(cfg.auto_backup_enabled)
        layout.addWidget(self.chk_autobackup)

        # Quick manual actions
        actions_row = QHBoxLayout()
        btn_scan = QPushButton("Сканировать папку сейчас")
        btn_scan.clicked.connect(self._scan_now)
        btn_analyze_file = QPushButton("Анализировать .replay / видео...")
        btn_analyze_file.clicked.connect(self._analyze_manual_file)
        actions_row.addWidget(btn_scan)
        actions_row.addWidget(btn_analyze_file)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        layout.addStretch()
        return page

    # ------------------------------------------------------------------
    # Tab 2: Match History & Stats Summary
    # ------------------------------------------------------------------
    def _build_history_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        self.lbl_summary = QLabel()
        self.lbl_summary.setStyleSheet("color: #94A3B8; font-weight: 600;")
        layout.addWidget(self.lbl_summary)

        self.table_matches = QTableWidget(0, 6)
        self.table_matches.setHorizontalHeaderLabels(
            ["Дата (UTC)", "Сундуки", "Источник", "Атрибуция", "Киллы", "Файл"]
        )
        self.table_matches.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_matches.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_matches.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table_matches, 1)

        export_row = QHBoxLayout()
        btn_csv = QPushButton("Экспорт CSV")
        btn_csv.clicked.connect(self._export_csv)
        btn_json = QPushButton("Экспорт JSON")
        btn_json.clicked.connect(self._export_json)
        export_row.addWidget(btn_csv)
        export_row.addWidget(btn_json)
        export_row.addStretch()
        layout.addLayout(export_row)
        return page

    # ------------------------------------------------------------------
    # Tab 3: Failed Analyses & Backup Recovery
    # ------------------------------------------------------------------
    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)

        lbl_err = QLabel("Необработанные / ошибочные реплеи (никогда не записываются как 0):")
        lbl_err.setStyleSheet("color: #FCA5A5; font-weight: 600;")
        layout.addWidget(lbl_err)

        self.list_errors = QListWidget()
        self.list_errors.setMaximumHeight(140)
        layout.addWidget(self.list_errors)

        btn_retry = QPushButton("Повторить анализ ошибочных файлов")
        btn_retry.clicked.connect(self._retry_failed)
        layout.addWidget(btn_retry, alignment=Qt.AlignmentFlag.AlignLeft)

        lbl_bak = QLabel("Резервные копии SQLite (с проверкой SHA-256 и integrity_check):")
        lbl_bak.setStyleSheet("color: #93C5FD; font-weight: 600;")
        layout.addWidget(lbl_bak)

        self.list_backups = QListWidget()
        layout.addWidget(self.list_backups, 1)

        bak_row = QHBoxLayout()
        btn_create_bak = QPushButton("Создать бэкап сейчас")
        btn_create_bak.clicked.connect(self._create_backup)
        btn_restore_bak = QPushButton("Восстановить выбранный бэкап")
        btn_restore_bak.clicked.connect(self._restore_backup)
        bak_row.addWidget(btn_create_bak)
        bak_row.addWidget(btn_restore_bak)
        bak_row.addStretch()
        layout.addLayout(bak_row)
        return page

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _refresh_history(self) -> None:
        snap = self.controller.get_stats()
        self.lbl_summary.setText(
            f"Всего: {format_chest_number(snap.total_chests)}  •  "
            f"Матчей: {snap.total_matches}  •  "
            f"Месяц: {format_chest_number(snap.month_chests)}  •  "
            f"Среднее: {snap.average_per_match:.1f}  •  "
            f"Рекорд: {snap.best_match_chests}"
        )
        matches = self.controller.get_recent_matches(limit=150)
        self.table_matches.setRowCount(len(matches))
        for row_idx, match in enumerate(matches):
            vals = [
                match.played_at_utc().strftime("%Y-%m-%d %H:%M"),
                str(match.chests_opened),
                match.source.value,
                match.attribution_mode.value,
                "—" if match.eliminations is None else str(match.eliminations),
                match.file_name,
            ]
            for col_idx, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if col_idx in (1, 4):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_matches.setItem(row_idx, col_idx, item)

    def _refresh_diagnostics(self) -> None:
        self.list_errors.clear()
        failed = self.controller.get_failed_analyses()
        if not failed:
            self.list_errors.addItem("Ошибок анализа нет.")
        else:
            for rec in failed:
                ts = rec.attempted_at.strftime("%Y-%m-%d %H:%M")
                self.list_errors.addItem(f"[{ts}] {rec.file_name} ({rec.status.value}): {rec.error_message}")

        self.list_backups.clear()
        for info in self.controller.list_backups():
            kb = info.size_bytes / 1024.0
            label = f"{info.path.name}  ({kb:.1f} KB  •  sha256:{info.sha256[:10]})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, str(info.path))
            self.list_backups.addItem(item)

    def _browse_replay_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку реплеев Fortnite (Demos)",
            self.edit_replay_dir.text() or str(Path.home()),
        )
        if chosen:
            self.edit_replay_dir.setText(chosen)

    def _browse_oodle_dll(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите oo2core_*_win64.dll",
            str(Path.home()),
            "Oodle DLL (oo2core*.dll);;All Files (*)",
        )
        if chosen:
            self.edit_oodle.setText(chosen)

    def _save_settings(self) -> None:
        updated = replace(
            self.controller.config,
            replay_dir=self.edit_replay_dir.text().strip(),
            detection_mode=str(self.combo_mode.currentData()),
            oodle_dll_path=self.edit_oodle.text().strip(),
            external_parser_cmd=self.edit_ext_cmd.text().strip(),
            window_opacity=self.slider_opacity.value() / 100.0,
            backup_retention_count=int(self.spin_retention.value()),
            always_on_top=self.chk_topmost.isChecked(),
            minimize_to_tray=self.chk_tray.isChecked(),
            auto_scan_existing=self.chk_autoscan.isChecked(),
            auto_backup_enabled=self.chk_autobackup.isChecked(),
        )
        self.controller.update_config(updated, restart_monitoring=True)
        self.accept()

    def _scan_now(self) -> None:
        count = self.controller.scan_replay_directory_now()
        self._refresh_history()
        self._refresh_diagnostics()
        QMessageBox.information(
            self,
            "Сканирование завершено",
            f"Обработано новых достоверных реплеев: {count}.",
        )

    def _analyze_manual_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл реплея Fortnite или видеозапись матча",
            self.edit_replay_dir.text() or str(Path.home()),
            "Fortnite Replay & Video (*.replay *.mp4 *.mkv *.avi);;All Files (*)",
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.suffix.lower() == ".replay":
            res = self.controller.process_replay_file(path, force_retry_failed=True)
        else:
            res = self.controller.analyze_video_file(path)

        self._refresh_history()
        self._refresh_diagnostics()
        if res is None:
            QMessageBox.information(self, "Уже обработано", "Этот файл уже сохранён в базе данных.")
        elif res.is_reliable:
            QMessageBox.information(
                self,
                "Анализ завершён",
                f"Определено открытых сундуков: {res.chests_opened}\n"
                f"Источник: {res.source.value}\n"
                f"Атрибуция: {res.attribution_mode.value}",
            )
        else:
            QMessageBox.warning(
                self,
                "Анализ невозможен",
                f"Файл не записан как 0.\nПричина ({res.status.value}):\n{res.error_message}",
            )

    def _retry_failed(self) -> None:
        count = self.controller.retry_failed_replays()
        self._refresh_history()
        self._refresh_diagnostics()
        QMessageBox.information(
            self,
            "Повторный анализ",
            f"Успешно восстановлено и обработано реплеев: {count}.",
        )

    def _create_backup(self) -> None:
        info = self.controller.create_manual_backup()
        self._refresh_diagnostics()
        QMessageBox.information(
            self,
            "Бэкап создан",
            f"Сохранена проверенная копия:\n{info.path.name}",
        )

    def _restore_backup(self) -> None:
        current = self.list_backups.currentItem()
        if current is None:
            return
        backup_path = current.data(Qt.ItemDataRole.UserRole)
        reply = QMessageBox.question(
            self,
            "Восстановление базы данных",
            f"Восстановить базу данных из резервной копии?\n{Path(backup_path).name}\n\n"
            "Текущая копия будет предварительно сохранена.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.controller.restore_from_backup(backup_path)
            self._refresh_history()
            self._refresh_diagnostics()
            QMessageBox.information(self, "Восстановлено", "Данные успешно восстановлены.")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка восстановления", str(exc))

    def _export_csv(self) -> None:
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт статистики в CSV",
            str(Path.home() / "chestmemory_stats.csv"),
            "CSV Files (*.csv)",
        )
        if dest:
            self.controller.stats_service.export_csv(dest)
            QMessageBox.information(self, "Экспорт завершён", f"Сохранено: {dest}")

    def _export_json(self) -> None:
        dest, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт статистики в JSON",
            str(Path.home() / "chestmemory_stats.json"),
            "JSON Files (*.json)",
        )
        if dest:
            self.controller.stats_service.export_json(dest)
            QMessageBox.information(self, "Экспорт завершён", f"Сохранено: {dest}")
