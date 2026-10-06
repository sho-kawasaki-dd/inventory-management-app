from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QRegularExpression, Qt
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import Category
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.error_handling import run_guarded
from inventory_manager_mini.ui.widgets.category_picker import CategoryPickerDialog


class MasterTab(StrEnum):
    CLIENT = "client"
    PURCHASER = "purchaser"
    STAFF = "staff"
    CATEGORY = "category"
    LOCATION = "location"


_TAB_LABELS = {
    MasterTab.CLIENT: "クライアント",
    MasterTab.PURCHASER: "発注主体",
    MasterTab.STAFF: "担当者",
    MasterTab.CATEGORY: "カテゴリ",
    MasterTab.LOCATION: "保管場所",
}
_NAME_ROLE = Qt.ItemDataRole.UserRole + 1
_ACTIVE_ROLE = Qt.ItemDataRole.UserRole + 2
_INACTIVE_TOOLTIP = "使用中のため削除できません。無効化してください"
_MasterRecord = tuple[int, str, bool]


@dataclass(frozen=True, slots=True)
class _SimpleMasterOperations:
    list_records: Callable[[], list[_MasterRecord]]
    add: Callable[[str], object]
    rename: Callable[[int, str], object]
    deactivate: Callable[[int], object] | None
    reactivate: Callable[[int], object] | None
    can_delete: Callable[[int], bool]
    delete: Callable[[int], None]


def _simple_master_operations(context: AppContext, tab: MasterTab) -> _SimpleMasterOperations:
    master = context.master
    if tab is MasterTab.CLIENT:
        return _SimpleMasterOperations(
            lambda: [
                (entry.id, entry.name, entry.is_active)
                for entry in master.list_clients(include_inactive=True)
            ],
            master.add_client,
            master.rename_client,
            master.deactivate_client,
            master.reactivate_client,
            master.can_delete_client,
            master.delete_client,
        )
    if tab is MasterTab.PURCHASER:
        return _SimpleMasterOperations(
            lambda: [
                (entry.id, entry.name, entry.is_active)
                for entry in master.list_purchasers(include_inactive=True)
            ],
            master.add_purchaser,
            master.rename_purchaser,
            master.deactivate_purchaser,
            master.reactivate_purchaser,
            master.can_delete_purchaser,
            master.delete_purchaser,
        )
    if tab is MasterTab.STAFF:
        return _SimpleMasterOperations(
            lambda: [
                (entry.id, entry.name, entry.is_active)
                for entry in master.list_staff(include_inactive=True)
            ],
            master.add_staff,
            master.rename_staff,
            master.deactivate_staff,
            master.reactivate_staff,
            master.can_delete_staff,
            master.delete_staff,
        )
    return _SimpleMasterOperations(
        lambda: [(entry.id, entry.name, True) for entry in master.list_locations()],
        master.add_location,
        master.rename_location,
        None,
        None,
        master.can_delete_location,
        master.delete_location,
    )


class MasterDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        initial_tab: MasterTab,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.setWindowTitle("マスタ管理")
        self.resize(760, 520)
        self.tabs = QTabWidget(self)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)

        self.simple_tabs: dict[MasterTab, SimpleMasterTab] = {}
        for tab in (MasterTab.CLIENT, MasterTab.PURCHASER, MasterTab.STAFF):
            widget = SimpleMasterTab(context, tab, _simple_master_operations(context, tab), self)
            self.simple_tabs[tab] = widget
            self.tabs.addTab(widget, _TAB_LABELS[tab])
        self.category_tab = _CategoryTab(context, self)
        self.tabs.addTab(self.category_tab, _TAB_LABELS[MasterTab.CATEGORY])
        self.location_tab = SimpleMasterTab(
            context,
            MasterTab.LOCATION,
            _simple_master_operations(context, MasterTab.LOCATION),
            self,
        )
        self.simple_tabs[MasterTab.LOCATION] = self.location_tab
        self.tabs.addTab(self.location_tab, _TAB_LABELS[MasterTab.LOCATION])
        self.tabs.setCurrentIndex(list(MasterTab).index(initial_tab))


