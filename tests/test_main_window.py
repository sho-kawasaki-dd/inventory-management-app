from __future__ import annotations

import csv
import hashlib
import logging
import sqlite3
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QItemSelectionModel, QRect, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMessageBox,
    QToolBar,
    QToolButton,
    QWidget,
)

from inventory_manager_mini.core import services
from inventory_manager_mini.core.errors import InvalidBackupError, RestoreError
from inventory_manager_mini.core.models import (
    REASON_LABELS,
    HistoryPeriod,
    HistoryPeriodKind,
    ItemRow,
    NewItem,
    Reason,
)
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.db.connection import connect, connect_readonly
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, create_schema
from inventory_manager_mini.ui import main_window as main_window_module
from inventory_manager_mini.ui.csv_export import _choose_export_path
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.history_export_dialog import HistoryExportDialog
from inventory_manager_mini.ui.dialogs.stock_move_dialog import StockMoveDialog
from inventory_manager_mini.ui.display_settings import DisplaySettings
from inventory_manager_mini.ui.main_window import ExitRequest, MainWindow
from inventory_manager_mini.ui.printing.layout import LabelEntry, LabelPlacement
from inventory_manager_mini.ui.widgets.dashboard import DashboardWindow
from tests.conftest import create_test_context


def _item_ids(window: MainWindow) -> set[int]:
    return {window.item_model.row_at(row).id for row in range(window.item_model.rowCount())}


def _select_item(window: MainWindow, item_id: int) -> None:
    source_row = window.item_model.row_of(item_id)
    assert source_row is not None
    proxy_index = window.sort_model.mapFromSource(window.item_model.index(source_row, 0))
    selection_model = window.table.selectionModel()
    selection_model.select(
        proxy_index,
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    selection_model.setCurrentIndex(proxy_index, QItemSelectionModel.SelectionFlag.NoUpdate)


def _select_items(window: MainWindow, item_ids: list[int]) -> None:
    selection_model = window.table.selectionModel()
    selection_model.clearSelection()
    for item_id in item_ids:
        source_row = window.item_model.row_of(item_id)
        assert source_row is not None
        proxy_index = window.sort_model.mapFromSource(window.item_model.index(source_row, 0))
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )


def _set_model_purchase_url(window: MainWindow, item_id: int, url: str | None) -> None:
    rows = [
        replace(window.item_model.row_at(row), purchase_url=url)
        if window.item_model.row_at(row).id == item_id
        else window.item_model.row_at(row)
        for row in range(window.item_model.rowCount())
    ]
    window.item_model.set_rows(rows)


@pytest.fixture
def window_with_items(
    qtbot,
    seeded_conn,
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
    context = create_test_context(
        seeded_conn,
        inventory=inventory,
        master=master,
        settings=settings,
        db_path=Path("test.db"),
        schema_version=1,
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
    assert window.table.selectionMode() == window.table.SelectionMode.ExtendedSelection


def test_about_dialog_shows_legal_notice_and_opens_license_document(
    monkeypatch, tmp_path: Path, window_with_items
) -> None:
    window, _first, _second = window_with_items
    license_path = tmp_path / "DISTRIBUTION_LICENSE.txt"
    license_path.write_text("license text", encoding="utf-8")
    opened_urls: list[QUrl] = []
    monkeypatch.setattr(main_window_module, "_distribution_license_path", lambda: license_path)
    monkeypatch.setattr(
        main_window_module.QDesktopServices,
        "openUrl",
        lambda url: opened_urls.append(url) or True,
    )

    def click_license_button(dialog: QMessageBox) -> int:
        notice = dialog.informativeText()
        assert "Copyright (C) 2026 IKinoCore" in notice
        assert "GNU General Public License version 3 (GPLv3)" in notice
        assert "再配布・改変できます" in notice
        assert "無保証" in notice
        license_button = next(
            button for button in dialog.buttons() if button.text() == "ライセンス文書を開く"
        )
        license_button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", click_license_button)

    window._show_about()

    assert opened_urls == [QUrl.fromLocalFile(str(license_path))]


def test_distribution_license_path_uses_project_packaging_folder() -> None:
    project_root = Path(__file__).resolve().parents[1]

    assert main_window_module._distribution_license_path() == (
        project_root / "packaging" / "DISTRIBUTION_LICENSE.txt"
    )


def test_distribution_license_path_uses_frozen_executable_folder(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "inventory-manager-mini.exe"
    monkeypatch.setattr(main_window_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(main_window_module.sys, "executable", str(executable))

    assert main_window_module._distribution_license_path() == (
        tmp_path / "DISTRIBUTION_LICENSE.txt"
    )


def test_distribution_license_includes_complete_gplv3_text() -> None:
    project_root = Path(__file__).resolve().parents[1]
    gpl_text = (project_root / "packaging" / "licenses" / "GPL-3.0.txt").read_text(encoding="utf-8")
    distribution_text = (project_root / "packaging" / "DISTRIBUTION_LICENSE.txt").read_text(
        encoding="utf-8"
    )

    assert distribution_text.endswith(gpl_text)
    assert "17. Interpretation of Sections 15 and 16." in gpl_text
    assert "How to Apply These Terms to Your New Programs" in gpl_text


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


def test_multiple_selection_disables_single_item_actions_and_survives_refresh(
    window_with_items,
) -> None:
    window, first, second = window_with_items
    window.inactive_checkbox.setChecked(True)
    _select_items(window, [second.id, first.id])

    expected_ids = sorted(
        [first.id, second.id],
        key=lambda item_id: window.sort_model.mapFromSource(
            window.item_model.index(window.item_model.row_of(item_id), 0)
        ).row(),
    )
    assert window._selected_item_ids() == expected_ids
    assert window._selected_item_id() is None
    assert not window.edit_action.isEnabled()
    assert not window.toggle_active_action.isEnabled()
    assert not window.history_action.isEnabled()
    assert not window.purchase_page_action.isEnabled()
    assert all(not action.isEnabled() for action in window.stock_actions.values())

    window.refresh_items()
    assert window._selected_item_ids() == expected_ids

    window.refresh()
    assert window._selected_item_ids() == expected_ids

    window.location_combo.setCurrentIndex(window.location_combo.findData(1))
    assert window._selected_item_ids() == [first.id]


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
    assert toolbar_actions[10] is window.purchase_page_action
    assert toolbar_actions[11].isSeparator()
    assert window.toolbar.widgetForAction(toolbar_actions[12]) is window.csv_export_button
    assert window.csv_export_button.text() == "CSV 出力"
    assert window.csv_export_menu.actions() == [
        window.export_items_action,
        window.export_history_action,
    ]
    assert window.toolbar.widgetForAction(toolbar_actions[13]) is window.print_button
    assert window.print_button.text() == "印刷"
    assert window.print_button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
    assert window.print_toolbar_menu.actions() == [
        window.print_labels_action,
        window.print_inventory_sheet_action,
    ]
    file_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "ファイル")
    csv_menu = next(a.menu() for a in file_menu.actions() if a.text() == "CSV 出力")
    print_menu = next(a.menu() for a in file_menu.actions() if a.text() == "印刷")
    assert csv_menu.actions() == [window.export_items_action, window.export_history_action]
    assert print_menu.actions() == [window.print_labels_action, window.print_inventory_sheet_action]
    print_menu_action_index = next(
        index for index, action in enumerate(file_menu.actions()) if action.menu() is print_menu
    )
    assert print_menu_action_index == 1
    assert window.export_items_action.text() == "在庫一覧"
    assert window.export_history_action.text() == "履歴(全品目)"
    assert window.table.actions()[:3] == item_actions
    assert window.table.actions()[3].isSeparator()
    assert window.table.actions()[4:9] == stock_actions
    assert window.table.actions()[9].isSeparator()
    assert window.table.actions()[10] is window.history_action
    assert window.table.actions()[11] is window.purchase_page_action
    assert window.table.actions()[12].isSeparator()
    assert window.table.actions()[13] is window.print_labels_action
    assert window.table.contextMenuPolicy() == Qt.ContextMenuPolicy.ActionsContextMenu
    item_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "品目")
    assert item_menu.actions()[:3] == item_actions
    assert item_menu.actions()[3].isSeparator()
    assert item_menu.actions()[4:6] == [window.purchase_page_action, window.history_action]
    assert window.purchase_page_action.text() == "販売ページを開く"
    stock_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "在庫")
    assert stock_menu.actions() == stock_actions
    assert [action.text() for action in stock_actions] == [
        REASON_LABELS[reason] for reason in Reason
    ]

    assert window.new_action.isEnabled()
    assert window.print_inventory_sheet_action.isEnabled()
    assert not window.history_action.isEnabled()
    assert not window.print_labels_action.isEnabled()
    assert all(not action.isEnabled() for action in stock_actions)

    _select_item(window, first.id)
    assert window.edit_action.isEnabled()
    assert window.history_action.isEnabled()
    assert window.print_labels_action.isEnabled()
    assert all(action.isEnabled() for action in stock_actions)

    window.inactive_checkbox.setChecked(True)
    _select_item(window, second.id)
    assert window.edit_action.isEnabled()
    assert window.history_action.isEnabled()
    assert window.toggle_active_action.text() == "再有効化"
    assert all(not action.isEnabled() for action in stock_actions)


def test_print_inventory_sheet_uses_last_successful_display_snapshot(
    window_with_items, monkeypatch
) -> None:
    window, first, second = window_with_items
    window.inactive_checkbox.setChecked(True)
    window.sort_model.sort(1, Qt.SortOrder.DescendingOrder)
    expected_rows = [
        window.item_model.row_at(
            window.sort_model.mapToSource(window.sort_model.index(row, 0)).row()
        )
        for row in range(window.sort_model.rowCount())
    ]
    expected_filter = window._displayed_item_filter
    expected_names = window._displayed_filter_names
    assert expected_filter is not None
    assert expected_names is not None
    expected_ids = [item.id for item in expected_rows]
    assert expected_ids == [first.id, second.id]

    error_messages: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, _title, message: error_messages.append(message),
    )
    requested_filters = []

    def fail_list_items(item_filter):
        requested_filters.append(item_filter)
        raise RuntimeError("一覧取得失敗")

    monkeypatch.setattr(window.context.inventory, "list_items", fail_list_items)
    window.search_edit.setText("入力中の未適用条件")
    window.search_timer.stop()
    window.refresh_items()
    assert len(requested_filters) == 1
    assert error_messages
    assert window._displayed_item_filter == expected_filter
    assert window._displayed_filter_names == expected_names
    assert [window.item_model.row_at(row).id for row in range(window.item_model.rowCount())] == [
        first.id,
        second.id,
    ]

    render_calls = []

    def record_render(device, rows, filter_summary, printed_at_local, spec, *, page_numbers):
        render_calls.append((list(rows), filter_summary, printed_at_local, spec, page_numbers))

    monkeypatch.setattr(main_window_module, "render_inventory_sheet", record_render)
    previews = []

    class FakePreviewDialog:
        def __init__(self, printer, orientation, total_pages, render_callback, parent) -> None:
            assert parent is window
            assert orientation == main_window_module.QPageLayout.Orientation.Landscape
            assert total_pages == 1
            self.printer = printer
            self.render_callback = render_callback
            previews.append(self)

        def exec(self) -> int:
            self.render_callback(self.printer, None)
            window.item_model.set_rows([])
            window.search_edit.setText("プレビュー中の変更")
            window.search_timer.stop()
            self.render_callback(self.printer, [1])
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "PrintPreviewDialog", FakePreviewDialog)

    window.print_inventory_sheet_action.trigger()

    assert len(previews) == 1
    assert len(requested_filters) == 1
    assert len(render_calls) == 2
    expected_summary = main_window_module.summarize_item_filter(
        expected_filter,
        client_name=expected_names[0],
        purchaser_name=expected_names[1],
        category_path=expected_names[2],
        location_name=expected_names[3],
    )
    assert all(call[1] == expected_summary for call in render_calls)
    assert all(call[2] == render_calls[0][2] for call in render_calls)
    assert render_calls[0][2]
    assert all([item.id for item in call[0]] == expected_ids for call in render_calls)
    assert all(call[3] is main_window_module.INVENTORY_SHEET_SPEC for call in render_calls)
    assert [call[4] for call in render_calls] == [None, [1]]


