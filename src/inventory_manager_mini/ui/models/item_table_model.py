from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractProxyModel,
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
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


class ItemSortProxyModel(QAbstractProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source_rows: list[int] = []
        self._proxy_rows: list[int] = []
        self._sort_column = -1
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._selection_model: QItemSelectionModel | None = None

    def set_item_selection_model(self, selection_model: QItemSelectionModel) -> None:
        self._selection_model = selection_model

    def setSourceModel(self, source_model: QAbstractItemModel) -> None:
        previous_model = self.sourceModel()
        if previous_model is not None:
            previous_model.modelAboutToBeReset.disconnect(self._source_about_to_reset)
            previous_model.modelReset.disconnect(self._source_reset)
        super().setSourceModel(source_model)
        if source_model is not None:
            source_model.modelAboutToBeReset.connect(self._source_about_to_reset)
            source_model.modelReset.connect(self._source_reset)
        self.beginResetModel()
        self._rebuild_mapping()
        self.endResetModel()

    def rowCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._source_rows)

    def columnCount(self, parent: _ModelIndex = _INVALID_INDEX) -> int:
        source_model = self.sourceModel()
        return 0 if parent.isValid() or source_model is None else source_model.columnCount()

    def index(self, row: int, column: int, parent: _ModelIndex = _INVALID_INDEX) -> QModelIndex:
        if (
            parent.isValid()
            or not (0 <= row < self.rowCount())
            or not (0 <= column < self.columnCount())
        ):
            return QModelIndex()
        return self.createIndex(row, column)

    def parent(self, _index: _ModelIndex = _INVALID_INDEX) -> _ModelIndex:  # type: ignore[override]
        del _index
        return QModelIndex()

    def mapToSource(self, proxy_index: _ModelIndex) -> QModelIndex:
        source_model = self.sourceModel()
        if (
            source_model is None
            or not proxy_index.isValid()
            or not 0 <= proxy_index.row() < len(self._source_rows)
        ):
            return QModelIndex()
        return source_model.index(self._source_rows[proxy_index.row()], proxy_index.column())

    def mapFromSource(self, source_index: _ModelIndex) -> QModelIndex:
        source_model = self.sourceModel()
        if (
            source_model is None
            or not source_index.isValid()
            or source_index.model() is not source_model
            or not 0 <= source_index.row() < len(self._proxy_rows)
        ):
            return QModelIndex()
        return self.index(self._proxy_rows[source_index.row()], source_index.column())

    def data(self, index: _ModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        source_index = self.mapToSource(index)
        if not source_index.isValid():
            return None
        source_model = self.sourceModel()
        return None if source_model is None else source_model.data(source_index, role)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        source_model = self.sourceModel()
        return None if source_model is None else source_model.headerData(section, orientation, role)

    def flags(self, index: _ModelIndex) -> Qt.ItemFlag:
        source_index = self.mapToSource(index)
        source_model = self.sourceModel()
        return (
            Qt.ItemFlag.NoItemFlags
            if source_model is None or not source_index.isValid()
            else source_model.flags(source_index)
        )

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        selected_source_indexes = (
            []
            if self._selection_model is None
            else [self.mapToSource(index) for index in self._selection_model.selectedRows()]
        )
        persistent_indexes = self.persistentIndexList()
        source_indexes = [self.mapToSource(index) for index in persistent_indexes]
        self.layoutAboutToBeChanged.emit()
        self._sort_column = column
        self._sort_order = order
        self._rebuild_mapping()
        self.changePersistentIndexList(
            persistent_indexes,
            [self.mapFromSource(index) for index in source_indexes],
        )
        self.layoutChanged.emit()
        if self._selection_model is not None:
            self._selection_model.clearSelection()
            for source_index in selected_source_indexes:
                proxy_index = self.mapFromSource(source_index)
                if proxy_index.isValid():
                    self._selection_model.select(
                        proxy_index,
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
                    self._selection_model.setCurrentIndex(
                        proxy_index, QItemSelectionModel.SelectionFlag.NoUpdate
                    )

    def sortColumn(self) -> int:
        return self._sort_column

    def sortOrder(self) -> Qt.SortOrder:
        return self._sort_order

    def _source_about_to_reset(self) -> None:
        self.beginResetModel()

    def _source_reset(self) -> None:
        self._rebuild_mapping()
        self.endResetModel()

    def _rebuild_mapping(self) -> None:
        source_model = self.sourceModel()
        row_count = 0 if source_model is None else source_model.rowCount()
        source_rows = list(range(row_count))
        if (
            source_model is not None
            and isinstance(source_model, ItemTableModel)
            and 0 <= self._sort_column < source_model.columnCount()
        ):
            column = self._sort_column
            values = [ItemTableModel._raw_value(row, column) for row in source_model._rows]
            source_rows.sort(
                key=lambda row: (values[row] is None, values[row]),
                reverse=self._sort_order == Qt.SortOrder.DescendingOrder,
            )
        self._source_rows = source_rows
        self._proxy_rows = [0] * row_count
        for proxy_row, source_row in enumerate(source_rows):
            self._proxy_rows[source_row] = proxy_row
