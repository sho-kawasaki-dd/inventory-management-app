from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import REASON_LABELS, MovementRow
from inventory_manager_mini.core.timeutil import format_local
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.error_handling import run_guarded


class ReversalDialog(QDialog):
    def __init__(
        self, context: AppContext, movement: MovementRow, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.movement = movement
        self._item_available = False
        self._staff_available = False
        self.setWindowTitle("在庫操作の取り消し")
        self._build_ui()
        self._load_item()
        self._load_staff()
        self._validate()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        form_widget = QWidget(self.scroll_area)
        self.form_layout = QFormLayout(form_widget)

        self.moved_at_label = QLabel(format_local(self.movement.moved_at), form_widget)
        self.form_layout.addRow("日時", self.moved_at_label)
        self.reason_label = QLabel(REASON_LABELS[self.movement.reason], form_widget)
        self.form_layout.addRow("種別", self.reason_label)
        self.quantity_label = QLabel(f"{self.movement.delta:+,} 個", form_widget)
        self.form_layout.addRow("数量", self.quantity_label)
        unit_price = (
            "未登録" if self.movement.unit_price is None else f"{self.movement.unit_price:,} 円"
        )
        self.unit_price_label = QLabel(unit_price, form_widget)
        self.form_layout.addRow("単価", self.unit_price_label)
        self.used_for_label = QLabel(self.movement.used_for or "", form_widget)
        self.used_for_label.setWordWrap(True)
        self.form_layout.addRow("使用先", self.used_for_label)
        self.resulting_quantity_label = QLabel("取り消し後数量: -", form_widget)
        self.form_layout.addRow("取り消し後数量", self.resulting_quantity_label)

        self.staff_combo = QComboBox(form_widget)
        self.staff_combo.addItem("(選択してください)", None)
        self.staff_combo.currentIndexChanged.connect(self._validate)
        self.form_layout.addRow("担当者", self.staff_combo)
        self.note_edit = QPlainTextEdit(form_widget)
        self.note_edit.setMaximumHeight(100)
        self.form_layout.addRow("メモ", self.note_edit)
        self.error_label = QLabel(form_widget)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318")
        self.form_layout.addRow(self.error_label)

        self.scroll_area.setWidget(form_widget)
        outer.addWidget(self.scroll_area)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.button_box.accepted.connect(self._save)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)

        available = self.screen().availableGeometry()
        self.resize(min(650, available.width() - 24), min(520, available.height() - 48))

    def _load_item(self) -> None:
        succeeded, item = run_guarded(
            self, lambda: self.context.inventory.get_item(self.movement.item_id)
        )
        if not succeeded or item is None:
            self.error_label.setText("品目情報を読み込めません")
            return
        self._item_available = True
        resulting_quantity = item.quantity - self.movement.delta
        self.resulting_quantity_label.setText(f"取り消し後数量: {resulting_quantity:,} 個")

    def _load_staff(self) -> None:
        succeeded, staff = run_guarded(self, self.context.master.list_staff)
        if not succeeded or staff is None:
            return
        self._staff_available = True
        for member in staff:
            self.staff_combo.addItem(member.name, member.id)

    def _validate(self, *_args) -> bool:
        messages: list[str] = []
        if self.staff_combo.currentData() is None:
            messages.append("担当者を選択してください")
        if self._staff_available and self.staff_combo.count() == 1:
            messages.append("先に担当者マスタを登録してください")
        if not self._item_available:
            messages.append("品目情報を読み込めません")
        self.error_label.setText("\n".join(dict.fromkeys(messages)))
        enabled = not messages and self._staff_available
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)
        return enabled

    def _save(self) -> None:
        if not self._validate():
            return
        staff_id = self.staff_combo.currentData()
        assert isinstance(staff_id, int)
        note = self.note_edit.toPlainText().strip() or None
        succeeded, movement = run_guarded(
            self,
            lambda: self.context.inventory.reverse(self.movement.id, staff_id, note),
        )
        if succeeded and movement is not None:
            self.context.data_bus.data_changed.emit()
            self.accept()