def test_print_inventory_sheet_without_displayed_rows_shows_message(
    window_with_items, monkeypatch
) -> None:
    window, _first, _second = window_with_items
    window.search_edit.setText("該当なし")
    window.search_timer.stop()
    window.refresh_items()
    assert window.sort_model.rowCount() == 0

    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, message: messages.append(message),
    )

    def unexpected_preview(*_args, **_kwargs):
        pytest.fail("品目がない場合はプレビューを開きません")

    monkeypatch.setattr(main_window_module, "PrintPreviewDialog", unexpected_preview)
    window.print_inventory_sheet_action.trigger()

    window._displayed_item_filter = None
    window._displayed_filter_names = None
    window.print_inventory_sheet_action.trigger()

    assert messages == ["印刷する品目がありません", "印刷する品目がありません"]


def _display_item_ids(window: MainWindow) -> list[int]:
    return [
        window.item_model.row_at(
            window.sort_model.mapToSource(window.sort_model.index(row, 0)).row()
        ).id
        for row in range(window.sort_model.rowCount())
    ]


def _capture_inventory_sheet_prints(
    window: MainWindow, monkeypatch
) -> list[tuple[list[int], str, str]]:
    calls: list[tuple[list[int], str, str]] = []

    def record_render(device, rows, filter_summary, printed_at_local, spec, *, page_numbers):
        calls.append(([row.id for row in rows], filter_summary, printed_at_local))

    class FakePreviewDialog:
        def __init__(self, printer, orientation, total_pages, render_callback, parent) -> None:
            self.printer = printer
            self.render_callback = render_callback

        def exec(self) -> int:
            self.render_callback(self.printer, None)
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "render_inventory_sheet", record_render)
    monkeypatch.setattr(main_window_module, "PrintPreviewDialog", FakePreviewDialog)
    return calls


@pytest.mark.parametrize(
    "order",
    [Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder],
)
def test_print_inventory_sheet_rows_follow_sort_order(
    window_with_items, monkeypatch, order: Qt.SortOrder
) -> None:
    window, first, second = window_with_items
    window.inactive_checkbox.setChecked(True)
    calls = _capture_inventory_sheet_prints(window, monkeypatch)

    window.sort_model.sort(1, order)
    window.print_inventory_sheet_action.trigger()

    expected_ids = [second.id, first.id]
    if order == Qt.SortOrder.DescendingOrder:
        expected_ids.reverse()
    assert _display_item_ids(window) == expected_ids
    assert [call[0] for call in calls] == [expected_ids]


def test_print_inventory_sheet_prints_only_filtered_rows_with_summary(
    window_with_items, monkeypatch
) -> None:
    window, first, second = window_with_items
    calls = _capture_inventory_sheet_prints(window, monkeypatch)

    window.print_inventory_sheet_action.trigger()
    window.inactive_checkbox.setChecked(True)
    window.search_edit.setText("備品")
    window.search_timer.stop()
    window.refresh_items()
    window.print_inventory_sheet_action.trigger()

    assert [call[0] for call in calls] == [[first.id], [second.id]]
    assert [call[1] for call in calls] == ["(絞り込みなし)", "検索: 備品 / 廃止品目を含む"]


def test_print_inventory_sheet_is_available_from_file_menu_and_toolbar(
    window_with_items, monkeypatch
) -> None:
    window, first, _second = window_with_items
    calls = _capture_inventory_sheet_prints(window, monkeypatch)
    file_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "ファイル")
    print_menu = next(a.menu() for a in file_menu.actions() if a.text() == "印刷")

    next(a for a in print_menu.actions() if a.text() == "在庫表").trigger()
    next(a for a in window.print_toolbar_menu.actions() if a.text() == "在庫表").trigger()

    assert [call[0] for call in calls] == [[first.id], [first.id]]


def test_print_inventory_sheet_during_pending_search_uses_last_fetched_state(
    window_with_items, monkeypatch
) -> None:
    window, _first, _second = window_with_items
    window.inactive_checkbox.setChecked(True)
    expected_ids = _display_item_ids(window)
    assert len(expected_ids) == 2
    calls = _capture_inventory_sheet_prints(window, monkeypatch)
    list_calls: list[object] = []
    original_list_items = window.context.inventory.list_items

    def count_list_items(item_filter):
        list_calls.append(item_filter)
        return original_list_items(item_filter)

    monkeypatch.setattr(window.context.inventory, "list_items", count_list_items)

    window.search_edit.setText("鉛筆")
    assert window.search_timer.isActive()
    window.print_inventory_sheet_action.trigger()

    assert calls[0][0] == expected_ids
    assert calls[0][1] == "廃止品目を含む"
    assert list_calls == []
    assert window.search_timer.isActive()
    window.search_timer.stop()


