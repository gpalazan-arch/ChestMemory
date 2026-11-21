"""System tray integration and programmatic high-DPI icon builder for ChestMemory."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget


def build_app_icon(size: int = 64) -> QIcon:
    """Render a crisp minimalist ChestMemory icon programmatically (no external asset needed)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    margin = size * 0.06
    bg_rect = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    bg_grad = QLinearGradient(0, 0, size, size)
    bg_grad.addColorStop(0.0, QColor("#171C26"))
    bg_grad.addColorStop(1.0, QColor("#0B0E14"))
    painter.setBrush(QBrush(bg_grad))
    painter.setPen(QPen(QColor("#2A3344"), max(1.0, size * 0.03)))
    painter.drawRoundedRect(bg_rect, size * 0.22, size * 0.22)

    # Stylized golden Fortnite chest silhouette
    gold_grad = QLinearGradient(0, size * 0.24, 0, size * 0.78)
    gold_grad.addColorStop(0.0, QColor("#FDE047"))
    gold_grad.addColorStop(0.55, QColor("#F59E0B"))
    gold_grad.addColorStop(1.0, QColor("#B45309"))

    chest_x = size * 0.22
    chest_w = size * 0.56
    lid_rect = QRectF(chest_x, size * 0.27, chest_w, size * 0.19)
    body_rect = QRectF(chest_x + size * 0.02, size * 0.48, chest_w - size * 0.04, size * 0.25)

    painter.setBrush(QBrush(gold_grad))
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
    painter.setBrush(QBrush(QColor("#FFFBEB")))
    painter.drawRoundedRect(latch_rect, size * 0.03, size * 0.03)

    # Subtle glowing dot
    painter.setBrush(QBrush(QColor("#22C55E")))
    painter.drawEllipse(QPointF(size * 0.80, size * 0.20), size * 0.075, size * 0.075)

    painter.end()
    return QIcon(pixmap)


class ChestTrayIcon(QSystemTrayIcon):
    """Windows system tray icon with quick actions and match toast notifications."""

    def __init__(
        self,
        parent: Optional[QWidget],
        on_toggle_window: Callable[[], None],
        on_toggle_topmost: Callable[[bool], None],
        on_scan_now: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_create_backup: Callable[[], None],
        on_quit: Callable[[], None],
        always_on_top: bool = True,
    ) -> None:
        super().__init__(build_app_icon(64), parent)
        self._on_toggle_window = on_toggle_window

        self.setToolTip("ChestMemory — Fortnite Chest Counter")

        menu = QMenu()
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #131822;
                color: #E5E9F0;
                border: 1px solid #263042;
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
                background-color: #1F293B;
                color: #FFFFFF;
            }
            QMenu::separator {
                height: 1px;
                background: #232C3D;
                margin: 5px 8px;
            }
            """
        )

        self.act_toggle = QAction("Показать / Скрыть окно", menu)
        self.act_toggle.triggered.connect(on_toggle_window)
        menu.addAction(self.act_toggle)

        self.act_topmost = QAction("Поверх других окон", menu)
        self.act_topmost.setCheckable(True)
        self.act_topmost.setChecked(always_on_top)
        self.act_topmost.toggled.connect(on_toggle_topmost)
        menu.addAction(self.act_topmost)

        menu.addSeparator()

        act_scan = QAction("Сканировать реплеи сейчас", menu)
        act_scan.triggered.connect(on_scan_now)
        menu.addAction(act_scan)

        act_settings = QAction("Настройки и статистика...", menu)
        act_settings.triggered.connect(on_open_settings)
        menu.addAction(act_settings)

        act_backup = QAction("Создать резервную копию", menu)
        act_backup.triggered.connect(on_create_backup)
        menu.addAction(act_backup)

        menu.addSeparator()

        act_quit = QAction("Выход", menu)
        act_quit.triggered.connect(on_quit)
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

    def notify_match(self, chests_opened: int, total_formatted: str) -> None:
        if self.supportsMessages():
            self.showMessage(
                "ChestMemory — Матч обработан",
                f"+{chests_opened} сундуков • Всего: {total_formatted}",
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
