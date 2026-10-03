from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QBrush, QColor

from inventory_manager_mini.core.models import ItemRow
from inventory_manager_mini.core.timeutil import local_date

_INVALID_INDEX = QModelIndex()
_ModelIndex = QModelIndex | QPersistentModelIndex


class ItemTableModel(QAbstractTableModel):
    _COLUMNS = (
        ("管理番号", "code"),
        ("品名", "name"),
        ("メーカー型番", "manufacturer_part_number"),
        ("クライアント", "client_name"),
        ("発注主体", "purchaser_name"),
        ("カテゴリ", "category_path"),
        ("保管場所", "location_name"),
        ("数量", "quantity"),
        ("単位", "unit"),
        ("閾値", "reorder_threshold"),
        ("推奨発注数", "reorder_quantity"),
        ("参考価格", "reference_price"),
        ("仕入先", "supplier"),
        ("最終購入日", "last_purchased_at"),
        ("状態", "is_active"),
    )
    _NUMERIC_COLUMNS = {7, 9, 10, 11}
    _INACTIVE_FOREGROUND = QBrush(QColor(Qt.GlobalColor.gray))

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

        row = self._rows[index.row()]
        value = self._raw_value(row, index.column())
        if role == Qt.ItemDataRole.UserRole:
            return value
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_value(value, index.column())
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in self._NUMERIC_COLUMNS:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.ForegroundRole and not row.is_active:
            return self._INACTIVE_FOREGROUND
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

    def row_of(self, item_id: int) -> int | None:
        return next((index for index, row in enumerate(self._rows) if row.id == item_id), None)

    @staticmethod
    def _raw_value(row: ItemRow, column: int) -> Any:
        attribute = ItemTableModel._COLUMNS[column][1]
        if attribute == "last_purchased_at":
            timestamp = row.purchase_info.last_purchased_at
            return None if timestamp is None else local_date(timestamp)
        return getattr(row, attribute)

    @staticmethod
    def _display_value(value: Any, column: int) -> str:
        if value is None:
            return ""
        if column == 11:
            return f"{value:,}円"
        if column == 13:
            return value.isoformat() if isinstance(value, date) else ""
        if column == 14:
            return "有効" if value else "廃止"
        if column in ItemTableModel._NUMERIC_COLUMNS:
            return f"{value:,}"
        return str(value)


class ItemSortProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def lessThan(self, left: _ModelIndex, right: _ModelIndex) -> bool:
        left_value = left.data(self.sortRole())
        right_value = right.data(self.sortRole())
        if left_value is None:
            return False
        if right_value is None:
            return True
        return left_value < right_value