def test_print_labels_uses_display_order_and_fixed_settings_snapshot(
    window_with_items, monkeypatch, tmp_path: Path
) -> None:
    window, first, second = window_with_items
    window.inactive_checkbox.setChecked(True)
    window.sort_model.sort(1, Qt.SortOrder.DescendingOrder)
    _select_items(window, [second.id, first.id])
    expected_ids = window._selected_item_ids()
    assert expected_ids != [second.id, first.id]
    names_by_id = {
        window.item_model.row_at(row).id: window.item_model.row_at(row).name
        for row in range(window.item_model.rowCount())
    }
    expected_names = [names_by_id[item_id] for item_id in expected_ids]

    settings_path = tmp_path / "ui_settings.json"
    window.context.paths = replace(window.context.paths, ui_settings_path=settings_path)
    loaded_settings = DisplaySettings(label_offset_left_mm=1.2, label_offset_top_mm=-0.4)
    settings_reads: list[Path] = []

    def load_settings(path: Path) -> DisplaySettings:
        settings_reads.append(path)
        return loaded_settings

    monkeypatch.setattr(main_window_module, "load_display_settings", load_settings)

    label_dialog_items: list[int] = []

    class AcceptedLabelDialog:
        def __init__(self, items, parent) -> None:
            assert parent is window
            label_dialog_items.extend(item.id for item in items)
            self.entries = [LabelEntry(item.id, 1) for item in items]
            self.first_page_slots = [0]

        def exec(self) -> int:
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "LabelPrintDialog", AcceptedLabelDialog)

    render_calls: list[
        tuple[object, list[LabelPlacement], dict[int, ItemRow], float, float, object]
    ] = []

    def record_render(
        device, placements, items_by_id, _spec, offset_left, offset_top, *, page_numbers
    ) -> None:
        render_calls.append(
            (device, placements, items_by_id, offset_left, offset_top, page_numbers)
        )

    monkeypatch.setattr(main_window_module, "render_labels", record_render)
    previews: list[object] = []

    class FakePreviewDialog:
        def __init__(self, printer, orientation, total_pages, render_callback, parent) -> None:
            assert parent is window
            assert orientation == main_window_module.QPageLayout.Orientation.Portrait
            assert total_pages == 2
            self.printer = printer
            self.render_callback = render_callback
            previews.append(self)

        def exec(self) -> int:
            self.render_callback(self.printer, None)
            changed_rows = [
                replace(window.item_model.row_at(row), name="プレビュー中の変更")
                for row in range(window.item_model.rowCount())
            ]
            window.item_model.set_rows(changed_rows)
            self.render_callback(self.printer, [2])
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(main_window_module, "PrintPreviewDialog", FakePreviewDialog)

    window.print_labels_action.trigger()

    assert settings_reads == [settings_path]
    assert label_dialog_items == expected_ids
    assert len(previews) == 1
    assert len(render_calls) == 2
    assert [call[5] for call in render_calls] == [None, [2]]
    assert all(call[3:5] == (1.2, -0.4) for call in render_calls)
    assert all(
        [placement.item_id for placement in call[1]] == expected_ids for call in render_calls
    )
    assert all(
        [call[2][item_id].name for item_id in expected_ids] == expected_names
        for call in render_calls
    )


def test_purchase_page_action_requires_selected_item_with_url(window_with_items) -> None:
    window, first, second = window_with_items
    assert not window.purchase_page_action.isEnabled()

    _set_model_purchase_url(window, first.id, "https://example.com/item")
    _select_item(window, first.id)
    assert window.purchase_page_action.isEnabled()

    window.inactive_checkbox.setChecked(True)
    _select_item(window, second.id)
    assert not window.purchase_page_action.isEnabled()
    _set_model_purchase_url(window, second.id, "https://example.com/inactive")
    _select_item(window, second.id)
    assert window.purchase_page_action.isEnabled()


@pytest.mark.parametrize("url", ["http://example.com/item", "https://example.com/item"])
def test_purchase_page_action_opens_valid_http_url(
    window_with_items, monkeypatch, url: str
) -> None:
    window, first, _second = window_with_items
    _set_model_purchase_url(window, first.id, url)
    _select_item(window, first.id)
    opened_urls = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda value: opened_urls.append(value.toString()) or True,
    )

    window.purchase_page_action.trigger()

    assert opened_urls == [url]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https:///missing-host"])
def test_purchase_page_action_rejects_invalid_url(window_with_items, monkeypatch, url: str) -> None:
    window, first, _second = window_with_items
    _set_model_purchase_url(window, first.id, url)
    _select_item(window, first.id)
    opened_urls = []
    warnings = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda value: opened_urls.append(value))
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    window.purchase_page_action.trigger()

    assert opened_urls == []
    assert warnings
    assert "http または https" in warnings[0]


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


def _alert_ids(window: MainWindow) -> list[int]:
    return [
        window.alert_panel.model.row_at(row).id
        for row in range(window.alert_panel.model.rowCount())
    ]


def _is_colored(window: MainWindow, item_id: int) -> bool:
    row = window.item_model.row_of(item_id)
    assert row is not None
    return window.item_model.index(row, 0).data(Qt.ItemDataRole.BackgroundRole) is not None


def _count_list_calls(window: MainWindow, monkeypatch) -> dict[str, int]:
    calls = {"items": 0, "low_stock": 0}
    inventory = window.context.inventory
    original_items = inventory.list_items
    original_low_stock = inventory.list_low_stock

    def list_items(*args, **kwargs):
        calls["items"] += 1
        return original_items(*args, **kwargs)

    def list_low_stock(*args, **kwargs):
        calls["low_stock"] += 1
        return original_low_stock(*args, **kwargs)

    monkeypatch.setattr(inventory, "list_items", list_items)
    monkeypatch.setattr(inventory, "list_low_stock", list_low_stock)
    return calls


def test_alert_panel_follows_stock_moves_reversal_and_active_state(
    window_with_items, monkeypatch
) -> None:
    window, first, _second = window_with_items
    inventory = window.context.inventory
    assert _alert_ids(window) == []
    assert window.alert_panel.windowTitle() == "低在庫 (0)"

    movement = inventory.issue(first.id, 1, 1, "備品")
    window.context.data_bus.data_changed.emit()
    assert _alert_ids(window) == [first.id]
    assert _is_colored(window, first.id)
    assert window.alert_panel.windowTitle() == "低在庫 (1)"

    inventory.reverse(movement.id, staff_id=1)
    window.context.data_bus.data_changed.emit()
    assert _alert_ids(window) == []
    assert not _is_colored(window, first.id)

    inventory.issue(first.id, 1, 1, "備品")
    window.context.data_bus.data_changed.emit()
    inventory.receive(first.id, 1, 1)
    window.context.data_bus.data_changed.emit()
    assert _alert_ids(window) == []

    inventory.issue(first.id, 1, 1, "備品")
    window.context.data_bus.data_changed.emit()
    _select_item(window, first.id)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.toggle_active_action.trigger()
    assert _alert_ids(window) == []
    window.inactive_checkbox.setChecked(True)
    assert not _is_colored(window, first.id)
    _select_item(window, first.id)
    window.toggle_active_action.trigger()
    assert _alert_ids(window) == [first.id]
    assert _is_colored(window, first.id)


def test_item_dialog_edit_threshold_updates_dock_with_one_reload_each(
    window_with_items, monkeypatch
) -> None:
    window, first, _second = window_with_items
    from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog

    def edit_and_save(dialog: ItemDialog) -> int:
        dialog.threshold_spin.setValue(5)
        dialog._save()
        return dialog.result()

    monkeypatch.setattr(ItemDialog, "exec", edit_and_save)
    _select_item(window, first.id)
    calls = _count_list_calls(window, monkeypatch)

    window.edit_action.trigger()

    assert calls == {"items": 1, "low_stock": 1}
    assert _alert_ids(window) == [first.id]
    assert _is_colored(window, first.id)


