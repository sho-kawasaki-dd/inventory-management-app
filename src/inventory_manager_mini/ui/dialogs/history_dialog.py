from __future__ import annotations

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import (
    DEFAULT_HISTORY_PAGE_SIZE,
    HISTORY_PAGE_SIZES,
    HistoryPage,
    MovementRow,
)
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.csv_export import export_csv_with_dialog
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from inventory_manager_mini.ui.error_handling import run_guarded
from inventory_manager_mini.ui.models.movement_table_model import MovementTableModel
from inventory_manager_mini.ui.widgets.history_period_selector import HistoryPeriodSelector


class HistoryDialog(QDialog):
    def __init__(self, context: AppContext, item_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.item_id = item_id
        self._item_available = False
        self._page = 1
        self.setWindowTitle("在庫履歴")
        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.item_info_label = QLabel(self)
        self.item_info_label.setWordWrap(True)
        self.item_info_label.setMaximumHeight(72)
        layout.addWidget(self.item_info_label)

        self.period_selector = HistoryPeriodSelector(self.context, self)
        layout.addWidget(self.period_selector)

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

        pagination = QHBoxLayout()
        pagination.addWidget(QLabel("表示件数", self))
        self.page_size_combo = QComboBox(self)
        for size in HISTORY_PAGE_SIZES:
            self.page_size_combo.addItem(f"{size}件", size)
        self.page_size_combo.setCurrentIndex(
            max(self.page_size_combo.findData(DEFAULT_HISTORY_PAGE_SIZE), 0)
        )
        pagination.addWidget(self.page_size_combo)
        pagination.addStretch(1)
        self.prev_page_button = QPushButton("前へ", self)
        pagination.addWidget(self.prev_page_button)
        self.page_label = QLabel("0 / 0 ページ (全 0 件)", self)
        pagination.addWidget(self.page_label)
        self.next_page_button = QPushButton("次へ", self)
        pagination.addWidget(self.next_page_button)
        layout.addLayout(pagination)

        self.period_selector.periodChanged.connect(self._reset_to_first_page)
        self.page_size_combo.currentIndexChanged.connect(self._reset_to_first_page)
        self.prev_page_button.clicked.connect(lambda: self._move_page(-1))
        self.next_page_button.clicked.connect(lambda: self._move_page(1))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.export_button = QPushButton("CSV 出力", self)
        self.export_button.clicked.connect(self._export_csv)
        buttons.addWidget(self.export_button)
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

    def _reset_to_first_page(self, *_args) -> None:
        self._page = 1
        self._refresh()

    def _move_page(self, step: int) -> None:
        self._page += step
        self._refresh()

    def _show_empty(self) -> None:
        self._item_available = False
        self.model.set_rows([])
        self._update_page_controls(None)
        self._update_reversal_button()

    def _update_page_controls(self, page: HistoryPage | None) -> None:
        if page is None or page.total_count == 0:
            self.page_label.setText("0 / 0 ページ (全 0 件)")
        else:
            self.page_label.setText(
                f"{page.page} / {page.total_pages} ページ (全 {page.total_count:,} 件)"
            )
        has_page = self._item_available and page is not None
        self.prev_page_button.setEnabled(has_page and page is not None and page.page > 1)
        self.next_page_button.setEnabled(
            has_page and page is not None and page.page < page.total_pages
        )
        self.export_button.setEnabled(self._item_available)

    def _refresh(self, select_movement_id: int | None = None) -> None:
        succeeded, item = run_guarded(self, lambda: self.context.inventory.get_item(self.item_id))
        if not succeeded:
            self.item_info_label.setText("品目情報を読み込めません")
            self._show_empty()
            return
        if item is None:
            self.item_info_label.setText("品目が見つかりません")
            self._show_empty()
            return
        self._item_available = True
        state = "有効" if item.is_active else "廃止"
        info = f"{item.code} / {item.name} / 現在数量 {item.quantity:,} 個 / {state}"
        self.item_info_label.setText(info)
        self.item_info_label.setToolTip(info)

        page_size = self.page_size_combo.currentData()
        succeeded, result = run_guarded(
            self,
            lambda: self.context.reports.list_history_page(
                self.item_id,
                period=self.period_selector.period(),
                page=self._page,
                page_size=page_size,
                select_movement_id=select_movement_id,
            ),
        )
        if not succeeded or result is None:
            self._show_empty()
            return
        self._page = result.page
        self.model.set_rows(list(result.rows))
        self._update_page_controls(result)
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

    def _export_csv(self) -> None:
        path = export_csv_with_dialog(
            self,
            "履歴",
            "履歴",
            lambda destination: self.context.reports.export_history_csv(
                destination, self.item_id, period=self.period_selector.period()
            ),
        )
        if path is not None:
            QMessageBox.information(self, "CSV 出力", f"履歴を出力しました:\n{path}")

    def _open_reversal(self) -> None:
        movement = self._selected_movement()
        if movement is None or not self.reverse_button.isEnabled():
            return
        dialog = ReversalDialog(self.context, movement, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh(select_movement_id=movement.id)
