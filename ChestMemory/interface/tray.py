"""System tray integration and programmatic high-DPI icon builder for ChestMemory."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget


def build_app_icon(size: int = 64) -> QIcon:
    """Render a crisp obsidian/crimson/ember ChestMemory icon programmatically."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin = size * 0.06
    bg_rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    bg_grad = QLinearGradient(0, 0, size, size)
    bg_grad.setColorAt(0.0, QColor("#1A0C10"))
    bg_grad.setColorAt(0.55, QColor("#0B080A"))
    bg_grad.setColorAt(1.0, QColor("#050507"))
    painter.setBrush(QBrush(bg_grad))
    painter.setPen(QPen(QColor("#7F1D1D"), max(1.2, size * 0.032)))
    painter.drawRoundedRect(bg_rect, size * 0.22, size * 0.22)

    # Stylized crimson-to-ember chest silhouette
    ember_grad = QLinearGradient(0, size * 0.24, 0, size * 0.78)
    ember_grad.setColorAt(0.0, QColor("#FB923C"))
    ember_grad.setColorAt(0.45, QColor("#F97316"))
    ember_grad.setColorAt(1.0, QColor("#DC2626"))

    chest_x = size * 0.22
    chest_w = size * 0.56
    lid_rect = QRectF(chest_x, size * 0.27, chest_w, size * 0.19)
    body_rect = QRectF(chest_x + size * 0.02, size * 0.48, chest_w - size * 0.04, size * 0.25)

    painter.setBrush(QBrush(ember_grad))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(lid_rect, size * 0.06, size * 0.06)
    painter.drawRoundedRect(body_rect, size * 0.05, size * 0.05)

    # Center latch
    latch_w = size * 0.12
    latch_h = size * 0.14
    latch_rect = QRectF(
        (size - latch_w) / 2.0,
        size * 0.42,
        latch_w,
        latch_h,
    )
    painter.setBrush(QBrush(QColor("#FFF7ED")))
    painter.drawRoundedRect(latch_rect, size * 0.03, size * 0.03)

    # Glowing ember status dot
    painter.setBrush(QBrush(QColor("#EF4444")))
    painter.drawEllipse(QPointF(size * 0.80, size * 0.20), size * 0.075, size * 0.075)

    painter.end()
    return QIcon(pixmap)


class ChestTrayIcon(QSystemTrayIcon):
    """Windows system tray icon with quick actions, click-through unlock, and notifications."""

    def __init__(
        self,
        parent: Optional[QWidget],
        on_toggle_window: Callable[[], None],
        on_toggle_topmost: Callable[[bool], None],
        on_toggle_click_through: Callable[[bool], None],
        on_scan_now: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_create_backup: Callable[[], None],
        on_quit: Callable[[], None],
        always_on_top: bool = True,
        click_through: bool = False,
    ) -> None:
        super().__init__(build_app_icon(64), parent)
        self._on_toggle_window = on_toggle_window

        self.setToolTip("ChestMemory — tg @newreligionsoft")

        menu = QMenu(parent)
        self._menu = menu
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #0D090B;
                color: #F4F0F1;
                border: 1px solid #3B181E;
                border-radius: 8px;
                padding: 6px;
                font-family: "Segoe UI";
                font-size: 12px;
            }
            QMenu::item {
                padding: 7px 16px;
                border-radius: 5px;
            }
            QMenu::item:selected {
                background-color: #2A1216;
                color: #FF8C42;
            }
            QMenu::separator {
                height: 1px;
                background: #2C1519;
                margin: 5px 8px;
            }
            """
        )

        self.act_toggle = QAction("Показать / Скрыть окно", menu)
        self.act_toggle.triggered.connect(lambda _checked=False: on_toggle_window())
        menu.addAction(self.act_toggle)

        self.act_click_through = QAction("Сквозь окно (не мешать игре)", menu)
        self.act_click_through.setCheckable(True)
        self.act_click_through.setChecked(click_through)
        self.act_click_through.toggled.connect(on_toggle_click_through)
        menu.addAction(self.act_click_through)

        self.act_topmost = QAction("Поверх других окон", menu)
        self.act_topmost.setCheckable(True)
        self.act_topmost.setChecked(always_on_top)
        self.act_topmost.toggled.connect(on_toggle_topmost)
        menu.addAction(self.act_topmost)

        menu.addSeparator()

        act_scan = QAction("Сканировать реплеи сейчас", menu)
        act_scan.triggered.connect(lambda _checked=False: on_scan_now())
        menu.addAction(act_scan)

        act_settings = QAction("Настройки и статистика...", menu)
        act_settings.triggered.connect(lambda _checked=False: on_open_settings())
        menu.addAction(act_settings)

        act_backup = QAction("Создать резервную копию", menu)
        act_backup.triggered.connect(lambda _checked=False: on_create_backup())
        menu.addAction(act_backup)

        menu.addSeparator()

        act_quit = QAction("Выход", menu)
        act_quit.triggered.connect(lambda _checked=False: on_quit())
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._handle_activated)

    def _handle_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_toggle_window()

    def sync_topmost(self, enabled: bool) -> None:
        self.act_topmost.blockSignals(True)
        self.act_topmost.setChecked(enabled)
        self.act_topmost.blockSignals(False)

    def sync_click_through(self, enabled: bool) -> None:
        self.act_click_through.blockSignals(True)
        self.act_click_through.setChecked(enabled)
        self.act_click_through.blockSignals(False)

    def notify_match(self, chests_opened: int, total_formatted: str) -> None:
        if self.supportsMessages():
            self.showMessage(
                "ChestMemory — Матч обработан",
                f"+{chests_opened} сундуков • Всего: {total_formatted}",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )

    def notify_info(self, title: str, message: str) -> None:
        if self.supportsMessages():
            self.showMessage(
                f"ChestMemory — {title}",
                message[:220],
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )

    def notify_error(self, title: str, message: str) -> None:
        if self.supportsMessages():
            self.showMessage(
                f"ChestMemory — {title}",
                message[:220],
                QSystemTrayIcon.MessageIcon.Warning,
                5000,
            )