def test_item_dialog_create_reloads_once_and_cancel_does_not_reload(
    window_with_items, monkeypatch
) -> None:
    window, first, _second = window_with_items
    from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog

    def create_and_save(dialog: ItemDialog) -> int:
        dialog.client_combo.setCurrentIndex(0)
        dialog.name_edit.setText("新規品目")
        dialog.category_picker.set_current_category_id(1)
        dialog.purchaser_combo.setCurrentIndex(0)
        dialog._save()
        return dialog.result()

    calls = _count_list_calls(window, monkeypatch)
    monkeypatch.setattr(ItemDialog, "exec", create_and_save)
    window.new_action.trigger()
    assert calls == {"items": 1, "low_stock": 1}
    assert len(_alert_ids(window)) == 1

    calls["items"] = calls["low_stock"] = 0
    monkeypatch.setattr(ItemDialog, "exec", lambda dialog: dialog.reject() or dialog.result())
    _select_item(window, first.id)
    window.edit_action.trigger()
    window.new_action.trigger()
    assert calls == {"items": 0, "low_stock": 0}


def test_search_and_filters_do_not_reload_dock(qtbot, window_with_items, monkeypatch) -> None:
    window, first, _second = window_with_items
    window.context.inventory.issue(first.id, 1, 1, "備品")
    window.context.data_bus.data_changed.emit()
    calls = _count_list_calls(window, monkeypatch)

    window.search_edit.setText("該当なし")
    qtbot.waitUntil(lambda: calls["items"] >= 1, timeout=1500)
    window.low_stock_checkbox.setChecked(True)
    window.inactive_checkbox.setChecked(True)

    assert calls["low_stock"] == 0
    assert _alert_ids(window) == [first.id]


def test_dock_double_click_clears_filters_and_selects_item(
    qtbot, window_with_items, monkeypatch
) -> None:
    window, first, second = window_with_items
    window.context.inventory.issue(first.id, 1, 1, "備品")
    window.context.data_bus.data_changed.emit()
    window.inactive_checkbox.setChecked(True)
    window.low_stock_checkbox.setChecked(True)
    window.client_combo.setCurrentIndex(window.client_combo.findData(2))
    window.location_combo.setCurrentIndex(window.location_combo.findData(2))
    window.category_picker.set_current_category_id(1)
    window.category_picker.category_changed.emit(1)
    window.search_edit.setText("該当なし")
    assert window.search_timer.isActive()
    assert _item_ids(window) == set()
    calls = _count_list_calls(window, monkeypatch)

    window.alert_panel.table.doubleClicked.emit(window.alert_panel.model.index(0, 0))
    qtbot.wait(window.search_timer.interval() + 150)

    assert not window.search_timer.isActive()
    assert calls == {"items": 1, "low_stock": 0}
    assert window.search_edit.text() == ""
    assert window.client_combo.currentIndex() == 0
    assert window.purchaser_combo.currentIndex() == 0
    assert window.location_combo.currentIndex() == 0
    assert window.category_picker.current_category_id() is None
    assert not window.low_stock_checkbox.isChecked()
    assert not window.inactive_checkbox.isChecked()
    assert not window.include_inactive_action.isChecked()
    assert _item_ids(window) == {first.id}
    assert window._selected_item_id() == first.id
    assert second.id not in _item_ids(window)


def test_alert_panel_can_be_closed_and_reopened_from_view_menu(window_with_items) -> None:
    window, _first, _second = window_with_items
    window.show()
    window.context.inventory.issue(_first.id, 1, 1, "備品")
    window.context.data_bus.data_changed.emit()
    view_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "表示")
    toggle = window.alert_panel.toggleViewAction()
    assert toggle in view_menu.actions()
    assert toggle.text() == "アラートパネル"
    assert window.alert_panel.isVisible()

    toggle.trigger()
    assert not window.alert_panel.isVisible()
    toggle.trigger()
    assert window.alert_panel.isVisible()


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_window_with_dock_fits_1366x768_work_area(qtbot, window_with_items, scale: float) -> None:
    window, _first, _second = window_with_items
    available = QRect(0, 0, 1366, 768)
    original_font = window.font()
    try:
        font = QFont(original_font)
        font.setPointSizeF(original_font.pointSizeF() * scale)
        window.setFont(font)
        window.show()
        qtbot.waitExposed(window)
        frame_width = window.frameGeometry().width() - window.width()
        frame_height = window.frameGeometry().height() - window.height()
        hint = window.minimumSizeHint()
        assert hint.width() + frame_width <= available.width()
        assert hint.height() + frame_height <= available.height()
        window.resize(
            min(1000, available.width() - frame_width), min(650, available.height() - frame_height)
        )
        assert window.alert_panel.isVisible()
        table = window.alert_panel.table
        assert table.horizontalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert table.horizontalHeader().length() > 0
    finally:
        window.setFont(original_font)


def _patch_save_dialog(monkeypatch, selected: Path | None) -> list[str]:
    default_names: list[str] = []

    def fake_get_save_file_name(_parent, _caption, default_name, _filter):
        default_names.append(default_name)
        return (str(selected) if selected is not None else "", "")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", fake_get_save_file_name)
    return default_names


