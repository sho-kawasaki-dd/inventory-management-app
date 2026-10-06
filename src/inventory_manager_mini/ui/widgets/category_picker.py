from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import Category

_ID_ROLE = Qt.ItemDataRole.UserRole


def _children_by_parent(categories: list[Category]) -> dict[int | None, list[Category]]:
    children: dict[int | None, list[Category]] = defaultdict(list)
    for category in categories:
        children[category.parent_id].append(category)
    for siblings in children.values():
        siblings.sort(key=lambda category: category.name)
    return children


def _category_paths(categories: list[Category]) -> dict[int, str]:
    children = _children_by_parent(categories)
    paths: dict[int, str] = {}

    def walk(parent_id: int | None, prefix: str) -> None:
        for category in children[parent_id]:
            path = f"{prefix} > {category.name}" if prefix else category.name
            paths[category.id] = path
            walk(category.id, path)

    walk(None, "")
    return paths


class CategoryPickerDialog(QDialog):
    """カテゴリ木から 1 件を選ぶモーダルダイアログ。"""

    def __init__(
        self,
        categories: list[Category],
        leading_label: str | None,
        current_id: int | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("カテゴリの選択")
        self.resize(360, 440)
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.tree)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._items: dict[int | None, QTreeWidgetItem] = {}
        if leading_label is not None:
            leading = QTreeWidgetItem(self.tree, [leading_label])
            leading.setData(0, _ID_ROLE, None)
            self._items[None] = leading
        children = _children_by_parent(categories)

        def append(parent_item: QTreeWidgetItem | None, parent_id: int | None) -> None:
            for category in children[parent_id]:
                item = (
                    QTreeWidgetItem(self.tree, [category.name])
                    if parent_item is None
                    else QTreeWidgetItem(parent_item, [category.name])
                )
                item.setData(0, _ID_ROLE, category.id)
                self._items[category.id] = item
                append(item, category.id)

        append(None, None)
        self.tree.expandAll()

        initial = self._items.get(current_id)
        if initial is not None:
            self.tree.setCurrentItem(initial)
            self.tree.scrollToItem(initial)
        self.tree.itemSelectionChanged.connect(self._update_ok_state)
        self.tree.itemDoubleClicked.connect(lambda *_args: self._accept_selected())
        self._update_ok_state()

    def selected_id(self) -> int | None:
        item = self.tree.currentItem()
        return None if item is None else item.data(0, _ID_ROLE)

    def _update_ok_state(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setEnabled(self.tree.currentItem() is not None)

    def _accept_selected(self) -> None:
        if self.tree.currentItem() is not None:
            self.accept()


class CategoryPicker(QWidget):
    category_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.display = QLineEdit(self)
        self.display.setReadOnly(True)
        self.display.setPlaceholderText("(未選択)")
        self.button = QPushButton("選択…", self)
        self.button.clicked.connect(self.open_dialog)
        layout.addWidget(self.display, 1)
        layout.addWidget(self.button)
        self.setFocusProxy(self.button)
        self._categories: list[Category] = []
        self._paths: dict[int, str] = {}
        self._leading_label: str | None = None
        self._current_id: int | None = None

    def set_categories(self, categories: list[Category], leading_label: str | None = None) -> None:
        self._categories = list(categories)
        self._paths = _category_paths(categories)
        self._leading_label = leading_label
        self.set_current_category_id(self._current_id)

    def category_count(self) -> int:
        return len(self._categories)

    def current_category_id(self) -> int | None:
        return self._current_id

    def set_current_category_id(self, category_id: int | None) -> None:
        self._current_id = category_id if category_id in self._paths else None
        if self._current_id is not None:
            text = self._paths[self._current_id]
        else:
            text = self._leading_label or ""
        self.display.setText(text)
        self.display.setToolTip(text)

    def open_dialog(self) -> None:
        dialog = CategoryPickerDialog(self._categories, self._leading_label, self._current_id, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        selected = dialog.selected_id()
        if selected == self._current_id:
            return
        self.set_current_category_id(selected)
        self.category_changed.emit(self._current_id)
