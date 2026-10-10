from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDockWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from inventory_manager_mini.core.models import ItemRow
from inventory_manager_mini.core.services import validate_purchase_url
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.error_handling import run_guarded

_INVALID_INDEX = QModelIndex()
_ModelIndex = QModelIndex | QPersistentModelIndex


class LowStockTableModel(QAbstractTableModel):
    URL_COLUMN = 7
    _COLUMNS = (
        ("管理番号", "code"),
        ("品名", "name"),
        ("メーカー型番", "manufacturer_part_number"),
        ("数量", "quantity"),
        ("閾値", "reorder_threshold"),
        ("推奨発注数", "reorder_quantity"),
        ("発注主体", "purchaser_name"),
        ("販売ページ", "purchase_url"),
    )
    _NUMERIC_COLUMNS = {3, 4, 5}
    _BUTTON_LABEL = "開く"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[ItemRow] = []

    def rowCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._COLUMNS)

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        if not 0 <= index.column() < len(self._COLUMNS):
            return None

        value = getattr(self._rows[index.row()], self._COLUMNS[index.column()][1])
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == self.URL_COLUMN:
                return self._BUTTON_LABEL
            if value is None:
                return ""
            return f"{value:,}" if index.column() in self._NUMERIC_COLUMNS else str(value)
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in self._NUMERIC_COLUMNS:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < len(self._COLUMNS)
        ):
            return self._COLUMNS[section][0]
        return None

    def set_rows(self, rows: list[ItemRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> ItemRow:
        return self._rows[row]


class OpenUrlButtonDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._error_parent = parent

    @staticmethod
    def _url_of(index: _ModelIndex) -> str:
        value = index.data(Qt.ItemDataRole.UserRole)
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def button_rect(cell_rect: QRect) -> QRect:
        return cell_rect.adjusted(4, 2, -4, -2)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: _ModelIndex,
    ) -> None:
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawPrimitive(
            QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget
        )
        button = QStyleOptionButton()
        button.rect = self.button_rect(option.rect)
        button.text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        button.state = QStyle.StateFlag.State_Raised
        if self._url_of(index):
            button.state |= QStyle.StateFlag.State_Enabled
        style.drawControl(QStyle.ControlElement.CE_PushButton, button, painter, option.widget)

    def editorEvent(
        self,
        event: QEvent,
        model: QAbstractItemModel,
        option: QStyleOptionViewItem,
        index: _ModelIndex,
    ) -> bool:
        if (
            not isinstance(event, QMouseEvent)
            or event.type() != QEvent.Type.MouseButtonRelease
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        url = self._url_of(index)
        if not url or not self.button_rect(option.rect).contains(event.position().toPoint()):
            return False
        self.open_url(url)
        return True

    def open_url(self, url: str) -> None:
        succeeded, validated = run_guarded(self._error_parent, lambda: validate_purchase_url(url))
        if succeeded and validated is not None:
            QDesktopServices.openUrl(QUrl(validated))


class AlertPanel(QDockWidget):
    item_activated = Signal(int)

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setObjectName("alert_panel")

        self.model = LowStockTableModel(self)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.url_delegate = OpenUrlButtonDelegate(self.table)
        self.table.setItemDelegateForColumn(LowStockTableModel.URL_COLUMN, self.url_delegate)
        for column, width in enumerate((90, 160, 120, 60, 60, 90, 100, 80)):
            self.table.setColumnWidth(column, width)
        self.table.doubleClicked.connect(self._on_double_clicked)
        self.setWidget(self.table)
        self._update_title()

    def refresh(self) -> None:
        succeeded, rows = run_guarded(self, self.context.inventory.list_low_stock)
        if succeeded and rows is not None:
            self.model.set_rows(rows)
            self._update_title()

    def _update_title(self) -> None:
        self.setWindowTitle(f"低在庫 ({self.model.rowCount()})")

    def _on_double_clicked(self, index: QModelIndex) -> None:
        if index.column() == LowStockTableModel.URL_COLUMN:
            return
        self.item_activated.emit(self.model.row_at(index.row()).id)
