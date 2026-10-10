from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QMessageBox

from inventory_manager_mini.core.models import REASON_LABELS, NewItem, Reason
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.stock_move_dialog import StockMoveDialog
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.ui.signals import DataBus


def _item_ids(window: MainWindow) -> set[int]:
    return {window.item_model.row_at(row).id for row in range(window.item_model.rowCount())}


def _select_item(window: MainWindow, item_id: int) -> None:
    source_row = window.item_model.row_of(item_id)
    assert source_row is not None
    proxy_index = window.sort_model.mapFromSource(window.item_model.index(source_row, 0))
    window.table.selectRow(proxy_index.row())


@pytest.fixture
def window_with_items(
    qtbot,
    inventory: InventoryService,
    master: MasterService,
    settings: SettingsService,
):
    first = inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="鉛筆",
            category_id=2,
            location_id=1,
            initial_quantity=1,
            initial_staff_id=1,
            manufacturer_part_number="P-100",
        )
    )
    second = inventory.create_item(
        NewItem(
            client_id=2,
            purchaser_id=2,
            name="備品ケース",
            category_id=1,
            location_id=2,
            initial_quantity=2,
            initial_staff_id=1,
            reorder_threshold=3,
            manufacturer_part_number="CASE-200",
        )
    )
    inventory.deactivate_item(second.id)
    context = AppContext(
        inventory=inventory,
        master=master,
        settings=settings,
        data_bus=DataBus(),
        db_path=Path("test.db"),
        schema_version=1,
        app_version="0.1.0",
    )
    window = MainWindow(context)
    qtbot.addWidget(window)
    return window, first, second


def test_main_window_starts_with_active_items_and_disabled_item_actions(window_with_items) -> None:
    window, first, _second = window_with_items

    assert window.windowTitle() == "Inventory Manager mini"
    assert _item_ids(window) == {first.id}
    assert not window.edit_action.isEnabled()
    assert not window.toggle_active_action.isEnabled()
    assert window.table.selectionBehavior() == window.table.SelectionBehavior.SelectRows
    assert window.table.selectionMode() == window.table.SelectionMode.SingleSelection


def test_search_debounces_name_code_and_manufacturer_number(qtbot, window_with_items) -> None:
    window, first, _second = window_with_items
    window.inactive_checkbox.setChecked(True)

    for search_text in ("鉛", "ST-0001", "P-100"):
        window.search_edit.setText(search_text)
        qtbot.waitUntil(lambda: _item_ids(window) == {first.id}, timeout=1500)
        window.search_edit.clear()
        qtbot.waitUntil(lambda: len(_item_ids(window)) == 2, timeout=1500)


def test_low_stock_background_survives_proxy_sort_filter_and_selection(window_with_items) -> None:
    window, _first, _second = window_with_items
    low_stock_item = window.context.inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="閾値ゼロ品目",
            category_id=2,
            initial_quantity=0,
            initial_staff_id=1,
            reorder_threshold=0,
        )
    )
    window.context.data_bus.data_changed.emit()
    window.sort_model.sort(1, Qt.SortOrder.AscendingOrder)
    low_stock_source_row = window.item_model.row_of(low_stock_item.id)
    assert low_stock_source_row is not None
    low_stock_proxy_index = window.sort_model.mapFromSource(
        window.item_model.index(low_stock_source_row, 0)
    )

    assert low_stock_proxy_index.data(Qt.ItemDataRole.BackgroundRole).color().name() == "#fff3cd"

    window.low_stock_checkbox.setChecked(True)
    assert _item_ids(window) == {low_stock_item.id}
    window.table.selectRow(0)

    selected_rows = window.table.selectionModel().selectedRows()
    assert len(selected_rows) == 1
    assert window._selected_item_id() == low_stock_item.id
    assert selected_rows[0].data(Qt.ItemDataRole.BackgroundRole).color().name() == "#fff3cd"


