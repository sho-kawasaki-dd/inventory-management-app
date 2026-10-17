from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextEdit, QVBoxLayout, QWidget

from inventory_manager_mini.ui.dialog_placement import center_dialog


class RestoreConfirmationDialog(QDialog):
    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("復元の確認")
        layout = QVBoxLayout(self)
        self.message_edit = QTextEdit(self)
        self.message_edit.setReadOnly(True)
        self.message_edit.setPlainText(message)
        layout.addWidget(self.message_edit)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No, self
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Yes).setAutoDefault(False)
        self.button_box.button(QDialogButtonBox.StandardButton.No).setDefault(True)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)
        available = self.screen().availableGeometry()
        self.resize(min(900, available.width() - 24), min(600, available.height() - 48))
        self.button_box.button(QDialogButtonBox.StandardButton.No).setFocus()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)