def _patch_history_export_dialog(
    monkeypatch,
    *,
    accept: bool = True,
    client_id: int | None = None,
    kind: HistoryPeriodKind = HistoryPeriodKind.ALL,
    fiscal_year: int | None = None,
    month: int | None = None,
) -> list[HistoryExportDialog]:
    dialogs: list[HistoryExportDialog] = []

    def fake_exec(dialog: HistoryExportDialog) -> int:
        dialogs.append(dialog)
        if not accept:
            return QDialog.DialogCode.Rejected
        selector = dialog.period_selector
        if fiscal_year is not None:
            selector.year_combo.setCurrentIndex(selector.year_combo.findData(fiscal_year))
        if month is not None:
            selector.month_combo.setCurrentIndex(selector.month_combo.findData(month))
        selector.period_kind_combo.setCurrentIndex(selector.period_kind_combo.findData(kind))
        dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(client_id))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(HistoryExportDialog, "exec", fake_exec)
    return dialogs


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def _forbid_overwrite_question(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("追加の上書き確認は表示されないはずです")

    monkeypatch.setattr(QMessageBox, "question", fail)


def _apply_filters(window: MainWindow, filters: dict[str, Any]) -> None:
    window.search_edit.setText(str(filters.get("search", "")))
    window.client_combo.setCurrentIndex(window.client_combo.findData(filters.get("client")))
    window.purchaser_combo.setCurrentIndex(
        window.purchaser_combo.findData(filters.get("purchaser"))
    )
    window.location_combo.setCurrentIndex(window.location_combo.findData(filters.get("location")))
    category = filters.get("category")
    window.category_picker.set_current_category_id(category)
    window.category_picker.category_changed.emit(category)
    window.low_stock_checkbox.setChecked(bool(filters.get("low_only")))
    window.inactive_checkbox.setChecked(bool(filters.get("inactive")))
    window.search_timer.stop()
    window.refresh_items()


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, {"A", "C"}),
        ({"inactive": True}, {"A", "B", "C"}),
        ({"search": "鉛"}, {"A"}),
        ({"client": 2, "inactive": True}, {"B"}),
        ({"purchaser": 1}, {"A", "C"}),
        ({"category": 1}, {"A", "C"}),
        ({"category": 1, "inactive": True}, {"A", "B", "C"}),
        ({"category": 2}, {"A", "C"}),
        ({"location": 2, "inactive": True}, {"B"}),
        ({"low_only": True}, {"C"}),
        ({"inactive": True, "client": 1, "location": 1}, {"A"}),
        ({"category": 1, "low_only": True}, {"C"}),
    ],
)
def test_items_csv_reflects_current_filters(
    window_with_items, monkeypatch, tmp_path: Path, filters: dict[str, Any], expected: set[str]
) -> None:
    window, first, second = window_with_items
    low_item = window.context.inventory.create_item(
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
    codes = {"A": first.code, "B": second.code, "C": low_item.code}
    _apply_filters(window, filters)
    listed_codes = {
        window.item_model.row_at(row).code for row in range(window.item_model.rowCount())
    }
    assert listed_codes == {codes[key] for key in expected}

    destination = tmp_path / "items.csv"
    _patch_save_dialog(monkeypatch, destination)
    window.export_items_action.trigger()

    rows = _read_csv(destination)
    assert rows[0][0] == "管理番号"
    assert {row[0] for row in rows[1:]} == listed_codes
    assert len(rows) - 1 == len(listed_codes)


def test_csv_export_default_names_use_label_and_local_timestamp(
    window_with_items, monkeypatch
) -> None:
    window, _first, _second = window_with_items
    default_names = _patch_save_dialog(monkeypatch, None)
    _patch_history_export_dialog(monkeypatch)

    window.export_items_action.trigger()
    window.export_history_action.trigger()

    assert default_names[0].startswith("在庫一覧_")
    assert default_names[1].startswith("履歴_")
    assert all(name.endswith(".csv") for name in default_names)
    assert all(len(name.split("_", 1)[1]) == len("YYYYMMDD_HHMMSS.csv") for name in default_names)


def test_history_csv_exports_all_items(window_with_items, monkeypatch, tmp_path: Path) -> None:
    window, first, _second = window_with_items
    window.context.inventory.receive(first.id, 1, 3)
    destination = tmp_path / "history.csv"
    _patch_save_dialog(monkeypatch, destination)
    _patch_history_export_dialog(monkeypatch)

    window.export_history_action.trigger()

    rows = _read_csv(destination)
    assert rows[0][0] == "日時"
    assert len(rows) - 1 == len(window.context.inventory.list_all_history()) == 3
    assert str(destination) in window.statusBar().currentMessage()


def test_history_export_dialog_lists_all_and_inactive_clients(
    window_with_items, monkeypatch
) -> None:
    window, _first, _second = window_with_items
    window.context.master.deactivate_client(2)
    dialogs = _patch_history_export_dialog(monkeypatch, accept=False)

    window.export_history_action.trigger()

    combo = dialogs[0].client_combo
    labels = [combo.itemText(index) for index in range(combo.count())]
    assert sorted(labels[1:]) == sorted(["総務", "経理 (無効)", "廃止クライアント (無効)"])
    assert labels[0] == "すべて"
    assert dialogs[0].selected_client_id() is None
    assert dialogs[0].selected_period() == HistoryPeriod()


@pytest.mark.parametrize("deactivate", [False, True])
def test_history_csv_filters_by_client(
    window_with_items, monkeypatch, tmp_path: Path, deactivate: bool
) -> None:
    window, _first, _second = window_with_items
    window.context.inventory.receive(_first.id, 1, 3)
    if deactivate:
        window.context.master.deactivate_client(2)
    destination = tmp_path / "history.csv"
    _patch_save_dialog(monkeypatch, destination)
    _patch_history_export_dialog(monkeypatch, client_id=2)

    window.export_history_action.trigger()

    rows = _read_csv(destination)
    assert len(rows) - 1 == 1
    assert {row[7] for row in rows[1:]} == {"経理"}


@pytest.mark.parametrize(
    ("kind", "fiscal_year", "month", "expected"),
    [
        (HistoryPeriodKind.ALL, None, None, 3),
        (HistoryPeriodKind.ANNUAL, 2025, None, 3),
        (HistoryPeriodKind.ANNUAL, 2026, None, 0),
        (HistoryPeriodKind.MONTHLY, 2025, 1, 3),
        (HistoryPeriodKind.MONTHLY, 2025, 2, 0),
    ],
)
def test_history_csv_filters_by_period(
    window_with_items,
    monkeypatch,
    tmp_path: Path,
    kind: HistoryPeriodKind,
    fiscal_year: int | None,
    month: int | None,
    expected: int,
) -> None:
    window, first, _second = window_with_items
    window.context.inventory.receive(first.id, 1, 3)
    destination = tmp_path / "history.csv"
    _patch_save_dialog(monkeypatch, destination)
    _patch_history_export_dialog(monkeypatch, kind=kind, fiscal_year=fiscal_year, month=month)

    window.export_history_action.trigger()

    assert len(_read_csv(destination)) - 1 == expected


def test_csv_export_cancel_creates_no_file(window_with_items, monkeypatch, tmp_path: Path) -> None:
    window, _first, _second = window_with_items
    _patch_save_dialog(monkeypatch, None)
    _patch_history_export_dialog(monkeypatch)

    window.export_items_action.trigger()
    window.export_history_action.trigger()

    assert list(tmp_path.iterdir()) == []


def test_history_export_dialog_cancel_skips_save_dialog(
    window_with_items, monkeypatch, tmp_path: Path
) -> None:
    window, _first, _second = window_with_items
    default_names = _patch_save_dialog(monkeypatch, tmp_path / "history.csv")
    _patch_history_export_dialog(monkeypatch, accept=False)
    message_before = window.statusBar().currentMessage()

    window.export_history_action.trigger()

    assert default_names == []
    assert list(tmp_path.iterdir()) == []
    assert window.statusBar().currentMessage() == message_before


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
def test_csv_export_confirms_overwrite_only_after_extension_completion(
    window_with_items, monkeypatch, tmp_path: Path, answer
) -> None:
    window, _first, _second = window_with_items
    existing = tmp_path / "out.csv"
    existing.write_bytes(b"old")
    _patch_save_dialog(monkeypatch, tmp_path / "out")
    questions = []

    def fake_question(_parent, _title, _text, _buttons, default):
        questions.append(default)
        return answer

    monkeypatch.setattr(QMessageBox, "question", fake_question)

    window.export_items_action.trigger()

    assert questions == [QMessageBox.StandardButton.No]
    if answer == QMessageBox.StandardButton.Yes:
        assert _read_csv(existing)[0][0] == "管理番号"
    else:
        assert existing.read_bytes() == b"old"


def test_csv_export_completes_extension_without_extra_confirmation(
    window_with_items, monkeypatch, tmp_path: Path
) -> None:
    window, _first, _second = window_with_items
    _forbid_overwrite_question(monkeypatch)
    _patch_save_dialog(monkeypatch, tmp_path / "out")

    window.export_items_action.trigger()

    assert _read_csv(tmp_path / "out.csv")[0][0] == "管理番号"
    assert not (tmp_path / "out").exists()


def test_csv_export_does_not_confirm_regular_csv_path_again(
    window_with_items, monkeypatch, tmp_path: Path
) -> None:
    window, _first, _second = window_with_items
    existing = tmp_path / "out.csv"
    existing.write_bytes(b"old")
    _forbid_overwrite_question(monkeypatch)
    _patch_save_dialog(monkeypatch, existing)

    window.export_items_action.trigger()

    assert _read_csv(existing)[0][0] == "管理番号"


def test_png_export_path_completes_extension_without_extra_confirmation(
    qtbot, monkeypatch, tmp_path: Path
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    _forbid_overwrite_question(monkeypatch)
    _patch_save_dialog(monkeypatch, tmp_path / "dashboard")

    path = _choose_export_path(
        parent, "ダッシュボード", "ダッシュボード", ".png", "PNG ファイル (*.png)"
    )

    assert path == tmp_path / "dashboard.png"


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
def test_png_export_path_confirms_overwrite_after_extension_completion(
    qtbot, monkeypatch, tmp_path: Path, answer
) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    existing = tmp_path / "dashboard.png"
    existing.write_bytes(b"old")
    _patch_save_dialog(monkeypatch, tmp_path / "dashboard")
    questions = []

    def fake_question(_parent, _title, _text, _buttons, default):
        questions.append(default)
        return answer

    monkeypatch.setattr(QMessageBox, "question", fake_question)

    path = _choose_export_path(
        parent, "ダッシュボード", "ダッシュボード", "png", "PNG ファイル (*.png)"
    )

    assert questions == [QMessageBox.StandardButton.No]
    assert path == (existing if answer == QMessageBox.StandardButton.Yes else None)
    if answer == QMessageBox.StandardButton.No:
        assert existing.read_bytes() == b"old"


def test_csv_export_failure_shows_error_and_keeps_app_running(
    window_with_items, monkeypatch, tmp_path: Path
) -> None:
    window, first, _second = window_with_items
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, message: warnings.append(message)
    )
    destination = tmp_path / "missing" / "out.csv"
    _patch_save_dialog(monkeypatch, destination)

    window.export_items_action.trigger()

    assert len(warnings) == 1
    assert str(destination) in warnings[0]
    assert not destination.exists()
    _select_item(window, first.id)
    assert window.edit_action.isEnabled()


def test_csv_export_logs_start_success_and_failure(
    window_with_items, monkeypatch, tmp_path: Path, caplog
) -> None:
    window, _first, _second = window_with_items
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: None)
    caplog.set_level(logging.INFO, logger="inventory_manager_mini.ui.csv_export")
    destination = tmp_path / "items.csv"
    _patch_save_dialog(monkeypatch, destination)

    window.export_items_action.trigger()

    records = [r for r in caplog.records if r.name == "inventory_manager_mini.ui.csv_export"]
    assert [r.levelno for r in records] == [logging.INFO, logging.INFO]
    assert "開始" in records[0].getMessage()
    assert "完了" in records[1].getMessage()
    assert str(destination) in records[1].getMessage()

    caplog.clear()
    broken = tmp_path / "missing" / "items.csv"
    _patch_save_dialog(monkeypatch, broken)

    window.export_items_action.trigger()

    records = [r for r in caplog.records if r.name == "inventory_manager_mini.ui.csv_export"]
    assert [r.levelno for r in records] == [logging.INFO, logging.ERROR]
    assert str(broken) in records[1].getMessage()
    assert records[1].exc_info is not None