def test_filter_controls_and_inactive_master_labels(window_with_items) -> None:
    window, first, second = window_with_items

    assert window.client_combo.findData(3) >= 0
    assert (
        window.client_combo.itemText(window.client_combo.findData(3)) == "廃止クライアント (無効)"
    )
    assert (
        window.purchaser_combo.itemText(window.purchaser_combo.findData(3)) == "廃止発注主体 (無効)"
    )

    window.inactive_checkbox.setChecked(True)
    assert window.include_inactive_action.isChecked()
    assert _item_ids(window) == {first.id, second.id}

    _select_item(window, first.id)
    window.client_combo.setCurrentIndex(window.client_combo.findData(2))
    assert _item_ids(window) == {second.id}
    assert window._selected_item_id() is None
    window.client_combo.setCurrentIndex(0)
    window.purchaser_combo.setCurrentIndex(window.purchaser_combo.findData(1))
    assert _item_ids(window) == {first.id}
    window.purchaser_combo.setCurrentIndex(0)
    window.category_picker.set_current_category_id(1)
    window.category_picker.category_changed.emit(1)
    assert _item_ids(window) == {first.id, second.id}
    window.category_picker.set_current_category_id(2)
    window.category_picker.category_changed.emit(2)
    assert _item_ids(window) == {first.id}
    window.category_picker.set_current_category_id(None)
    window.category_picker.category_changed.emit(None)
    window.location_combo.setCurrentIndex(window.location_combo.findData(2))
    assert _item_ids(window) == {second.id}
    window.location_combo.setCurrentIndex(0)
    window.low_stock_checkbox.setChecked(True)
    assert _item_ids(window) == set()

    window.include_inactive_action.setChecked(False)
    assert not window.inactive_checkbox.isChecked()
    assert _item_ids(window) == set()


def test_selection_actions_and_deactivate_reactivate(window_with_items, monkeypatch) -> None:
    window, first, second = window_with_items
    window.inactive_checkbox.setChecked(True)
    _select_item(window, second.id)

    assert window.edit_action.isEnabled()
    assert window.toggle_active_action.isEnabled()
    assert window.toggle_active_action.text() == "再有効化"
    window.toggle_active_action.trigger()
    assert window.context.inventory.get_item(second.id).is_active
    assert window._selected_item_id() == second.id

    _select_item(window, first.id)
    assert window.toggle_active_action.text() == "廃止"
    confirmations = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: confirmations.append(args) or QMessageBox.StandardButton.Yes,
    )
    window.toggle_active_action.trigger()

    assert confirmations
    assert "現在の在庫数量: 1 個" in confirmations[0][2]
    assert not window.context.inventory.get_item(first.id).is_active
    assert window._selected_item_id() == first.id
    assert window.item_model.row_at(window.item_model.row_of(first.id)).is_active is False


