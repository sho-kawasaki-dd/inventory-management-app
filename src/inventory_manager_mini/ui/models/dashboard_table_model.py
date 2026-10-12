from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from inventory_manager_mini.core.models import DashboardRow

_INVALID_INDEX = QModelIndex()
_ModelIndex = QModelIndex | QPersistentModelIndex


class DashboardTableModel(QAbstractTableModel):
    _BREAKDOWN_COLUMNS = (
        ("年度/月", "period_label"),
        ("クライアント", "client_name"),
        ("発注主体", "purchaser_name"),
        ("入庫数", "inbound_quantity"),
        ("出庫数", "outbound_quantity"),
        ("支出額", "expenditure"),
        ("単価未登録数量", "unpriced_issue_quantity"),
    )
    _DISPOSAL_COLUMNS = (
        ("年度/月", "period_label"),
        ("クライアント", "client_name"),
        ("発注主体", "purchaser_name"),
        ("廃棄数", "disposed_quantity"),
        ("廃棄額", "disposal_amount"),
    )

    def __init__(self, disposal_only: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._disposal_only = disposal_only
        self._columns = self._DISPOSAL_COLUMNS if disposal_only else self._BREAKDOWN_COLUMNS
        self._numeric_columns = range(3, len(self._columns))
        self._rows: tuple[DashboardRow, ...] = ()

    def rowCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if (
            not index.isValid()
            or not 0 <= index.row() < len(self._rows)
            or not 0 <= index.column() < len(self._columns)
        ):
            return None

        value = getattr(self._rows[index.row()], self._columns[index.column()][1])
        if index.column() in (1, 2) and value is None:
            value = "(すべて)"
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(value, index.column())
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in self._numeric_columns:
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
            and 0 <= section < len(self._columns)
        ):
            return self._columns[section][0]
        return None

    def set_rows(self, rows: Sequence[DashboardRow]) -> None:
        self.beginResetModel()
        if self._disposal_only:
            self._rows = tuple(
                row for row in rows if row.disposed_quantity > 0 or row.disposal_amount > 0
            )
        else:
            self._rows = tuple(rows)
        self.endResetModel()

    def row_at(self, row: int) -> DashboardRow:
        return self._rows[row]

    def _display_value(self, value: Any, column: int) -> str:
        if column in self._numeric_columns:
            if column == len(self._columns) - 1 and self._disposal_only:
                return f"{value:,}円"
            if not self._disposal_only and column == 5:
                return f"{value:,}円"
            return f"{value:,}"
        return str(value)
