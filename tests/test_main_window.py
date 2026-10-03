from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox

from inventory_manager_mini.core.models import NewItem
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
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
    window.category_combo.set_current_category_id(1)
    window.category_combo.category_changed.emit(1)
    assert _item_ids(window) == {first.id, second.id}
    window.category_combo.set_current_category_id(2)
    window.category_combo.category_changed.emit(2)
    assert _item_ids(window) == {first.id}
    window.category_combo.set_current_category_id(None)
    window.category_combo.category_changed.emit(None)
    window.location_combo.setCurrentIndex(window.location_combo.findData(2))
    assert _item_ids(window) == {second.id}
    window.location_combo.setCurrentIndex(0)
    window.low_stock_checkbox.setChecked(True)
    assert _item_ids(window) == {second.id}

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


def test_data_changed_refreshes_master_options(window_with_items) -> None:
    window, _first, _second = window_with_items

    window.context.master.add_client("新規クライアント")
    window.context.data_bus.data_changed.emit()

    assert window.client_combo.findText("新規クライアント") >= 0
    assert window.statusBar().currentMessage() == "1 件"
