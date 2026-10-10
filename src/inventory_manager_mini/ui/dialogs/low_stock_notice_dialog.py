from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import ItemRow
from inventory_manager_mini.ui.dialog_placement import center_dialog

_INVALID_INDEX = QModelIndex()
_ModelIndex = QModelIndex | QPersistentModelIndex


class LowStockNoticeTableModel(QAbstractTableModel):
    _COLUMNS = (
        ("管理番号", "code"),
        ("品名", "name"),
        ("数量", "quantity"),
        ("閾値", "reorder_threshold"),
        ("発注主体", "purchaser_name"),
    )
    _NUMERIC_COLUMNS = {2, 3}

    def __init__(self, parent: QWidget | None = None) -> None:
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
        if role == Qt.ItemDataRole.DisplayRole:
            value = getattr(self._rows[index.row()], self._COLUMNS[index.column()][1])
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


class LowStockNoticeDialog(QDialog):
    def __init__(self, rows: list[ItemRow], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("低在庫のお知らせ")
        layout = QVBoxLayout(self)
        self.message_label = QLabel(f"在庫が閾値以下の品目が {len(rows):,} 件あります", self)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.table = QTableView(self)
        self.model = LowStockNoticeTableModel(self.table)
        self.model.set_rows(rows)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        for column, width in enumerate((90, 260, 70, 70, 120)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton("閉じる", self)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        available = self.screen().availableGeometry()
        self.resize(min(700, available.width() - 24), min(420, available.height() - 48))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)
