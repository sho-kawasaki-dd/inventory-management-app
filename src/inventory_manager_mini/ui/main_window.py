from PySide6.QtWidgets import QMainWindow

from inventory_manager_mini.ui.context import AppContext


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.setWindowTitle("Inventory Manager mini")
        self.resize(1000, 650)
        self.setMinimumSize(800, 600)