@pytest.fixture
def backup_window(qtbot, tmp_path: Path):
    db_path = tmp_path / "app.db"
    conn = connect(db_path)
    create_schema(conn)
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("INSERT INTO clients (id, name) VALUES (1, 'クライアント')")
    conn.execute("INSERT INTO purchasers (id, name) VALUES (1, '発注主体')")
    conn.execute("INSERT INTO staff (id, name) VALUES (1, '担当者')")
    conn.execute("INSERT INTO categories (id, name, code_prefix) VALUES (1, '備品', 'EQ')")
    InventoryService(conn).create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="WAL内の品目",
            category_id=1,
            initial_quantity=1,
            initial_staff_id=1,
        )
    )
    window = MainWindow(create_test_context(conn, db_path=db_path))
    qtbot.addWidget(window)
    yield window
    conn.close()


def _patch_backup_dialog(monkeypatch, selected: Path | None) -> list[str]:
    start_dirs: list[str] = []

    def fake_get_existing_directory(_parent, _caption, start_dir):
        start_dirs.append(start_dir)
        return str(selected) if selected is not None else ""

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", fake_get_existing_directory)
    return start_dirs


def _capture_message_boxes(monkeypatch) -> dict[str, list[str]]:
    messages: dict[str, list[str]] = {"information": [], "warning": []}
    monkeypatch.setattr(
        QMessageBox, "information", lambda _p, _t, text: messages["information"].append(text)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _p, _t, text: messages["warning"].append(text)
    )
    return messages


def test_backup_creates_file_with_committed_wal_data_and_shows_path(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    dest_dir = tmp_path / "dest"
    start_dirs = _patch_backup_dialog(monkeypatch, dest_dir)
    messages = _capture_message_boxes(monkeypatch)
    assert (tmp_path / "app.db-wal").stat().st_size > 0

    window.backup_action.trigger()

    created = list(dest_dir.glob("inventory_*.db"))
    assert len(created) == 1
    assert start_dirs == [str(window.context.paths.backup_dir)]
    assert messages["information"] == [f"バックアップを作成しました: {created[0]}"]
    assert str(created[0]) in window.statusBar().currentMessage()
    backup_conn = connect_readonly(created[0])
    try:
        assert backup_conn.execute("SELECT name FROM items").fetchall() == [("WAL内の品目",)]
    finally:
        backup_conn.close()


def test_backup_cancel_creates_nothing(backup_window, monkeypatch, tmp_path: Path) -> None:
    messages = _capture_message_boxes(monkeypatch)
    _patch_backup_dialog(monkeypatch, None)

    backup_window.backup_action.trigger()

    assert messages == {"information": [], "warning": []}
    assert not (tmp_path / "backups").exists()


def test_backup_to_unwritable_destination_shows_path_and_cause_and_keeps_running(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    _patch_backup_dialog(monkeypatch, dest_dir)
    messages = _capture_message_boxes(monkeypatch)

    def reject_copy(*_args, **_kwargs) -> None:
        raise PermissionError("injected copy failure")

    monkeypatch.setattr(services, "copy_database", reject_copy)

    window.backup_action.trigger()

    assert messages["information"] == []
    assert len(messages["warning"]) == 1
    assert str(dest_dir) in messages["warning"][0]
    assert "injected copy failure" in messages["warning"][0]
    assert window.statusBar().currentMessage() == ""
    assert window.backup_action.isEnabled()
    assert window.isEnabled()


def test_backup_rejects_existing_file_and_keeps_running(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    dest_dir = tmp_path / "dest"
    _patch_backup_dialog(monkeypatch, dest_dir)
    messages = _capture_message_boxes(monkeypatch)
    window.context.backup.clock = lambda: datetime(2026, 10, 3, 3, 4, 5, tzinfo=UTC)
    window.context.backup.tz = UTC

    window.backup_action.trigger()
    window.backup_action.trigger()

    assert len(messages["information"]) == 1
    assert len(messages["warning"]) == 1
    assert "すでに存在" in messages["warning"][0]
    assert len(list(dest_dir.glob("inventory_*.db"))) == 1


def test_backup_logs_start_success_and_failure(
    backup_window, monkeypatch, tmp_path: Path, caplog
) -> None:
    window = backup_window
    _capture_message_boxes(monkeypatch)
    logger_name = "inventory_manager_mini.ui.main_window"
    caplog.set_level(logging.INFO, logger=logger_name)
    dest_dir = tmp_path / "dest"
    _patch_backup_dialog(monkeypatch, dest_dir)

    window.backup_action.trigger()

    records = [r for r in caplog.records if r.name == logger_name]
    assert [r.levelno for r in records] == [logging.INFO, logging.INFO]
    assert "開始" in records[0].getMessage()
    assert str(dest_dir) in records[0].getMessage()
    assert "完了" in records[1].getMessage()

    caplog.clear()

    def reject_copy(*_args, **_kwargs) -> None:
        raise PermissionError("injected copy failure")

    monkeypatch.setattr(services, "copy_database", reject_copy)
    window.backup_action.trigger()

    records = [r for r in caplog.records if r.name == logger_name]
    assert [r.levelno for r in records] == [logging.INFO, logging.ERROR]
    assert str(dest_dir) in records[1].getMessage()
    assert records[1].exc_info is not None


def _make_restore_source(path: Path, item_name: str) -> Path:
    conn = connect(path)
    try:
        create_schema(conn)
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'クライアント')")
        conn.execute("INSERT INTO purchasers (id, name) VALUES (1, '発注主体')")
        conn.execute("INSERT INTO staff (id, name) VALUES (1, '担当者')")
        conn.execute("INSERT INTO categories (id, name, code_prefix) VALUES (1, '備品', 'EQ')")
        InventoryService(conn).create_item(
            NewItem(
                client_id=1,
                purchaser_id=1,
                name=item_name,
                category_id=1,
                initial_quantity=2,
                initial_staff_id=1,
            )
        )
    finally:
        conn.close()
    return path


def _patch_restore_dialogs(
    monkeypatch, source: Path | None, answer: QMessageBox.StandardButton
) -> dict[str, list[Any]]:
    record: dict[str, list[Any]] = {
        "questions": [],
        "information": [],
        "warning": [],
        "critical": [],
    }
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(source) if source is not None else "", ""),
    )

    def question(_parent, _title, text, _buttons, default):
        record["questions"].append((text, default))
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            kind,
            lambda _p, _t, text, kind=kind: record[kind].append(text),
        )
    return record


