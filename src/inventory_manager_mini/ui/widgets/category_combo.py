from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import QEvent, QModelIndex, QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QComboBox, QStyle, QStyleOptionViewItem, QTreeView

from inventory_manager_mini.core.models import Category


class CategoryComboBox(QComboBox):
    category_changed = Signal(object)

    _ID_ROLE = Qt.ItemDataRole.UserRole
    _PATH_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self._view = QTreeView(self)
        self._view.setHeaderHidden(True)
        self._view.setUniformRowHeights(True)
        self._view.setExpandsOnDoubleClick(False)
        self.setModel(self._model)
        self.setView(self._view)
        self._view.viewport().installEventFilter(self)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        line_edit = self.lineEdit()
        assert line_edit is not None
        line_edit.setReadOnly(True)
        self._view.clicked.connect(self._select_index)
        self._items_by_id: dict[int, QStandardItem] = {}
        self._paths_by_id: dict[int, str] = {}
        self._current_category_id: int | None = None
        self._leading_label: str | None = None
        self._branch_pressed = QModelIndex()

    def set_categories(self, categories: list[Category], leading_label: str | None = None) -> None:
        selected_id = self._current_category_id
        self._model.clear()
        self._items_by_id.clear()
        self._paths_by_id.clear()
        self._leading_label = leading_label

        if leading_label is not None:
            leading_item = QStandardItem(leading_label)
            leading_item.setEditable(False)
            leading_item.setData(None, self._ID_ROLE)
            leading_item.setData(leading_label, self._PATH_ROLE)
            self._model.appendRow(leading_item)

        children: dict[int | None, list[Category]] = defaultdict(list)
        for category in categories:
            children[category.parent_id].append(category)
        for siblings in children.values():
            siblings.sort(key=lambda category: category.name)

        def append_children(
            parent_item: QStandardItem | None, parent_id: int | None, prefix: str
        ) -> None:
            for category in children[parent_id]:
                path = f"{prefix} > {category.name}" if prefix else category.name
                item = QStandardItem(category.name)
                item.setEditable(False)
                item.setData(category.id, self._ID_ROLE)
                item.setData(path, self._PATH_ROLE)
                item.setToolTip(path)
                if parent_item is None:
                    self._model.appendRow(item)
                else:
                    parent_item.appendRow(item)
                self._items_by_id[category.id] = item
                self._paths_by_id[category.id] = path
                append_children(item, category.id, path)

        append_children(None, None, "")
        self.set_current_category_id(selected_id)

    def current_category_id(self) -> int | None:
        return self._current_category_id

    def set_current_category_id(self, category_id: int | None) -> None:
        if category_id is None:
            self._current_category_id = None
            if self._leading_label is not None:
                self.setRootModelIndex(QModelIndex())
                self.setCurrentIndex(0)
                self._set_display_text(self._leading_label)
                self.setToolTip(self._leading_label)
            else:
                self.setCurrentIndex(-1)
                self._set_display_text("")
                self.setToolTip("")
            return

        item = self._items_by_id.get(category_id)
        if item is None:
            self._current_category_id = None
            self.setCurrentIndex(-1)
            self._set_display_text("")
            self.setToolTip("")
            return

        index = item.index()
        parent = item.parent()
        root_index = QModelIndex() if parent is None else parent.index()
        self._current_category_id = category_id
        self._view.expand(index.parent())
        parent = item.parent()
        while parent is not None:
            self._view.expand(parent.index())
            parent = parent.parent()
        self._view.selectionModel().setCurrentIndex(
            index,
            self._view.selectionModel().SelectionFlag.ClearAndSelect,
        )
        self.setRootModelIndex(root_index)
        self.setCurrentIndex(index.row())
        self.setRootModelIndex(QModelIndex())
        path = self._paths_by_id[category_id]
        self._set_display_text(path)
        self.setToolTip(path)

    def eventFilter(self, watched, event) -> bool:
        if watched == self._view.viewport() and isinstance(event, QMouseEvent):
            if event.type() == QEvent.Type.MouseButtonPress:
                position = event.position().toPoint()
                index = self._view.indexAt(position)
                if self._is_branch_click(index, position):
                    self._branch_pressed = index
                    return True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                position = event.position().toPoint()
                index = self._view.indexAt(position)
                branch_pressed = self._branch_pressed
                self._branch_pressed = QModelIndex()
                if index == branch_pressed and self._is_branch_click(index, position):
                    self._view.setExpanded(index, not self._view.isExpanded(index))
                    return True
        return super().eventFilter(watched, event)

    def _is_branch_click(self, index: QModelIndex, position: QPoint) -> bool:
        if not index.isValid() or not self._model.hasChildren(index):
            return False
        option = QStyleOptionViewItem()
        self._view.initViewItemOption(option)
        option.rect = self._view.visualRect(index)
        option.index = index
        branch_rect = self._view.style().subElementRect(
            QStyle.SubElement.SE_TreeViewDisclosureItem, option, self._view
        )
        return branch_rect.contains(position)

    def _set_display_text(self, text: str) -> None:
        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setText(text)

    def _select_index(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        category_id = index.data(self._ID_ROLE)
        path = index.data(self._PATH_ROLE)
        self._current_category_id = category_id
        parent_index = index.parent()
        self.setRootModelIndex(parent_index)
        self.setCurrentIndex(index.row())
        self.setRootModelIndex(QModelIndex())
        self._set_display_text(path or "")
        self.setToolTip(path or "")
        self.category_changed.emit(category_id)
        self.hidePopup()
