"""Compact frameless dark-mode floating counter window for ChestMemory (PySide6).

Matches the exact target visual architecture:
┌─────────────────────────────┐
│ CHEST MEMORY             ⚙  │
├─────────────────────────────┤
│                             │
│          12 458             │
│       OPENED CHESTS         │
│                             │
├─────────────────────────────┤
│ Сегодня              126    │
│ Неделя               843    │
│ Последняя катка       18    │
│                             │
│ ● MONITORING ACTIVE         │
└─────────────────────────────┘
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.controller import ChestMemoryController, StatusEvent
from core.counter import CounterAnimationState
from core.statistics import format_chest_number
from database.models import MatchRecord, StatsSnapshot
from interface.settings import SettingsDialog
from interface.tray import ChestTrayIcon, build_app_icon


class _ControllerQtBridge(QObject):
    """Marshals background controller callbacks onto the Qt GUI thread safely."""

    stats_updated = Signal(object)
    status_changed = Signal(object)
    match_recorded = Signal(object)
    analysis_error = Signal(str, str)


MAIN_WINDOW_QSS = """
QFrame#RootCard {
    background-color: #0E1117;
    border: 1px solid #222B3A;
    border-radius: 16px;
}
QLabel#HeaderTitle {
    color: #E6EDF3;
    font-family: "Segoe UI";
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 2.2px;
}
QPushButton#HeaderIconBtn {
    background: transparent;
    color: #8B97AD;
    border: none;
    border-radius: 6px;
    font-size: 15px;
    padding: 2px 6px;
}
QPushButton#HeaderIconBtn:hover {
    background-color: #1B2332;
    color: #F8FAFC;
}
QFrame#Divider {
    background-color: #1E2634;
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QLabel#HeroCount {
    color: #F8FAFC;
    font-family: "Segoe UI";
    font-size: 46px;
    font-weight: 700;
    letter-spacing: 1px;
}
QLabel#HeroCount[highlight="true"] {
    color: #FBBF24;
}
QLabel#HeroSubtitle {
    color: #6B7890;
    font-family: "Segoe UI";
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2.6px;
}
QLabel#StatLabel {
    color: #8C99B0;
    font-family: "Segoe UI";
    font-size: 13px;
    font-weight: 500;
}
QLabel#StatValue {
    color: #F1F5F9;
    font-family: "Segoe UI";
    font-size: 14px;
    font-weight: 700;
}
QPushButton#StatusPill {
    background: transparent;
    border: none;
    text-align: left;
    padding: 2px 0px;
    font-family: "Segoe UI";
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.4px;
    color: #94A3B8;
}
QPushButton#StatusPill:hover {
    color: #E2E8F0;
}
"""

STATUS_COLORS = {
    "active": "#22C55E",
    "analyzing": "#F59E0B",
    "warning": "#F59E0B",
    "error": "#EF4444",
    "idle": "#64748B",
}


class ChestMemoryMainWindow(QWidget):
    """Compact frameless always-on-top desktop widget displaying opened chest totals."""

    def __init__(self, controller: ChestMemoryController) -> None:
        super().__init__()
        self.controller = controller
        self._drag_offset: Optional[QPoint] = None
        self._quitting = False
        self._anim_state = CounterAnimationState()
        self._highlight_ticks = 0

        self.setWindowTitle("CHESTMEMORY")
        self.setWindowIcon(build_app_icon(64))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(316, 352)
        self._apply_window_flags()
        self.setWindowOpacity(self.controller.config.window_opacity)

        self._build_ui()
        self._restore_window_position()

        # 60 FPS smooth ease-out counter animation timer
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._tick_animation)

        # System tray integration
        self.tray = ChestTrayIcon(
            parent=self,
            on_toggle_window=self.toggle_visibility,
            on_toggle_topmost=self.set_always_on_top,
            on_scan_now=self._scan_now,
            on_open_settings=lambda _checked=False: self.open_settings(0),
            on_create_backup=self._create_backup,
            on_quit=self.quit_application,
            always_on_top=self.controller.config.always_on_top,
        )
        self.tray.show()

        # Connect thread-safe bridge to background controller
        self._bridge = _ControllerQtBridge(self)
        self._bridge.stats_updated.connect(self._on_stats_updated)
        self._bridge.status_changed.connect(self._on_status_changed)
        self._bridge.match_recorded.connect(self._on_match_recorded)
        self._bridge.analysis_error.connect(self._on_analysis_error)

        self.controller.on_stats_updated(self._bridge.stats_updated.emit)
        self.controller.on_status_changed(self._bridge.status_changed.emit)
        self.controller.on_match_recorded(self._bridge.match_recorded.emit)
        self.controller.on_analysis_error(self._bridge.analysis_error.emit)

        # Render initial authoritative stats from SQLite
        initial = self.controller.get_stats()
        self._anim_state.set_immediate(initial.total_chests)
        self._apply_stats_labels(initial, animate=False)
        self._on_status_changed(self.controller.current_status)

    def _build_ui(self) -> None:
        self.setStyleSheet(MAIN_WINDOW_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        card = QFrame()
        card.setObjectName("RootCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 140))
        card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 14, 18, 16)
        card_layout.setSpacing(0)

        # ── Header Bar ──────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 12)
        lbl_title = QLabel("CHEST MEMORY")
        lbl_title.setObjectName("HeaderTitle")
        header.addWidget(lbl_title)
        header.addStretch()

        btn_min = QPushButton("—")
        btn_min.setObjectName("HeaderIconBtn")
        btn_min.setToolTip("Свернуть в трей")
        btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_min.clicked.connect(self.hide_to_tray)
        header.addWidget(btn_min)

        btn_settings = QPushButton("⚙")
        btn_settings.setObjectName("HeaderIconBtn")
        btn_settings.setToolTip("Настройки, история и бэкапы")
        btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_settings.clicked.connect(lambda _checked=False: self.open_settings(0))
        header.addWidget(btn_settings)
        card_layout.addLayout(header)

        div1 = QFrame()
        div1.setObjectName("Divider")
        card_layout.addWidget(div1)

        # ── Hero Counter Block ──────────────────────────────────────────
        hero_box = QVBoxLayout()
        hero_box.setContentsMargins(0, 26, 0, 26)
        hero_box.setSpacing(4)

        self.lbl_hero_count = QLabel("0")
        self.lbl_hero_count.setObjectName("HeroCount")
        self.lbl_hero_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_box.addWidget(self.lbl_hero_count)

        lbl_sub = QLabel("OPENED CHESTS")
        lbl_sub.setObjectName("HeroSubtitle")
        lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_box.addWidget(lbl_sub)
        card_layout.addLayout(hero_box)

        div2 = QFrame()
        div2.setObjectName("Divider")
        card_layout.addWidget(div2)

        # ── Period Stats Rows ───────────────────────────────────────────
        stats_box = QVBoxLayout()
        stats_box.setContentsMargins(0, 16, 0, 14)
        stats_box.setSpacing(9)

        self.lbl_today_val = self._add_stat_row(stats_box, "Сегодня")
        self.lbl_week_val = self._add_stat_row(stats_box, "Неделя")
        self.lbl_last_val = self._add_stat_row(stats_box, "Последняя катка")
        card_layout.addLayout(stats_box)

        # ── Monitoring Status Footer ────────────────────────────────────
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 4, 0, 0)
        status_row.setSpacing(8)

        self.lbl_status_dot = QLabel("●")
        self.lbl_status_dot.setStyleSheet("color: #22C55E; font-size: 11px;")
        status_row.addWidget(self.lbl_status_dot)

        self.btn_status = QPushButton("MONITORING ACTIVE")
        self.btn_status.setObjectName("StatusPill")
        self.btn_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_status.clicked.connect(lambda _checked=False: self.open_settings(2))
        status_row.addWidget(self.btn_status, 1)
        card_layout.addLayout(status_row)

        outer.addWidget(card)

    @staticmethod
    def _add_stat_row(parent_layout: QVBoxLayout, label_text: str) -> QLabel:
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setObjectName("StatLabel")
        val = QLabel("0")
        val.setObjectName("StatValue")
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        row.addStretch()
        row.addWidget(val)
        parent_layout.addLayout(row)
        return val

    # ------------------------------------------------------------------
    # Window flags, position persistence, mouse dragging
    # ------------------------------------------------------------------
    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.controller.config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def _restore_window_position(self) -> None:
        cfg = self.controller.config
        if cfg.window_x is not None and cfg.window_y is not None:
            self.move(int(cfg.window_x), int(cfg.window_y))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            pos = self.pos()
            updated = replace(self.controller.config, window_x=pos.x(), window_y=pos.y())
            self.controller.config = updated
            self.controller.db.save_config(updated)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._quitting and self.controller.config.minimize_to_tray:
            event.ignore()
            self.hide()
            return
        self.controller.stop_monitoring()
        self.tray.hide()
        event.accept()

    # ------------------------------------------------------------------
    # Counter animation & reactive updates
    # ------------------------------------------------------------------
    def _tick_animation(self) -> None:
        val = self._anim_state.step(16.0)
        self.lbl_hero_count.setText(format_chest_number(val))

        if self._highlight_ticks > 0:
            self._highlight_ticks -= 1
            if self._highlight_ticks == 0:
                self._set_hero_highlight(False)

        if not self._anim_state.is_animating and self._highlight_ticks == 0:
            self._anim_timer.stop()

    def _set_hero_highlight(self, active: bool) -> None:
        self.lbl_hero_count.setProperty("highlight", "true" if active else "false")
        self.lbl_hero_count.style().unpolish(self.lbl_hero_count)
        self.lbl_hero_count.style().polish(self.lbl_hero_count)

    def _apply_stats_labels(self, snap: StatsSnapshot, animate: bool) -> None:
        self.lbl_today_val.setText(format_chest_number(snap.today_chests))
        self.lbl_week_val.setText(format_chest_number(snap.week_chests))
        self.lbl_last_val.setText(
            "—" if snap.last_match_chests is None else format_chest_number(snap.last_match_chests)
        )
        if animate:
            started = self._anim_state.animate_to(snap.total_chests, duration_ms=780.0)
            if started:
                self._set_hero_highlight(True)
                self._highlight_ticks = 55
                if not self._anim_timer.isActive():
                    self._anim_timer.start()
            else:
                self.lbl_hero_count.setText(format_chest_number(snap.total_chests))
        else:
            self._anim_state.set_immediate(snap.total_chests)
            self.lbl_hero_count.setText(format_chest_number(snap.total_chests))

    def _on_stats_updated(self, snapshot: StatsSnapshot) -> None:
        self._apply_stats_labels(snapshot, animate=True)

    def _on_status_changed(self, status: StatusEvent) -> None:
        color = STATUS_COLORS.get(status.level, "#64748B")
        self.lbl_status_dot.setStyleSheet(f"color: {color}; font-size: 11px;")
        self.btn_status.setText(status.label)
        self.btn_status.setToolTip(status.detail or status.label)

    def _on_match_recorded(self, match: MatchRecord) -> None:
        snap = self.controller.get_stats()
        self.tray.notify_match(match.chests_opened, format_chest_number(snap.total_chests))

    def _on_analysis_error(self, source: str, message: str) -> None:
        self.tray.notify_error(source, message)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def hide_to_tray(self) -> None:
        self.hide()

    def set_always_on_top(self, enabled: bool) -> None:
        updated = replace(self.controller.config, always_on_top=enabled)
        self.controller.config = updated
        self.controller.db.save_config(updated)
        self.tray.sync_topmost(enabled)
        self._apply_window_flags()

    def open_settings(self, initial_tab: int = 0) -> None:
        dialog = SettingsDialog(self.controller, parent=self, initial_tab=initial_tab)
        if dialog.exec():
            self.setWindowOpacity(self.controller.config.window_opacity)
            self.tray.sync_topmost(self.controller.config.always_on_top)
            self._apply_window_flags()
            self._apply_stats_labels(self.controller.get_stats(), animate=True)

    def _scan_now(self) -> None:
        self.controller.scan_replay_directory_now()

    def _create_backup(self) -> None:
        info = self.controller.create_manual_backup()
        self.tray.notify_info("Резервная копия", f"Сохранена: {info.path.name}")

    def quit_application(self) -> None:
        self._quitting = True
        self.controller.stop_monitoring()
        self.tray.hide()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()
