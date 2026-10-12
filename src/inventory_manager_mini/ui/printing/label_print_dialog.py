from __future__ import annotations

from math import ceil

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import ItemRow
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.printing.layout import LABEL_72212, LabelEntry


class LabelPrintDialog(QDialog):
    def __init__(self, items: list[ItemRow], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items = list(items)
        self.setWindowTitle("棚ラベル印刷")

        outer_layout = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(self.scroll_area)
        content_layout = QVBoxLayout(content)

        self.item_table = QTableWidget(len(self._items), 3, content)
        self.item_table.setHorizontalHeaderLabels(["管理番号", "品名", "枚数"])
        self.item_table.verticalHeader().hide()
        self.item_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.item_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.item_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.item_table.setColumnWidth(2, 90)
        self.item_table.setMaximumHeight(280)
        self.quantity_spins: list[QSpinBox] = []
        for row_index, item in enumerate(self._items):
            for column, text in enumerate((item.code, item.name)):
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.item_table.setItem(row_index, column, cell)
            quantity_spin = QSpinBox(self.item_table)
            quantity_spin.setRange(1, 99)
            quantity_spin.setValue(1)
            quantity_spin.valueChanged.connect(self._update_summary)
            self.quantity_spins.append(quantity_spin)
            self.item_table.setCellWidget(row_index, 2, quantity_spin)
        content_layout.addWidget(self.item_table)

        slots_heading = QLabel("1 枚目の印字位置", content)
        content_layout.addWidget(slots_heading)
        slots_layout = QGridLayout()
        self.slot_checkboxes: list[QCheckBox] = []
        for slot in range(LABEL_72212.columns * LABEL_72212.rows):
            row, column = divmod(slot, LABEL_72212.columns)
            checkbox = QCheckBox(f"{row + 1}段 {column + 1}列", content)
            checkbox.setChecked(True)
            checkbox.stateChanged.connect(self._update_summary)
            self.slot_checkboxes.append(checkbox)
            slots_layout.addWidget(checkbox, row, column)
        content_layout.addLayout(slots_layout)

        self.select_all_button_widget = QPushButton("すべて選択", content)
        self.clear_all_button = QPushButton("すべて解除", content)
        self.select_all_button_widget.clicked.connect(lambda: self._set_all_slots(True))
        self.clear_all_button.clicked.connect(lambda: self._set_all_slots(False))
        selection_layout = QGridLayout()
        selection_layout.addWidget(self.select_all_button_widget, 0, 0)
        selection_layout.addWidget(self.clear_all_button, 0, 1)
        content_layout.addLayout(selection_layout)

        self.count_label = QLabel(content)
        content_layout.addWidget(self.count_label)
        self.error_label = QLabel(content)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318")
        content_layout.addWidget(self.error_label)
        content_layout.addStretch(1)

        self.scroll_area.setWidget(content)
        outer_layout.addWidget(self.scroll_area)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        outer_layout.addWidget(self.button_box)

        available = self.screen().availableGeometry()
        width = max(320, min(860, available.width() - 24))
        height = max(320, min(640, available.height() - 48))
        self.resize(width, height)
        self._update_summary()

    @property
    def entries(self) -> list[LabelEntry]:
        return [
            LabelEntry(item_id=item.id, copies=self.quantity_spins[index].value())
            for index, item in enumerate(self._items)
        ]

    @property
    def first_page_slots(self) -> list[int]:
        return [slot for slot, checkbox in enumerate(self.slot_checkboxes) if checkbox.isChecked()]

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)

    def _set_all_slots(self, checked: bool) -> None:
        for checkbox in self.slot_checkboxes:
            checkbox.setChecked(checked)

    def _update_summary(self, *_args) -> None:
        total_copies = sum(spin.value() for spin in self.quantity_spins)
        slot_count = len(self.first_page_slots)
        if total_copies == 0:
            page_count = 0
        else:
            sheet_capacity = LABEL_72212.columns * LABEL_72212.rows
            page_count = 1 + ceil(max(0, total_copies - slot_count) / sheet_capacity)
        self.count_label.setText(f"合計 {total_copies} 枚 / 用紙 {page_count} 枚")
        has_slot = slot_count > 0
        self.error_label.setText("" if has_slot else "印字位置を 1 つ以上選択してください")
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(has_slot)
