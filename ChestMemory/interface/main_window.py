"""Compact frameless obsidian/crimson/ember floating counter widget for ChestMemory (PySide6).

Features:
- Dark obsidian-black, crimson, and ember-orange visual identity
- Instant snappy hero counter animation with fiery ember pulse on +1
- Native Windows Click-Through + No-Activate mode (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
  so mouse cursor passes straight through to Fortnite without interrupting gameplay
- Watermark: tg @newreligionsoft
"""

from __future__ import annotations

import ctypes
import sys
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
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0.25, y2: 1,
        stop: 0 #160B0E,
        stop: 0.35 #0C090B,
        stop: 1 #070608
    );
    border: 1px solid #3A161D;
    border-radius: 16px;
}
QFrame#RootCard[clickThrough="true"] {
    border: 1px solid #6E231C;
}
QFrame#TopAccent {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #7F1D1D,
        stop: 0.42 #EF4444,
        stop: 0.78 #F97316,
        stop: 1 #FB923C
    );
    min-height: 3px;
    max-height: 3px;
    border-radius: 1px;
    border: none;
}
QLabel#BrandDiamond {
    color: #EF4444;
    font-family: "Segoe UI";
    font-size: 11px;
    font-weight: 700;
}
QLabel#HeaderTitle {
    color: #F5F2F3;
    font-family: "Segoe UI";
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 2.3px;
}
QPushButton#HeaderIconBtn {
    background: transparent;
    color: #9A7D82;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    padding: 2px 6px;
}
QPushButton#HeaderIconBtn:hover {
    background-color: #291217;
    color: #FF8A4C;
}
QPushButton#HeaderIconBtn[locked="true"] {
    background-color: #2E1315;
    color: #F97316;
}
QFrame#Divider {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 #180B0E,
        stop: 0.5 #4A1B22,
        stop: 1 #180B0E
    );
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QLabel#HeroCount {
    color: #FAFAFA;
    font-family: "Segoe UI";
    font-size: 46px;
    font-weight: 700;
    letter-spacing: 1px;
}
QLabel#HeroCount[highlight="true"] {
    color: #FF6B2B;
}
QLabel#HeroSubtitle {
    color: #C85A4B;
    font-family: "Segoe UI";
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 3.0px;
}
QFrame#StatsPanel {
    background-color: rgba(20, 11, 14, 210);
    border: 1px solid #2A1318;
    border-radius: 10px;
}
QLabel#StatLabel {
    color: #A48F94;
    font-family: "Segoe UI";
    font-size: 12px;
    font-weight: 500;
}
QLabel#StatValue {
    color: #F5F5F7;
    font-family: "Segoe UI";
    font-size: 14px;
    font-weight: 700;
}
QLabel#StatValueAccent {
    color: #FB923C;
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
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.3px;
    color: #B49DA2;
}
QPushButton#StatusPill:hover {
    color: #FFD1B8;
}
QLabel#Watermark {
    color: #7D443D;
    font-family: "Consolas", "Segoe UI";
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.9px;
}
"""

STATUS_COLORS = {
    "active": "#F97316",
    "analyzing": "#FB923C",
    "warning": "#F59E0B",
    "error": "#EF4444",
    "idle": "#6B575B",
}


class ChestMemoryMainWindow(QWidget):
    """Compact frameless obsidian/crimson/ember desktop widget displaying opened chest totals."""

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
        self.setFixedSize(324, 386)

        self._build_ui()
        self._apply_window_flags()
        self.setWindowOpacity(self.controller.config.window_opacity)
        self._restore_window_position()

        # 60 FPS smooth ease-out counter animation timer
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._tick_animation)

        # Gentle Win32 topmost z-order keepalive for Borderless Fullscreen games (SWP_NOACTIVATE)
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(1500)
        self._topmost_timer.timeout.connect(self._apply_win32_styles)
        if self.controller.config.always_on_top:
            self._topmost_timer.start()

        # System tray integration
        self.tray = ChestTrayIcon(
            parent=self,
            on_toggle_window=self.toggle_visibility,
            on_toggle_topmost=self.set_always_on_top,
            on_toggle_click_through=self.set_click_through,
            on_scan_now=self._scan_now,
            on_open_settings=lambda _checked=False: self.open_settings(0),
            on_create_backup=self._create_backup,
            on_quit=self.quit_application,
            always_on_top=self.controller.config.always_on_top,
            click_through=self.controller.config.click_through,
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
        self._sync_click_through_visuals()

    def _build_ui(self) -> None:
        self.setStyleSheet(MAIN_WINDOW_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)

        self.root_card = QFrame()
        self.root_card.setObjectName("RootCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(180, 30, 20, 75))
        self.root_card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(self.root_card)
        card_layout.setContentsMargins(18, 10, 18, 12)
        card_layout.setSpacing(0)

        # ── Top Crimson-Ember Accent Bar ────────────────────────────────
        accent = QFrame()
        accent.setObjectName("TopAccent")
        card_layout.addWidget(accent)
        card_layout.addSpacing(10)

        # ── Header Bar ──────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 10)
        header.setSpacing(6)

        lbl_diamond = QLabel("◆")
        lbl_diamond.setObjectName("BrandDiamond")
        header.addWidget(lbl_diamond)

        lbl_title = QLabel("CHEST MEMORY")
        lbl_title.setObjectName("HeaderTitle")
        header.addWidget(lbl_title)
        header.addStretch()

        self.btn_lock = QPushButton("🔓")
        self.btn_lock.setObjectName("HeaderIconBtn")
        self.btn_lock.setToolTip(
            "Включить режим «Сквозь окно» (курсор проходит в Fortnite, не прерывая игру).\n"
            "Отключить можно через значок ChestMemory в трее."
        )
        self.btn_lock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_lock.clicked.connect(
            lambda _checked=False: self.set_click_through(not self.controller.config.click_through)
        )
        header.addWidget(self.btn_lock)

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
        hero_box.setContentsMargins(0, 20, 0, 20)
        hero_box.setSpacing(3)

        self.lbl_hero_count = QLabel("0")
        self.lbl_hero_count.setObjectName("HeroCount")
        self.lbl_hero_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_box.addWidget(self.lbl_hero_count)

        lbl_sub = QLabel("OPENED CHESTS")
        lbl_sub.setObjectName("HeroSubtitle")
        lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_box.addWidget(lbl_sub)
        card_layout.addLayout(hero_box)

        # ── Period Stats Recessed Panel ─────────────────────────────────
        stats_panel = QFrame()
        stats_panel.setObjectName("StatsPanel")
        stats_box = QVBoxLayout(stats_panel)
        stats_box.setContentsMargins(14, 11, 14, 11)
        stats_box.setSpacing(8)

        self.lbl_today_val = self._add_stat_row(stats_box, "Сегодня")
        self.lbl_week_val = self._add_stat_row(stats_box, "Неделя")
        self.lbl_last_val = self._add_stat_row(stats_box, "Последняя катка", accent=True)
        card_layout.addWidget(stats_panel)

        card_layout.addSpacing(10)

        # ── Monitoring Status Footer ────────────────────────────────────
        status_row = QHBoxLayout()
        status_row.setContentsMargins(2, 0, 2, 0)
        status_row.setSpacing(7)

        self.lbl_status_dot = QLabel("●")
        self.lbl_status_dot.setStyleSheet("color: #F97316; font-size: 10px;")
        status_row.addWidget(self.lbl_status_dot)

        self.btn_status = QPushButton("MONITORING ACTIVE")
        self.btn_status.setObjectName("StatusPill")
        self.btn_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_status.clicked.connect(lambda _checked=False: self.open_settings(2))
        status_row.addWidget(self.btn_status, 1)
        card_layout.addLayout(status_row)

        card_layout.addSpacing(6)

        # ── Subtle Brand Watermark ──────────────────────────────────────
        wm_row = QHBoxLayout()
        wm_row.setContentsMargins(2, 0, 2, 0)
        self.lbl_lock_hint = QLabel("")
        self.lbl_lock_hint.setStyleSheet(
            'color: #9A4A36; font-family: "Segoe UI"; font-size: 9px; font-weight: 600;'
        )
        wm_row.addWidget(self.lbl_lock_hint)
        wm_row.addStretch()

        lbl_watermark = QLabel("tg @newreligionsoft")
        lbl_watermark.setObjectName("Watermark")
        lbl_watermark.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        wm_row.addWidget(lbl_watermark)
        card_layout.addLayout(wm_row)

        outer.addWidget(self.root_card)

    @staticmethod
    def _add_stat_row(parent_layout: QVBoxLayout, label_text: str, accent: bool = False) -> QLabel:
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setObjectName("StatLabel")
        val = QLabel("0")
        val.setObjectName("StatValueAccent" if accent else "StatValue")
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        row.addStretch()
        row.addWidget(val)
        parent_layout.addLayout(row)
        return val

    # ------------------------------------------------------------------
    # Window flags, Win32 click-through / topmost, position persistence
    # ------------------------------------------------------------------
    def _apply_window_flags(self) -> None:
        click_through = bool(self.controller.config.click_through)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.controller.config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if click_through:
            flags |= Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus

        was_visible = self.isVisible()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, click_through)
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

        if hasattr(self, "_topmost_timer"):
            if self.controller.config.always_on_top:
                self._topmost_timer.start()
            else:
                self._topmost_timer.stop()

        self._sync_click_through_visuals()
        self._apply_win32_styles()

    def _sync_click_through_visuals(self) -> None:
        if not hasattr(self, "root_card"):
            return
        locked = bool(self.controller.config.click_through)
        self.root_card.setProperty("clickThrough", "true" if locked else "false")
        self.root_card.style().unpolish(self.root_card)
        self.root_card.style().polish(self.root_card)

        self.btn_lock.setText("🔒" if locked else "🔓")
        self.btn_lock.setProperty("locked", "true" if locked else "false")
        self.btn_lock.style().unpolish(self.btn_lock)
        self.btn_lock.style().polish(self.btn_lock)
        self.lbl_lock_hint.setText("CLICK-THROUGH" if locked else "")

    def _apply_win32_styles(self) -> None:
        """Apply official Win32 extended styles on ChestMemory's OWN HWND (100% EAC-safe).

        - Always-on-top: SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE) keeps the widget
          visible over Borderless/Windowed Fullscreen games without stealing focus.
        - Click-Through: WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE lets mouse
          movement & clicks pass straight into Fortnite so gameplay is never interrupted.
        """
        if sys.platform != "win32" or not self.isVisible():
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = ctypes.c_void_p(int(self.winId()))

            # 1. Configure WS_EX_TRANSPARENT + WS_EX_NOACTIVATE cleanly (64-bit safe)
            is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
            get_long = user32.GetWindowLongPtrW if is_64bit else user32.GetWindowLongW
            set_long = user32.SetWindowLongPtrW if is_64bit else user32.SetWindowLongW
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_LAYERED = 0x00080000
            WS_EX_NOACTIVATE = 0x08000000

            current_ex = int(get_long(hwnd, GWL_EXSTYLE))
            if self.controller.config.click_through:
                desired_ex = current_ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
            else:
                desired_ex = current_ex & ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)

            style_changed = desired_ex != current_ex
            if style_changed:
                set_long(hwnd, GWL_EXSTYLE, desired_ex)

            # 2. Enforce topmost without activating or stealing focus from Fortnite
            if self.controller.config.always_on_top or style_changed:
                set_window_pos = user32.SetWindowPos
                set_window_pos.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_uint,
                ]
                set_window_pos.restype = ctypes.c_bool

                z_target = ctypes.c_void_p(-1 if self.controller.config.always_on_top else -2)
                swp_flags = 0x0002 | 0x0001 | 0x0010  # SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                if style_changed:
                    swp_flags |= 0x0020  # SWP_FRAMECHANGED
                set_window_pos(hwnd, z_target, 0, 0, 0, 0, swp_flags)
        except Exception:
            pass

    def _restore_window_position(self) -> None:
        cfg = self.controller.config
        if cfg.window_x is not None and cfg.window_y is not None:
            self.move(int(cfg.window_x), int(cfg.window_y))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.controller.config.click_through:
            event.ignore()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.controller.config.click_through:
            event.ignore()
            return
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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_win32_styles()

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
            delta = abs(snap.total_chests - self._anim_state.displayed_value)
            dur = 260.0 if delta <= 3 else 620.0
            started = self._anim_state.animate_to(snap.total_chests, duration_ms=dur)
            if started:
                self._set_hero_highlight(True)
                self._highlight_ticks = 34
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
        color = STATUS_COLORS.get(status.level, "#6B575B")
        self.lbl_status_dot.setStyleSheet(f"color: {color}; font-size: 10px;")
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
            self._apply_win32_styles()
            if not self.controller.config.click_through:
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

    def set_click_through(self, enabled: bool) -> None:
        updated = replace(self.controller.config, click_through=enabled)
        self.controller.config = updated
        self.controller.db.save_config(updated)
        self.tray.sync_click_through(enabled)
        self._apply_window_flags()
        if enabled:
            self.tray.notify_info(
                "Режим «Сквозь окно» ВКЛ",
                "Курсор больше не цепляет окно в Fortnite. Отключить можно ПКМ по значку в трее.",
            )
        else:
            self.tray.notify_info(
                "Режим «Сквозь окно» ВЫКЛ",
                "Окно снова интерактивно — его можно перетаскивать и нажимать ⚙.",
            )

    def open_settings(self, initial_tab: int = 0) -> None:
        dialog = SettingsDialog(self.controller, parent=self, initial_tab=initial_tab)
        if dialog.exec():
            self.setWindowOpacity(self.controller.config.window_opacity)
            self.tray.sync_topmost(self.controller.config.always_on_top)
            self.tray.sync_click_through(self.controller.config.click_through)
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
