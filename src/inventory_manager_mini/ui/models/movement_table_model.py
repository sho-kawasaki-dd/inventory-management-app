from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

from inventory_manager_mini.core.models import REASON_LABELS, MovementRow
from inventory_manager_mini.core.timeutil import format_local

_INVALID_INDEX = QModelIndex()
_ModelIndex = QModelIndex | QPersistentModelIndex


class MovementTableModel(QAbstractTableModel):
    _COLUMNS = (
        ("履歴 ID", "id"),
        ("日時", "moved_at"),
        ("種別", "reason"),
        ("数量", "delta"),
        ("単価", "unit_price"),
        ("クライアント", "client_name"),
        ("発注主体", "purchaser_name"),
        ("担当者", "staff_name"),
        ("使用先", "used_for"),
        ("メモ", "note"),
        ("取り消し状態", "reversal_status"),
    )
    _NUMERIC_COLUMNS = {0, 3, 4}
    _GRAY_FOREGROUND = QBrush(QColor(Qt.GlobalColor.gray))

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[MovementRow] = []

    def rowCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._COLUMNS)

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        if not 0 <= index.column() < len(self._COLUMNS):
            return None

        row = self._rows[index.row()]
        value = self._raw_value(row, index.column())
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(value, index.column())
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in self._NUMERIC_COLUMNS:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ForegroundRole and (
            row.reversal_of is not None or row.is_reversed
        ):
            return self._GRAY_FOREGROUND
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

    def set_rows(self, rows: list[MovementRow]) -> None:
        self.beginResetModel()
        self._rows = sorted(rows, key=lambda row: (row.moved_at, row.id), reverse=True)
        self.endResetModel()

    def row_at(self, row: int) -> MovementRow:
        return self._rows[row]

    def row_of(self, movement_id: int) -> int | None:
        return next((index for index, row in enumerate(self._rows) if row.id == movement_id), None)

    @staticmethod
    def _raw_value(row: MovementRow, column: int) -> Any:
        attribute = MovementTableModel._COLUMNS[column][1]
        if attribute == "moved_at":
            return format_local(row.moved_at)
        if attribute == "reason":
            return REASON_LABELS[row.reason]
        if attribute == "reversal_status":
            if row.reversal_of is not None:
                return f"#{row.reversal_of} の取り消し"
            return "取り消し済み" if row.is_reversed else ""
        return getattr(row, attribute)

    @staticmethod
    def _display_value(value: Any, column: int) -> str:
        if value is None:
            return ""
        if column == 3:
            return f"{value:+,}"
        if column == 4:
            return f"{value:,}円"
        return str(value)
