from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QInputDialog,
    QMenu,
    QMessageBox,
    QTreeWidgetItem,
)

from inventory_manager_mini.core.errors import MasterInUseError
from inventory_manager_mini.core.models import NewItem
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs import master_dialog as master_dialog_module
from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog
from inventory_manager_mini.ui.dialogs.master_dialog import MasterDialog, MasterTab, _CategoryTab
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.ui.signals import DataBus


@pytest.fixture
def dialog_context(
    inventory: InventoryService,
    master: MasterService,
    settings: SettingsService,
) -> AppContext:
    return AppContext(inventory, master, settings, DataBus(), Path("test.db"), 1, "0.1.0")


def _category_item(tab: _CategoryTab, category_id: int) -> QTreeWidgetItem | None:
    def find(parent: QTreeWidgetItem) -> QTreeWidgetItem | None:
        for row in range(parent.childCount()):
            item = parent.child(row)
            if item is None:
                continue
            if item.data(0, Qt.ItemDataRole.UserRole) == category_id:
                return item
            found = find(item)
            if found is not None:
                return found
        return None

    for row in range(tab.tree.topLevelItemCount()):
        item = tab.tree.topLevelItem(row)
        if item is None:
            continue
        if item.data(0, Qt.ItemDataRole.UserRole) == category_id:
            return item
        found = find(item)
        if found is not None:
            return found
    return None


def _select_category(tab: _CategoryTab, category_id: int) -> None:
    item = _category_item(tab, category_id)
    assert item is not None
    tab.tree.setCurrentItem(item)


def test_master_dialog_initial_tab_and_five_tabs(qtbot, dialog_context) -> None:
    dialog = MasterDialog(dialog_context, MasterTab.CATEGORY)
    qtbot.addWidget(dialog)

    assert dialog.tabs.count() == 5
    assert dialog.tabs.tabText(dialog.tabs.currentIndex()) == "カテゴリ"
    assert [dialog.tabs.tabText(index) for index in range(5)] == [
        "クライアント",
        "発注主体",
        "担当者",
        "カテゴリ",
        "保管場所",
    ]


@pytest.mark.parametrize(
    (
        "tab_id",
        "existing_id",
        "add_method",
        "rename_method",
        "deactivate_method",
        "reactivate_method",
    ),
    [
        (
            MasterTab.CLIENT,
            2,
            "add_client",
            "rename_client",
            "deactivate_client",
            "reactivate_client",
        ),
        (
            MasterTab.PURCHASER,
            2,
            "add_purchaser",
            "rename_purchaser",
            "deactivate_purchaser",
            "reactivate_purchaser",
        ),
        (MasterTab.STAFF, 2, "add_staff", "rename_staff", "deactivate_staff", "reactivate_staff"),
    ],
)
def test_simple_master_add_rename_deactivate_reactivate_and_delete(
    qtbot,
    dialog_context,
    monkeypatch,
    tab_id,
    existing_id,
    add_method,
    rename_method,
    deactivate_method,
    reactivate_method,
) -> None:
    dialog = MasterDialog(dialog_context, tab_id)
    qtbot.addWidget(dialog)
    tab = dialog.simple_tabs[tab_id]
    list_entries = {
        MasterTab.CLIENT: dialog_context.master.list_clients,
        MasterTab.PURCHASER: dialog_context.master.list_purchasers,
        MasterTab.STAFF: dialog_context.master.list_staff,
    }[tab_id]
    emissions: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: emissions.append(True))

    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("追加マスタ", True))
    tab.add_button.click()
    entries = list_entries(include_inactive=True)
    added = next(entry for entry in entries if entry.name == "追加マスタ")
    assert len(emissions) == 1

    existing_item = next(
        tab.list_widget.item(index)
        for index in range(tab.list_widget.count())
        if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == existing_id
    )
    tab.list_widget.setCurrentItem(existing_item)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("名称変更後", True))
    tab.rename_button.click()
    renamed = next(entry for entry in list_entries(True) if entry.id == existing_id)
    assert renamed.name == "名称変更後"

    tab.list_widget.setCurrentItem(
        next(
            tab.list_widget.item(index)
            for index in range(tab.list_widget.count())
            if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == existing_id
        )
    )
    assert tab.deactivate_button is not None
    assert tab.reactivate_button is not None
    tab.deactivate_button.click()
    inactive_item = tab.list_widget.currentItem()
    assert inactive_item.text() == "名称変更後 (無効)"
    assert inactive_item.foreground().color() == Qt.GlobalColor.gray
    assert not tab.deactivate_button.isEnabled()
    assert tab.reactivate_button.isEnabled()
    tab.reactivate_button.click()
    assert tab.list_widget.currentItem().text() == "名称変更後"

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: QMessageBox.StandardButton.Yes,
    )
    tab.list_widget.setCurrentItem(
        next(
            tab.list_widget.item(index)
            for index in range(tab.list_widget.count())
            if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == added.id
        )
    )
    assert tab.delete_button.isEnabled()
    tab.delete_button.click()
    assert all(entry.id != added.id for entry in list_entries(True))
    assert len(emissions) == 5


