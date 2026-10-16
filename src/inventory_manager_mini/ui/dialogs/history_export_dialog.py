from __future__ import annotations

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import HistoryPeriod
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.widgets.history_period_selector import HistoryPeriodSelector


class HistoryExportDialog(QDialog):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("履歴(全品目)の出力条件")
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.client_combo = QComboBox(self)
        self.client_combo.addItem("すべて", None)
        for client in self.context.master.list_clients(include_inactive=True):
            label = client.name if client.is_active else f"{client.name} (無効)"
            self.client_combo.addItem(label, client.id)
        form.addRow("クライアント", self.client_combo)
        layout.addLayout(form)

        self.period_selector = HistoryPeriodSelector(self.context, self)
        layout.addWidget(self.period_selector)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)

    def selected_client_id(self) -> int | None:
        return self.client_combo.currentData()

    def selected_period(self) -> HistoryPeriod:
        return self.period_selector.period()