def test_item_actions_are_shared_by_menu_toolbar_and_context_menu(window_with_items) -> None:
    window, first, second = window_with_items
    item_actions = [window.new_action, window.edit_action, window.toggle_active_action]
    stock_actions = list(window.stock_actions.values())

    toolbar_actions = window.toolbar.actions()
    assert toolbar_actions[:3] == item_actions
    assert toolbar_actions[3].isSeparator()
    assert toolbar_actions[4:6] == [
        window.stock_actions[Reason.IN],
        window.stock_actions[Reason.OUT],
    ]
    assert window.toolbar.widgetForAction(toolbar_actions[6]) is window.return_dispose_button
    assert toolbar_actions[7] == window.stock_actions[Reason.ADJUST]
    assert window.return_dispose_button.text() == "返品・廃棄"
    assert window.return_dispose_menu.actions() == [
        window.stock_actions[Reason.RETURN],
        window.stock_actions[Reason.DISPOSE],
    ]
    assert toolbar_actions[8].isSeparator()
    assert toolbar_actions[9] is window.history_action
    assert window.table.actions()[:3] == item_actions
    assert window.table.actions()[3].isSeparator()
    assert window.table.actions()[4:9] == stock_actions
    assert window.table.actions()[9].isSeparator()
    assert window.table.actions()[10] is window.history_action
    assert window.table.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu
    item_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "品目")
    assert item_menu.actions()[:3] == item_actions
    assert item_menu.actions()[3].isSeparator()
    assert item_menu.actions()[4] is window.history_action
    stock_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "在庫")
    assert stock_menu.actions() == stock_actions
    assert [action.text() for action in stock_actions] == [
        REASON_LABELS[reason] for reason in Reason
    ]

    assert window.new_action.isEnabled()
    assert not window.history_action.isEnabled()
    assert all(not action.isEnabled() for action in stock_actions)
    _select_item(window, first.id)
    assert window.edit_action.isEnabled()
    assert window.history_action.isEnabled()
    assert all(action.isEnabled() for action in stock_actions)

    window.inactive_checkbox.setChecked(True)
    _select_item(window, second.id)
    assert window.edit_action.isEnabled()
    assert window.history_action.isEnabled()
    assert window.toggle_active_action.text() == "再有効化"
    assert all(not action.isEnabled() for action in stock_actions)


def test_double_click_opens_history_for_clicked_item(window_with_items, monkeypatch) -> None:
    window, _first, second = window_with_items
    window.inactive_checkbox.setChecked(True)
    window.show()
    proxy_index = window.sort_model.mapFromSource(
        window.item_model.index(window.item_model.row_of(second.id), 0)
    )
    opened_item_ids = []
    edit_calls = []
    monkeypatch.setattr(
        HistoryDialog,
        "exec",
        lambda dialog: opened_item_ids.append(dialog.item_id) or QDialog.DialogCode.Rejected,
    )
    monkeypatch.setattr(window, "_open_item_dialog", lambda *_args: edit_calls.append(True))

    window.table.doubleClicked.emit(proxy_index)

    assert opened_item_ids == [second.id]
    assert not edit_calls
    assert window._selected_item_id() == second.id


def test_history_reversal_refreshes_main_window_quantity(window_with_items, monkeypatch) -> None:
    window, first, _second = window_with_items
    movement = window.context.inventory.list_history(first.id)[0]

    def reverse_on_exec(dialog: HistoryDialog) -> int:
        window.context.inventory.reverse(movement.id, staff_id=1)
        window.context.data_bus.data_changed.emit()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(HistoryDialog, "exec", reverse_on_exec)
    _select_item(window, first.id)

    window.history_action.trigger()

    assert window.context.inventory.get_item(first.id).quantity == 0
    assert window.item_model.row_at(window.item_model.row_of(first.id)).quantity == 0
    assert window._selected_item_id() == first.id


def test_stock_move_action_refreshes_quantity_and_preserves_selection(
    window_with_items, monkeypatch
) -> None:
    window, first, _second = window_with_items

    def save_on_exec(dialog: StockMoveDialog) -> int:
        dialog.staff_combo.setCurrentIndex(1)
        dialog.quantity_spin.setValue(1)
        dialog._save()
        return dialog.result()

    monkeypatch.setattr(StockMoveDialog, "exec", save_on_exec)
    _select_item(window, first.id)

    window.stock_actions[Reason.IN].trigger()

    assert window.context.inventory.get_item(first.id).quantity == 2
    assert window.item_model.row_at(window.item_model.row_of(first.id)).quantity == 2
    assert window._selected_item_id() == first.id


def test_data_changed_refreshes_master_options(window_with_items) -> None:
    window, _first, _second = window_with_items

    window.context.master.add_client("新規クライアント")
    window.context.data_bus.data_changed.emit()

    assert window.client_combo.findText("新規クライアント") >= 0
    assert window.statusBar().currentMessage() == "1 件"
