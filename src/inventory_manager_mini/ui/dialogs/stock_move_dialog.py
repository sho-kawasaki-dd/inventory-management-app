from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout

from inventory_manager_mini.core.models import REASON_LABELS, Reason
from inventory_manager_mini.ui.context import AppContext


class StockMoveDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        reason: Reason,
        item_id: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.item_id = item_id
        self.setWindowTitle("在庫操作")

        layout = QFormLayout(self)
        self.reason_combo = QComboBox(self)
        for value, label in REASON_LABELS.items():
            self.reason_combo.addItem(label, value)
        self.reason_combo.setCurrentIndex(self.reason_combo.findData(reason))
        layout.addRow("操作種別", self.reason_combo)