def test_simple_master_disables_delete_for_in_use_record(qtbot, dialog_context) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="使用中", category_id=1)
    )
    dialog = MasterDialog(dialog_context, MasterTab.CLIENT)
    qtbot.addWidget(dialog)
    tab = dialog.simple_tabs[MasterTab.CLIENT]
    selected = next(
        tab.list_widget.item(index)
        for index in range(tab.list_widget.count())
        if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == item.client_id
    )
    tab.list_widget.setCurrentItem(selected)

    assert not tab.delete_button.isEnabled()
    assert tab.delete_button.toolTip() == "使用中のため削除できません。無効化してください"


def test_category_controls_create_move_cycle_prefix_and_delete(
    qtbot, dialog_context, monkeypatch
) -> None:
    dialog = MasterDialog(dialog_context, MasterTab.CATEGORY)
    qtbot.addWidget(dialog)
    tab = dialog.category_tab

    class CreateDialog:
        def __init__(self, _parent):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self) -> tuple[str, str]:
            return "新カテゴリ", "NEW"

    monkeypatch.setattr(master_dialog_module, "_CategoryCreateDialog", CreateDialog)
    tab.add_button.click()
    added = next(
        item for item in dialog_context.master.list_categories() if item.name == "新カテゴリ"
    )
    assert added.parent_id is None

    _select_category(tab, 1)

    class ChildDialog(CreateDialog):
        def values(self) -> tuple[str, str]:
            return "子カテゴリ", "CHD"

    monkeypatch.setattr(master_dialog_module, "_CategoryCreateDialog", ChildDialog)
    tab.add_child_button.click()
    child = next(
        item for item in dialog_context.master.list_categories() if item.name == "子カテゴリ"
    )
    assert child.parent_id == 1

    class ParentDialog:
        selected_parent_id = child.id

        def __init__(self, _categories, _parent_id, _parent):
            self.parent_id = self.selected_parent_id

        def exec(self):
            return QDialog.DialogCode.Accepted

        def parent_category_id(self):
            return self.parent_id

    monkeypatch.setattr(master_dialog_module, "_CategoryParentDialog", ParentDialog)
    _select_category(tab, added.id)
    tab.move_button.click()
    assert (
        next(
            item for item in dialog_context.master.list_categories() if item.id == added.id
        ).parent_id
        == child.id
    )

    _select_category(tab, added.id)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("更新カテゴリ", True))
    tab.rename_button.click()
    renamed = next(item for item in dialog_context.master.list_categories() if item.id == added.id)
    assert renamed.name == "更新カテゴリ"

    class PrefixDialog:
        def __init__(self, _prefix, _parent):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def value(self):
            return "UPD"

    monkeypatch.setattr(master_dialog_module, "_PrefixDialog", PrefixDialog)
    tab.prefix_button.click()
    assert (
        next(
            item for item in dialog_context.master.list_categories() if item.id == added.id
        ).code_prefix
        == "UPD"
    )

    _select_category(tab, 1)
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    emission_count: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: emission_count.append(True))
    tab.move_button.click()
    assert warnings and "子孫" in warnings[-1]
    assert not emission_count

    _select_category(tab, 1)
    assert not tab.delete_button.isEnabled()
    assert "削除できません" in tab.delete_button.toolTip()

    dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="採番済み", category_id=added.id)
    )
    tab.refresh(added.id)
    assert not tab.prefix_button.isEnabled()
    assert tab.prefix_button.toolTip() == "採番済みのため変更できません"
    assert not tab.delete_button.isEnabled()

    class DeleteCandidateDialog(CreateDialog):
        def values(self) -> tuple[str, str]:
            return "削除候補", "DEL"

    monkeypatch.setattr(master_dialog_module, "_CategoryCreateDialog", DeleteCandidateDialog)
    tab.add_button.click()
    candidate = next(
        item for item in dialog_context.master.list_categories() if item.name == "削除候補"
    )
    _select_category(tab, candidate.id)
    assert tab.delete_button.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    tab.delete_button.click()
    assert all(item.id != candidate.id for item in dialog_context.master.list_categories())