def _item_names(db_path: Path) -> list[str]:
    conn = connect_readonly(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT name FROM items ORDER BY id")]
    finally:
        conn.close()


def _record_prepared_restores(monkeypatch, window: MainWindow) -> list[Any]:
    prepared_list: list[Any] = []
    original = window.context.backup.prepare_restore

    def recording_prepare(source_path):
        prepared = original(source_path)
        prepared_list.append(prepared)
        return prepared

    monkeypatch.setattr(window.context.backup, "prepare_restore", recording_prepare)
    return prepared_list


def test_restore_replaces_database_and_requests_restart(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    source_bytes = source.read_bytes()
    record = _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    prepared_list = _record_prepared_restores(monkeypatch, window)

    window.restore_action.trigger()

    assert _item_names(tmp_path / "app.db") == ["復元元の品目"]
    assert source.read_bytes() == source_bytes
    assert len(list((tmp_path / "backups").glob("pre-restore_*.db"))) == 1
    assert window.exit_request is ExitRequest.RESTART
    assert window.isEnabled()
    text, default = record["questions"][0]
    assert default == QMessageBox.StandardButton.No
    for expected in (str(source), "品目数: 1 件", "履歴数: 1 件", "再起動", "自動バックアップ"):
        assert expected in text
    assert "再起動します" in record["information"][0]
    assert not prepared_list[0].path.parent.exists()


def test_restore_declined_keeps_database_and_cleans_temporary_files(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.No)
    prepared_list = _record_prepared_restores(monkeypatch, window)

    window.restore_action.trigger()

    assert _item_names(tmp_path / "app.db") == ["WAL内の品目"]
    assert window.exit_request is ExitRequest.NORMAL
    assert not (tmp_path / "backups").exists()
    assert not prepared_list[0].path.parent.exists()


def test_restore_cancel_in_file_dialog_does_nothing(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    record = _patch_restore_dialogs(monkeypatch, None, QMessageBox.StandardButton.Yes)

    backup_window.restore_action.trigger()

    assert record["questions"] == []
    assert _item_names(tmp_path / "app.db") == ["WAL内の品目"]


def test_restore_rejects_invalid_file_with_limited_reasons(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    invalid = tmp_path / "invalid.db"
    invalid.write_text("not a database", encoding="utf-8")
    record = _patch_restore_dialogs(monkeypatch, invalid, QMessageBox.StandardButton.Yes)

    window.restore_action.trigger()

    assert record["questions"] == []
    assert len(record["warning"]) == 1
    assert "復元を中止" in record["warning"][0]
    assert _item_names(tmp_path / "app.db") == ["WAL内の品目"]
    assert window.isEnabled()
    assert window.exit_request is ExitRequest.NORMAL


def test_restore_lists_at_most_20_rejection_reasons(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    reasons = tuple(f"理由{index}" for index in range(25))

    def reject(_source_path):
        raise InvalidBackupError(reasons)

    monkeypatch.setattr(backup_window.context.backup, "prepare_restore", reject)
    record = _patch_restore_dialogs(monkeypatch, tmp_path / "x.db", QMessageBox.StandardButton.Yes)

    backup_window.restore_action.trigger()

    message = record["warning"][0]
    assert "理由19" in message
    assert "理由20" not in message
    assert "他 5 件" in message


def test_restore_disables_window_while_overwriting(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    original = window.context.backup.apply_restore
    states: list[bool] = []

    def observing_apply(prepared, db_path, backup_dir):
        states.append(window.isEnabled())
        return original(prepared, db_path, backup_dir)

    monkeypatch.setattr(window.context.backup, "apply_restore", observing_apply)

    window.restore_action.trigger()

    assert states == [False]


@pytest.mark.parametrize(
    ("stage", "recovered", "enabled", "exit_request", "message_kind"),
    [
        ("pre_backup", True, True, ExitRequest.NORMAL, "warning"),
        ("overwrite", True, True, ExitRequest.NORMAL, "warning"),
        ("recovery", False, False, ExitRequest.ABNORMAL, "critical"),
    ],
)
def test_restore_failure_stages(
    backup_window,
    monkeypatch,
    tmp_path: Path,
    stage: str,
    recovered: bool,
    enabled: bool,
    exit_request: ExitRequest,
    message_kind: str,
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    record = _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    auto_backup = tmp_path / "backups" / "pre-restore_test.db"

    def failing_apply(_prepared, _db_path, _backup_dir):
        raise RestoreError(
            "injected restore failure",
            stage=stage,  # type: ignore[arg-type]
            recovered=recovered,
            backup_path=auto_backup,
        )

    monkeypatch.setattr(window.context.backup, "apply_restore", failing_apply)
    refresh_calls: list[bool] = []
    monkeypatch.setattr(window, "refresh", lambda: refresh_calls.append(True))

    window.restore_action.trigger()

    assert window.isEnabled() is enabled
    assert window.exit_request is exit_request
    assert record["information"] == []
    message = record[message_kind][0]
    assert "injected restore failure" in message
    if stage == "pre_backup":
        assert str(window.context.paths.backup_dir) in message
    else:
        assert str(auto_backup) in message
    if stage == "recovery":
        assert str(window.context.paths.log_path) in message
    assert refresh_calls == ([True] if stage == "overwrite" else [])


def test_restore_logs_each_stage(backup_window, monkeypatch, tmp_path: Path, caplog) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    logger_name = "inventory_manager_mini.ui.main_window"
    caplog.set_level(logging.INFO, logger=logger_name)

    window.restore_action.trigger()

    records = [r for r in caplog.records if r.name == logger_name]
    assert [r.levelno for r in records] == [logging.INFO] * 4
    messages = [r.getMessage() for r in records]
    assert "検査・準備を開始" in messages[0]
    assert "検査・準備が完了" in messages[1]
    assert "上書きを開始" in messages[2]
    assert "復元が完了" in messages[3]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _table_rows(db_path: Path) -> dict[str, list[tuple[Any, ...]]]:
    conn = connect_readonly(db_path)
    try:
        tables = ("items", "stock_movements", "stock_allocations", "settings", "total_aggregates")
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
            for table in tables
        }
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = connect_readonly(db_path)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _isolate_temp_dir(monkeypatch, tmp_path: Path) -> Path:
    temp_root = tmp_path / "restore-tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return temp_root


def _make_invalid_restore_source(path: Path, case: str) -> Path:
    _make_restore_source(path, "不正な復元元")
    if case == "version_zero":
        conn = sqlite3.connect(path, autocommit=True)
        conn.execute("PRAGMA user_version = 0")
        conn.close()
    elif case == "too_new":
        conn = sqlite3.connect(path, autocommit=True)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
    elif case == "schema_mismatch":
        conn = connect(path)
        conn.execute("DROP TRIGGER trg_items_updated_at")
        conn.close()
    elif case == "business_violation":
        conn = connect(path)
        conn.execute("UPDATE items SET quantity = 99 WHERE id = 1")
        conn.close()
    elif case == "corrupt":
        data = bytearray(path.read_bytes())
        data[4096:] = b"\xff" * (len(data) - 4096)
        path.write_bytes(bytes(data))
    return path


def test_restore_result_matches_source_and_keeps_ui_settings_file(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    source_hash = _sha256(source)
    expected = _table_rows(source)
    ui_settings = tmp_path / "ui_settings.json"
    ui_settings.write_text('{"font_scale_percent": 125}', encoding="utf-8")
    ui_settings_hash = _sha256(ui_settings)
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)

    window.restore_action.trigger()

    assert window.exit_request is ExitRequest.RESTART
    assert _table_rows(tmp_path / "app.db") == expected
    assert _user_version(tmp_path / "app.db") == SCHEMA_VERSION
    assert _sha256(source) == source_hash
    assert _sha256(ui_settings) == ui_settings_hash


@pytest.mark.parametrize(
    "case", ["version_zero", "too_new", "schema_mismatch", "corrupt", "business_violation"]
)
def test_restore_rejects_invalid_sources_without_changing_current_database(
    backup_window, monkeypatch, tmp_path: Path, case: str
) -> None:
    window = backup_window
    source = _make_invalid_restore_source(tmp_path / f"{case}.db", case)
    source_hash = _sha256(source)
    before = _table_rows(tmp_path / "app.db")
    temp_root = _isolate_temp_dir(monkeypatch, tmp_path)
    record = _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)

    window.restore_action.trigger()

    assert record["questions"] == []
    assert len(record["warning"]) == 1
    assert "復元を中止" in record["warning"][0]
    assert _table_rows(tmp_path / "app.db") == before
    assert _sha256(source) == source_hash
    assert not (tmp_path / "backups").exists()
    assert list(temp_root.iterdir()) == []
    assert window.isEnabled()
    assert window.exit_request is ExitRequest.NORMAL


def test_restore_migrates_old_version_source_and_shows_migration_notice(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = tmp_path / "source-v3.db"
    conn = connect(source)
    try:
        schema_v3 = files("inventory_manager_mini.db").joinpath("schema_v3.sql")
        create_schema(conn, schema_v3.read_text(encoding="utf-8"), version=3)
        conn.execute("INSERT INTO clients (id, name) VALUES (1, 'クライアント')")
        conn.execute("INSERT INTO purchasers (id, name) VALUES (1, '発注主体')")
        conn.execute("INSERT INTO categories (id, name, code_prefix) VALUES (1, '備品', 'EQ')")
        conn.execute(
            "INSERT INTO items (client_id, purchaser_id, code, name, category_id) "
            "VALUES (1, 1, 'EQ-0001', '旧版の品目', 1)"
        )
    finally:
        conn.close()
    source_hash = _sha256(source)
    record = _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)

    window.restore_action.trigger()

    text, _default = record["questions"][0]
    assert "元の版数: 3(最新版へ移行済み)" in text
    assert window.exit_request is ExitRequest.RESTART
    assert _user_version(tmp_path / "app.db") == SCHEMA_VERSION
    assert _item_names(tmp_path / "app.db") == ["旧版の品目"]
    assert _sha256(source) == source_hash


def _fail_inspection_after_overwrite(monkeypatch, window: MainWindow, failures: int | None) -> None:
    """上書き後の検査を failures 回(None なら常に)失敗させる。"""
    original = window.context.backup._inspect_path
    calls = 0

    def inspect_path(path: Path) -> None:
        nonlocal calls
        calls += 1
        if failures is None or calls <= failures:
            raise InvalidBackupError(("injected inspection failure",))
        original(path)

    monkeypatch.setattr(window.context.backup, "_inspect_path", inspect_path)


@pytest.mark.parametrize(
    ("stage", "enabled", "exit_request", "message_kind"),
    [
        ("pre_backup", True, ExitRequest.NORMAL, "warning"),
        ("overwrite", True, ExitRequest.NORMAL, "warning"),
        ("recovery", False, ExitRequest.ABNORMAL, "critical"),
    ],
)
def test_restore_real_failures_report_stage_and_control_resumption(
    backup_window,
    monkeypatch,
    tmp_path: Path,
    caplog,
    stage: str,
    enabled: bool,
    exit_request: ExitRequest,
    message_kind: str,
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    record = _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    window.context.backup.clock = lambda: datetime(2026, 10, 3, 3, 4, 5, tzinfo=UTC)
    window.context.backup.tz = UTC
    backup_dir = tmp_path / "backups"
    auto_backup = backup_dir / "pre-restore_20261003_030405.db"
    if stage == "pre_backup":
        backup_dir.mkdir()
        auto_backup.write_bytes(b"existing")
    else:
        _fail_inspection_after_overwrite(monkeypatch, window, 1 if stage == "overwrite" else None)
    logger_name = "inventory_manager_mini.ui.main_window"
    caplog.set_level(logging.INFO, logger=logger_name)

    window.restore_action.trigger()

    assert window.isEnabled() is enabled
    assert window.exit_request is exit_request
    assert record["information"] == []
    assert len(record[message_kind]) == 1
    if stage == "pre_backup":
        assert auto_backup.read_bytes() == b"existing"
        assert _item_names(tmp_path / "app.db") == ["WAL内の品目"]
    else:
        assert str(auto_backup) in record[message_kind][0]
    if stage == "overwrite":
        assert _item_names(tmp_path / "app.db") == ["WAL内の品目"]
    errors = [r for r in caplog.records if r.name == logger_name and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert f"段階={stage}" in errors[0].getMessage()
    assert errors[0].exc_info is not None


def test_open_connection_reads_recovered_data_after_failed_overwrite(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    conn = window.context.backup.conn
    assert conn is not None
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    _fail_inspection_after_overwrite(monkeypatch, window, 1)
    # 上書き前に開いたままの接続でページ・WAL を読み込ませておく
    assert [row[0] for row in conn.execute("SELECT name FROM items")] == ["WAL内の品目"]

    window.restore_action.trigger()

    assert [row[0] for row in conn.execute("SELECT name FROM items")] == ["WAL内の品目"]
    assert window.context.backup.inspect_database(conn, SCHEMA_VERSION) == []
    assert {window.item_model.row_at(row).name for row in range(window.item_model.rowCount())} == {
        "WAL内の品目"
    }
    # 復旧後も開いたままの接続で書き込みでき、別接続から読める
    conn.execute("UPDATE items SET name = '更新後' WHERE id = 1")
    assert _item_names(tmp_path / "app.db") == ["更新後"]
    assert window.isEnabled()
    assert window.exit_request is ExitRequest.NORMAL


def test_restore_logs_rejected_source_as_error(
    backup_window, monkeypatch, tmp_path: Path, caplog
) -> None:
    source = _make_invalid_restore_source(tmp_path / "invalid.db", "version_zero")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    logger_name = "inventory_manager_mini.ui.main_window"
    caplog.set_level(logging.INFO, logger=logger_name)

    backup_window.restore_action.trigger()

    records = [r for r in caplog.records if r.name == logger_name]
    assert [r.levelno for r in records] == [logging.INFO, logging.ERROR]
    assert "検査・準備を開始" in records[0].getMessage()
    assert "復元元を利用できません" in records[1].getMessage()
    assert records[1].exc_info is not None


def _open_dashboard(window: MainWindow) -> DashboardWindow:
    window.dashboard_action.trigger()
    dashboard = window._dashboard_window
    assert dashboard is not None
    return dashboard


def _count_dashboard_calls(monkeypatch, window: MainWindow) -> list[int]:
    reports = window.context.reports
    original = reports.dashboard_result
    calls: list[int] = []

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(reports, "dashboard_result", counting)
    return calls


def _count_dashboard_shutdowns(monkeypatch) -> list[int]:
    original = DashboardWindow.shutdown
    calls: list[int] = []

    def counting(self: DashboardWindow) -> None:
        calls.append(1)
        original(self)

    monkeypatch.setattr(DashboardWindow, "shutdown", counting)
    return calls


def test_dashboard_opens_from_view_menu_and_is_reused(window_with_items, monkeypatch) -> None:
    window, _first, _second = window_with_items
    view_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "表示")
    assert window.dashboard_action in view_menu.actions()
    assert window.dashboard_action.text() == "ダッシュボード"
    assert all(
        window.dashboard_action not in toolbar.actions()
        for toolbar in window.findChildren(QToolBar)
    )
    assert window._dashboard_window is None

    first = _open_dashboard(window)
    assert first.isVisible()
    assert first.isWindow()
    raised: list[bool] = []
    monkeypatch.setattr(first, "raise_", lambda: raised.append(True))

    window.dashboard_action.trigger()

    assert window._dashboard_window is first
    assert raised == [True]
    first.hide()
    window.dashboard_action.trigger()
    assert window._dashboard_window is first
    assert first.isVisible()


def test_main_window_close_shuts_down_dashboard(window_with_items, monkeypatch) -> None:
    window, _first, _second = window_with_items
    _open_dashboard(window)
    calls = _count_dashboard_calls(monkeypatch, window)
    shutdowns = _count_dashboard_shutdowns(monkeypatch)
    window.context.data_bus.data_changed.emit()

    window.close()
    QApplication.processEvents()

    assert shutdowns == [1]
    assert window._dashboard_window is None
    assert calls == []


def test_restore_shuts_down_dashboard_before_overwrite_without_double_shutdown(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    _open_dashboard(window)
    calls = _count_dashboard_calls(monkeypatch, window)
    shutdowns = _count_dashboard_shutdowns(monkeypatch)
    original = window.context.backup.apply_restore
    observed: list[DashboardWindow | None] = []

    def observing_apply(prepared, db_path, backup_dir):
        observed.append(window._dashboard_window)
        return original(prepared, db_path, backup_dir)

    monkeypatch.setattr(window.context.backup, "apply_restore", observing_apply)
    window.context.data_bus.data_changed.emit()

    window.restore_action.trigger()
    QApplication.processEvents()

    assert observed == [None]
    assert window.exit_request is ExitRequest.RESTART
    assert shutdowns == [1]
    assert window._dashboard_window is None
    assert calls == []


def test_dashboard_can_be_reopened_after_failed_restore(
    backup_window, monkeypatch, tmp_path: Path
) -> None:
    window = backup_window
    source = _make_restore_source(tmp_path / "source.db", "復元元の品目")
    _patch_restore_dialogs(monkeypatch, source, QMessageBox.StandardButton.Yes)
    old = _open_dashboard(window)

    def failing_apply(_prepared, _db_path, _backup_dir):
        raise RestoreError(
            "injected restore failure",
            stage="overwrite",
            recovered=True,
            backup_path=tmp_path / "backups" / "pre-restore_test.db",
        )

    monkeypatch.setattr(window.context.backup, "apply_restore", failing_apply)

    window.restore_action.trigger()

    assert window._dashboard_window is None
    assert window.exit_request is ExitRequest.NORMAL
    new = _open_dashboard(window)
    assert new is not old
    assert new.isVisible()
