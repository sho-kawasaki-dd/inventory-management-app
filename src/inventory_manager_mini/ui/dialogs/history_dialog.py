from __future__ import annotations

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

from inventory_manager_mini.core.models import MovementRow
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from inventory_manager_mini.ui.error_handling import run_guarded
from inventory_manager_mini.ui.models.movement_table_model import MovementTableModel


class HistoryDialog(QDialog):
    def __init__(self, context: AppContext, item_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.item_id = item_id
        self._item_available = False
        self.setWindowTitle("在庫履歴")
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.item_info_label = QLabel(self)
        self.item_info_label.setWordWrap(True)
        self.item_info_label.setMaximumHeight(72)
        layout.addWidget(self.item_info_label)

        self.table = QTableView(self)
        self.model = MovementTableModel(self.table)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        self.table.selectionModel().currentRowChanged.connect(self._update_reversal_button)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.reverse_button = QPushButton("取り消し…", self)
        self.reverse_button.setEnabled(False)
        self.reverse_button.clicked.connect(self._open_reversal)
        buttons.addWidget(self.reverse_button)
        self.close_button = QPushButton("閉じる", self)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        available = self.screen().availableGeometry()
        self.resize(min(1050, available.width() - 24), min(620, available.height() - 48))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)

    def _refresh(self, select_movement_id: int | None = None) -> None:
        succeeded, item = run_guarded(self, lambda: self.context.inventory.get_item(self.item_id))
        if not succeeded:
            self._item_available = False
            self.item_info_label.setText("品目情報を読み込めません")
            self.model.set_rows([])
            self._update_reversal_button()
            return
        if item is None:
            self._item_available = False
            self.item_info_label.setText("品目が見つかりません")
        else:
            self._item_available = True
            state = "有効" if item.is_active else "廃止"
            info = f"{item.code} / {item.name} / 現在数量 {item.quantity:,} 個 / {state}"
            self.item_info_label.setText(info)
            self.item_info_label.setToolTip(info)

        succeeded, rows = run_guarded(
            self, lambda: self.context.inventory.list_history(self.item_id)
        )
        if not succeeded or rows is None:
            self.model.set_rows([])
            self._update_reversal_button()
            return
        self.model.set_rows(rows)
        if select_movement_id is not None:
            row_index = self.model.row_of(select_movement_id)
            if row_index is not None:
                self.table.selectRow(row_index)
                self.table.scrollTo(self.model.index(row_index, 0))
        self._update_reversal_button()

    def _selected_movement(self) -> MovementRow | None:
        current = self.table.currentIndex()
        if not current.isValid() or not self._item_available:
            return None
        return self.model.row_at(current.row())

    def _update_reversal_button(self, *_args) -> None:
        movement = self._selected_movement()
        if movement is None:
            self.reverse_button.setEnabled(False)
            self.reverse_button.setToolTip("")
            return
        succeeded, reason = run_guarded(
            self, lambda: self.context.inventory.reversal_block_reason(movement.id)
        )
        if not succeeded:
            self.reverse_button.setEnabled(False)
            self.reverse_button.setToolTip("取り消し可否を確認できません")
            return
        self.reverse_button.setEnabled(reason is None)
        self.reverse_button.setToolTip(reason or "")

    def _open_reversal(self) -> None:
        movement = self._selected_movement()
        if movement is None or not self.reverse_button.isEnabled():
            return
        dialog = ReversalDialog(self.context, movement, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh(select_movement_id=movement.id)