def test_location_crud_and_failed_delete_does_not_emit(qtbot, dialog_context, monkeypatch) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="保管中", category_id=1, location_id=1)
    )
    dialog = MasterDialog(dialog_context, MasterTab.LOCATION)
    qtbot.addWidget(dialog)
    tab = dialog.location_tab
    used_item = next(
        tab.list_widget.item(index)
        for index in range(tab.list_widget.count())
        if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == item.location_id
    )
    tab.list_widget.setCurrentItem(used_item)
    assert not tab.delete_button.isEnabled()
    assert tab.deactivate_button is None

    emissions: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: emissions.append(True))
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("別室", True))
    tab.add_button.click()
    new_location = next(
        location for location in dialog_context.master.list_locations() if location.name == "別室"
    )
    tab.list_widget.setCurrentItem(
        next(
            tab.list_widget.item(index)
            for index in range(tab.list_widget.count())
            if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == new_location.id
        )
    )
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("別室・改", True))
    tab.rename_button.click()
    assert any(location.name == "別室・改" for location in dialog_context.master.list_locations())
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    tab.delete_button.click()
    assert all(
        location.id != new_location.id for location in dialog_context.master.list_locations()
    )
    assert len(emissions) == 3

    monkeypatch.setattr(dialog_context.master, "can_delete_location", lambda _entity_id: True)

    def reject_delete(_entity_id):
        raise MasterInUseError("使用中です")

    monkeypatch.setattr(dialog_context.master, "delete_location", reject_delete)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text: warnings.append(text))
    failing_dialog = MasterDialog(dialog_context, MasterTab.LOCATION)
    qtbot.addWidget(failing_dialog)
    failing_tab = failing_dialog.location_tab
    failing_tab.list_widget.setCurrentItem(
        next(
            failing_tab.list_widget.item(index)
            for index in range(failing_tab.list_widget.count())
            if failing_tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == 2
        )
    )
    failing_tab.delete_button.click()

    assert len(emissions) == 3
    assert warnings == ["使用中です"]


def test_add_purchaser_then_reopen_item_dialog(qtbot, dialog_context, monkeypatch) -> None:
    dialog_context.master.deactivate_purchaser(1)
    dialog_context.master.deactivate_purchaser(2)
    item_dialog = ItemDialog(dialog_context)
    qtbot.addWidget(item_dialog)
    assert item_dialog.purchaser_combo.count() == 0
    assert "先に発注主体マスタを登録してください" in item_dialog.error_label.text()
    assert not item_dialog.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    item_dialog.reject()

    master_dialog = MasterDialog(dialog_context, MasterTab.PURCHASER)
    qtbot.addWidget(master_dialog)
    purchaser_tab = master_dialog.simple_tabs[MasterTab.PURCHASER]
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("新発注主体", True))
    purchaser_tab.add_button.click()
    master_dialog.reject()

    reopened = ItemDialog(dialog_context)
    qtbot.addWidget(reopened)
    assert reopened.purchaser_combo.findText("新発注主体") >= 0
    reopened.client_combo.setCurrentIndex(reopened.client_combo.findData(1))
    reopened.name_edit.setText("新しい品目")
    reopened.category_combo.set_current_category_id(1)
    reopened.purchaser_combo.setCurrentIndex(reopened.purchaser_combo.findText("新発注主体"))
    assert reopened.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_master_change_refreshes_main_window_and_menu_opens_requested_tab(
    qtbot, dialog_context, monkeypatch
) -> None:
    dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="連携確認", category_id=1, location_id=1)
    )
    window = MainWindow(dialog_context)
    qtbot.addWidget(window)
    master_dialog = MasterDialog(dialog_context, MasterTab.CLIENT, window)
    qtbot.addWidget(master_dialog)
    tab = master_dialog.simple_tabs[MasterTab.CLIENT]
    client_item = next(
        tab.list_widget.item(index)
        for index in range(tab.list_widget.count())
        if tab.list_widget.item(index).data(Qt.ItemDataRole.UserRole) == 1
    )
    tab.list_widget.setCurrentItem(client_item)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("更新クライアント", True))
    tab.rename_button.click()

    assert window.client_combo.itemText(window.client_combo.findData(1)) == "更新クライアント"
    assert window.item_model.row_at(0).client_name == "更新クライアント"

    requested_tabs: list[MasterTab] = []

    def fake_exec(_dialog):
        requested_tabs.append(_dialog.tabs.tabText(_dialog.tabs.currentIndex()))
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(MasterDialog, "exec", fake_exec)
    master_menu = next(
        action.menu() for action in window.menuBar().actions() if action.text() == "マスタ"
    )
    assert isinstance(master_menu, QMenu)
    for action in master_menu.actions():
        action.trigger()

    assert requested_tabs == ["クライアント", "発注主体", "担当者", "カテゴリ", "保管場所"]