class SimpleMasterTab(QWidget):
    def __init__(
        self,
        context: AppContext,
        tab: MasterTab,
        operations: _SimpleMasterOperations,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.tab = tab
        self.operations = operations
        self.list_widget = QListWidget(self)
        self.list_widget.currentItemChanged.connect(self._update_buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)

        self.add_button = self._button("追加", buttons, self._add)
        self.rename_button = self._button("名称変更", buttons, self._rename)
        self.deactivate_button: QPushButton | None = None
        self.reactivate_button: QPushButton | None = None
        if tab is not MasterTab.LOCATION:
            self.deactivate_button = self._button("無効化", buttons, self._deactivate)
            self.reactivate_button = self._button("再有効化", buttons, self._reactivate)
        self.delete_button = self._button("削除", buttons, self._delete)
        self.refresh()

    @staticmethod
    def _button(text: str, layout: QHBoxLayout, callback: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def refresh(self, selected_id: int | None = None) -> None:
        if selected_id is None:
            selected_id = self._selected_id()
        succeeded, records = run_guarded(self, self.operations.list_records)
        if not succeeded or records is None:
            return
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        selected_item = None
        for entity_id, name, is_active in records:
            label = name if is_active else f"{name} (無効)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, entity_id)
            item.setData(_NAME_ROLE, name)
            item.setData(_ACTIVE_ROLE, is_active)
            if not is_active:
                item.setForeground(Qt.GlobalColor.gray)
            self.list_widget.addItem(item)
            if entity_id == selected_id:
                selected_item = item
        if selected_item is not None:
            self.list_widget.setCurrentItem(selected_item)
        self.list_widget.blockSignals(False)
        self._update_buttons()

    def _selected_id(self) -> int | None:
        item = self.list_widget.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def _selected(self) -> tuple[int, str, bool] | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return (
            item.data(Qt.ItemDataRole.UserRole),
            item.data(_NAME_ROLE),
            item.data(_ACTIVE_ROLE),
        )

    def _update_buttons(self, *_args) -> None:
        selected = self._selected()
        enabled = selected is not None
        self.rename_button.setEnabled(enabled)
        if self.deactivate_button is not None and self.reactivate_button is not None:
            self.deactivate_button.setEnabled(enabled and bool(selected and selected[2]))
            self.reactivate_button.setEnabled(enabled and bool(selected and not selected[2]))
        can_delete = False
        if selected is not None:
            can_delete = self._can_delete(selected[0])
        self.delete_button.setEnabled(can_delete)
        self.delete_button.setToolTip("" if can_delete or selected is None else _INACTIVE_TOOLTIP)

    def _can_delete(self, entity_id: int) -> bool:
        succeeded, can_delete = run_guarded(self, lambda: self.operations.can_delete(entity_id))
        return succeeded and bool(can_delete)

    def _call(self, action: Callable[[], object], selected_id: int | None = None) -> bool:
        succeeded, _result = run_guarded(self, action)
        if succeeded:
            self.context.data_bus.data_changed.emit()
            self.refresh(selected_id)
        return succeeded

    def _add(self) -> None:
        name, accepted = QInputDialog.getText(self, "マスタの追加", "名称")
        if not accepted:
            return
        self._call(lambda: self.operations.add(name))

    def _rename(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        entity_id, current_name, _active = selected
        name, accepted = QInputDialog.getText(
            self, "名称変更", "名称", QLineEdit.EchoMode.Normal, current_name
        )
        if not accepted:
            return
        self._call(lambda: self.operations.rename(entity_id, name), entity_id)

    def _deactivate(self) -> None:
        selected = self._selected()
        deactivate = self.operations.deactivate
        if selected is None or deactivate is None:
            return
        entity_id = selected[0]
        self._call(lambda: deactivate(entity_id), entity_id)

    def _reactivate(self) -> None:
        selected = self._selected()
        reactivate = self.operations.reactivate
        if selected is None or reactivate is None:
            return
        entity_id = selected[0]
        self._call(lambda: reactivate(entity_id), entity_id)

    def _delete(self) -> None:
        selected = self._selected()
        if selected is None or not self._can_delete(selected[0]):
            self._update_buttons()
            return
        entity_id, name, _active = selected
        answer = QMessageBox.question(self, "マスタの削除", f"「{name}」を削除しますか？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._call(lambda: self.operations.delete(entity_id))


class _CategoryTab(QWidget):
    def __init__(self, context: AppContext, parent: QWidget) -> None:
        super().__init__(parent)
        self.context = context
        self.tree = QTreeWidget(self)
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(("名称", "接頭辞", "次番号"))
        self.tree.currentItemChanged.connect(self._update_buttons)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tree)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        self.add_button = self._button("追加", buttons, self._add)
        self.add_child_button = self._button("子カテゴリ追加", buttons, self._add_child)
        self.rename_button = self._button("名称変更", buttons, self._rename)
        self.move_button = self._button("親変更", buttons, self._move)
        self.prefix_button = self._button("接頭辞変更", buttons, self._change_prefix)
        self.delete_button = self._button("削除", buttons, self._delete)
        self.refresh()

    @staticmethod
    def _button(text: str, layout: QHBoxLayout, callback: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _categories(self) -> list[Category]:
        succeeded, categories = run_guarded(self, self.context.master.list_categories)
        return categories if succeeded and categories is not None else []

    def refresh(self, selected_id: int | None = None) -> None:
        if selected_id is None:
            selected_id = self._selected_id()
        categories = self._categories()
        self.tree.blockSignals(True)
        self.tree.clear()
        children: dict[int | None, list[Category]] = {}
        for category in categories:
            children.setdefault(category.parent_id, []).append(category)
        for siblings in children.values():
            siblings.sort(key=lambda category: category.name)
        selected_item = None

        def append_children(parent_item: QTreeWidgetItem | None, parent_id: int | None) -> None:
            nonlocal selected_item
            for category in children.get(parent_id, []):
                item = QTreeWidgetItem(
                    [category.name, category.code_prefix, str(category.next_seq)]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, category.id)
                if parent_item is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)
                if category.id == selected_id:
                    selected_item = item
                append_children(item, category.id)

        append_children(None, None)
        self.tree.expandAll()
        if selected_item is not None:
            self.tree.setCurrentItem(selected_item)
        self.tree.blockSignals(False)
        self._update_buttons()

    def _selected_id(self) -> int | None:
        item = self.tree.currentItem()
        return None if item is None else item.data(0, Qt.ItemDataRole.UserRole)

    def _selected_category(self) -> Category | None:
        selected_id = self._selected_id()
        if selected_id is None:
            return None
        return next((item for item in self._categories() if item.id == selected_id), None)

    def _update_buttons(self, *_args) -> None:
        category = self._selected_category()
        enabled = category is not None
        self.add_child_button.setEnabled(enabled)
        self.rename_button.setEnabled(enabled)
        self.move_button.setEnabled(enabled)
        prefix_editable = enabled and category.next_seq <= 1
        self.prefix_button.setEnabled(prefix_editable)
        self.prefix_button.setToolTip(
            "" if prefix_editable or not enabled else "採番済みのため変更できません"
        )
        can_delete = enabled and self._can_delete_category(category.id)
        self.delete_button.setEnabled(can_delete)
        self.delete_button.setToolTip(
            ""
            if can_delete or not enabled
            else "子カテゴリ・品目・採番実績があるため削除できません"
        )

    def _can_delete_category(self, category_id: int) -> bool:
        succeeded, can_delete = run_guarded(
            self, lambda: self.context.master.can_delete_category(category_id)
        )
        return succeeded and bool(can_delete)

    def _call(self, action: Callable[[], object], selected_id: int | None = None) -> bool:
        succeeded, _result = run_guarded(self, action)
        if succeeded:
            self.context.data_bus.data_changed.emit()
            self.refresh(selected_id)
        return succeeded

    def _create_category(self, parent_id: int | None) -> None:
        dialog = _CategoryCreateDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, prefix = dialog.values()
        self._call(lambda: self.context.master.add_category(name, prefix, parent_id), parent_id)

    def _add(self) -> None:
        self._create_category(None)

    def _add_child(self) -> None:
        parent_id = self._selected_id()
        if parent_id is not None:
            self._create_category(parent_id)

    def _rename(self) -> None:
        category = self._selected_category()
        if category is None:
            return
        name, accepted = QInputDialog.getText(
            self, "カテゴリ名の変更", "名称", QLineEdit.EchoMode.Normal, category.name
        )
        if accepted:
            self._call(lambda: self.context.master.rename_category(category.id, name), category.id)

    def _move(self) -> None:
        category = self._selected_category()
        if category is None:
            return
        dialog = _CategoryParentDialog(self._categories(), category.parent_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            parent_id = dialog.parent_category_id()
            self._call(
                lambda: self.context.master.move_category(category.id, parent_id), category.id
            )

    def _change_prefix(self) -> None:
        category = self._selected_category()
        if category is None or category.next_seq > 1:
            return
        dialog = _PrefixDialog(category.code_prefix, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            prefix = dialog.value()
            self._call(
                lambda: self.context.master.change_category_prefix(category.id, prefix), category.id
            )

    def _delete(self) -> None:
        category = self._selected_category()
        if category is None or not self._can_delete_category(category.id):
            self._update_buttons()
            return
        answer = QMessageBox.question(
            self, "カテゴリの削除", f"カテゴリ「{category.name}」を削除しますか？"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._call(lambda: self.context.master.delete_category(category.id))


class _CategoryCreateDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("カテゴリの追加")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("名称", self))
        self.name_edit = QLineEdit(self)
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("接頭辞 (英大文字・数字 2〜5 文字)", self))
        self.prefix_edit = QLineEdit(self)
        validator = QRegularExpressionValidator(QRegularExpression("[A-Za-z0-9]{2,5}"), self)
        self.prefix_edit.setValidator(validator)
        self.prefix_edit.textEdited.connect(lambda text: self.prefix_edit.setText(text.upper()))
        layout.addWidget(self.prefix_edit)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self) -> tuple[str, str]:
        return self.name_edit.text(), self.prefix_edit.text()


class _PrefixDialog(QDialog):
    def __init__(self, prefix: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("接頭辞の変更")
        layout = QVBoxLayout(self)
        self.prefix_edit = QLineEdit(prefix, self)
        self.prefix_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression("[A-Za-z0-9]{2,5}"), self)
        )
        self.prefix_edit.textEdited.connect(lambda text: self.prefix_edit.setText(text.upper()))
        layout.addWidget(QLabel("接頭辞 (英大文字・数字 2〜5 文字)", self))
        layout.addWidget(self.prefix_edit)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def value(self) -> str:
        return self.prefix_edit.text()


class _CategoryParentDialog(CategoryPickerDialog):
    def __init__(self, categories: list[Category], parent_id: int | None, parent: QWidget) -> None:
        super().__init__(categories, "(最上位)", parent_id, parent)
        self.setWindowTitle("親カテゴリの変更")

    def parent_category_id(self) -> int | None:
        return self.selected_id()
