import csv
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QMessageBox

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import (
    MAX_STOCK_QUANTITY,
    Allocation,
    HistoryPeriodKind,
    NewItem,
)
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from tests.conftest import create_test_context


@pytest.fixture
def dialog_context(seeded_conn, inventory, master, settings) -> AppContext:
    return create_test_context(
        seeded_conn,
        inventory=inventory,
        master=master,
        settings=settings,
        schema_version=3,
    )


def _create_item(context: AppContext, quantity: int = 0, reference_price: int | None = None):
    return context.inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="履歴テスト品目",
            category_id=1,
            initial_quantity=quantity,
            initial_staff_id=1 if quantity else None,
            reference_price=reference_price,
        )
    )


def _ok_button(dialog: ReversalDialog):
    return dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)


def test_history_dialog_loads_item_and_orders_history_newest_first(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=2)
    dialog_context.inventory.receive(item.id, 1, 3)
    dialog = HistoryDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    assert item.code in dialog.item_info_label.text()
    assert item.name in dialog.item_info_label.text()
    assert "現在数量 5 個" in dialog.item_info_label.text()
    assert dialog.model.rowCount() == 2
    assert dialog.model.row_at(0).delta == 3
    assert dialog.model.row_at(1).delta == 2
    assert not dialog.table.isSortingEnabled()
    assert dialog.reverse_button.isEnabled() is False


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("reversal", "取り消し行は取り消せません"),
        ("already_reversed", "この履歴はすでに取り消されています"),
        ("zero_adjustment", "差分 0 の棚卸履歴は取り消せません"),
        ("inactive_item", "廃止品目の履歴は取り消せません"),
        (
            "consumed_lot",
            "この入庫(在庫増加)はすでに出庫・廃棄・返品・棚卸減少で消費されているため取り消せません",
        ),
        (
            "above_limit",
            "取り消し後の在庫数が上限(1,000,000)を超えるため実行できません",
        ),
    ],
)
def test_history_selection_disables_reversal_with_reason(
    qtbot, dialog_context, case: str, expected_reason: str
) -> None:
    if case == "above_limit":
        item = _create_item(dialog_context, quantity=MAX_STOCK_QUANTITY - 2)
        target = dialog_context.inventory.issue(item.id, 1, 2, "テスト")
        dialog_context.inventory.stocktake(item.id, 1, MAX_STOCK_QUANTITY)
    elif case == "consumed_lot":
        item = _create_item(dialog_context)
        target = dialog_context.inventory.receive(item.id, 1, 2)
        dialog_context.inventory.dispose(item.id, 1, 2)
    elif case == "zero_adjustment":
        item = _create_item(dialog_context, quantity=2)
        target = dialog_context.inventory.stocktake(item.id, 1, 2)
    else:
        item = _create_item(dialog_context)
        if case == "inactive_item":
            target = dialog_context.inventory.receive(item.id, 1, 2)
            dialog_context.inventory.deactivate_item(item.id)
        else:
            target = dialog_context.inventory.receive(item.id, 1, 2)
            if case == "already_reversed":
                dialog_context.inventory.reverse(target.id, 1)
            elif case == "reversal":
                target = dialog_context.inventory.reverse(target.id, 1)

    dialog = HistoryDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    target_row = dialog.model.row_of(target.id)
    assert target_row is not None

    dialog.table.selectRow(target_row)

    assert not dialog.reverse_button.isEnabled()
    assert dialog.reverse_button.toolTip() == expected_reason


def test_successful_reversal_refreshes_history_and_reselects_original(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = _create_item(dialog_context, quantity=4, reference_price=100)
    original = dialog_context.inventory.receive(item.id, 1, 2, 250, True, "仕入れメモ")
    dialog = HistoryDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    original_row = dialog.model.row_of(original.id)
    assert original_row is not None
    dialog.table.selectRow(original_row)
    dialog_context.master.deactivate_client(1)
    dialog_context.master.deactivate_purchaser(1)
    dialog_context.master.deactivate_staff(1)
    changes = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))

    def execute_and_save(reversal_dialog: ReversalDialog) -> QDialog.DialogCode:
        reversal_dialog.staff_combo.setCurrentIndex(1)
        reversal_dialog.note_edit.setPlainText("取消理由")
        reversal_dialog._save()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReversalDialog, "exec", execute_and_save)
    dialog._open_reversal()

    assert dialog.isVisible()
    assert dialog.model.rowCount() == 3
    assert dialog.table.currentIndex().isValid()
    selected = dialog.model.row_at(dialog.table.currentIndex().row())
    assert selected.id == original.id
    assert selected.is_reversed
    assert (
        dialog.model.data(
            dialog.model.index(dialog.table.currentIndex().row(), 0),
            Qt.ItemDataRole.ForegroundRole,
        )
        is not None
    )
    reversal = next(
        entry for entry in dialog_context.inventory.list_history(item.id) if entry.reversal_of
    )
    assert reversal.delta == -original.delta
    assert reversal.unit_price == original.unit_price
    assert reversal.used_for == original.used_for
    assert reversal.client_id == original.client_id
    assert reversal.purchaser_id == original.purchaser_id
    assert reversal.staff_id == 2
    assert reversal.note == "取消理由"
    assert dialog_context.inventory.get_item(item.id).quantity == 4
    assert dialog_context.inventory.get_item(item.id).reference_price == 250
    assert changes == [True]


@pytest.mark.parametrize(
    ("operation", "unit_price", "expected_cost"),
    [("issue", 10, 10), ("issue", 0, 0), ("return", 10, None)],
)
def test_history_reversal_preserves_cost_and_reverses_allocations(
    qtbot, dialog_context, monkeypatch, operation: str, unit_price: int, expected_cost: int | None
) -> None:
    item = _create_item(dialog_context)
    lot = dialog_context.inventory.receive(item.id, 1, 2, unit_price=unit_price)
    if operation == "issue":
        movement = dialog_context.inventory.issue(item.id, 1, 1, "テスト")
    else:
        movement = dialog_context.inventory.return_to_supplier(item.id, 1, 1)

    dialog = HistoryDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    row_index = dialog.model.row_of(movement.id)
    assert row_index is not None
    dialog.table.selectRow(row_index)
    assert dialog.reverse_button.isEnabled()

    def execute_and_save(reversal_dialog: ReversalDialog) -> QDialog.DialogCode:
        reversal_dialog.staff_combo.setCurrentIndex(1)
        reversal_dialog._save()
        assert reversal_dialog.result() == QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReversalDialog, "exec", execute_and_save)
    dialog._open_reversal()

    reversal = next(
        row
        for row in dialog_context.inventory.list_history(item.id)
        if row.reversal_of == movement.id
    )
    assert movement.cost_amount == expected_cost
    assert reversal.cost_amount == (None if expected_cost is None else -expected_cost)
    assert reversal.unit_price == movement.unit_price
    original_allocations = dialog_context.inventory.allocations.list_for_movement(movement.id)
    reversal_allocations = dialog_context.inventory.allocations.list_for_movement(reversal.id)
    assert original_allocations == (Allocation(lot.id, -movement.delta),)
    assert reversal_allocations == tuple(
        Allocation(allocation.lot_id, -allocation.quantity) for allocation in original_allocations
    )
    original_row = dialog.model.row_of(movement.id)
    assert original_row is not None
    assert dialog.model.row_at(original_row).is_reversed


def test_history_can_reverse_consumption_then_its_receipt(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = _create_item(dialog_context)
    receipt = dialog_context.inventory.receive(item.id, 1, 2, unit_price=10)
    issue = dialog_context.inventory.issue(item.id, 1, 1, "テスト")
    dialog = HistoryDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    def execute_and_save(reversal_dialog: ReversalDialog) -> QDialog.DialogCode:
        reversal_dialog.staff_combo.setCurrentIndex(1)
        reversal_dialog._save()
        assert reversal_dialog.result() == QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReversalDialog, "exec", execute_and_save)
    receipt_row = dialog.model.row_of(receipt.id)
    assert receipt_row is not None
    dialog.table.selectRow(receipt_row)
    assert not dialog.reverse_button.isEnabled()
    assert dialog.reverse_button.toolTip() == (
        "この入庫(在庫増加)はすでに出庫・廃棄・返品・棚卸減少で消費されているため取り消せません"
    )

    issue_row = dialog.model.row_of(issue.id)
    assert issue_row is not None
    dialog.table.selectRow(issue_row)
    assert dialog.reverse_button.isEnabled()
    dialog._open_reversal()

    receipt_row = dialog.model.row_of(receipt.id)
    assert receipt_row is not None
    dialog.table.selectRow(receipt_row)
    assert dialog.reverse_button.isEnabled()
    dialog._open_reversal()

    history = dialog_context.inventory.list_history(item.id)
    issue_reversal = next(row for row in history if row.reversal_of == issue.id)
    receipt_reversal = next(row for row in history if row.reversal_of == receipt.id)
    assert issue_reversal.cost_amount == -10
    assert receipt_reversal.unit_price == receipt.unit_price == 10
    assert receipt_reversal.cost_amount is None
    assert dialog_context.inventory.allocations.list_for_movement(issue_reversal.id) == (
        Allocation(receipt.id, -1),
    )
    assert dialog_context.inventory.allocations.list_for_movement(receipt_reversal.id) == ()
    assert dialog_context.inventory.get_item(item.id).quantity == 0


def test_reversal_requires_active_staff_and_saves_only_on_success(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=3)
    movement = dialog_context.inventory.receive(item.id, 1, 1)
    row = dialog_context.inventory.list_history(item.id)[-1]
    dialog = ReversalDialog(dialog_context, row)
    qtbot.addWidget(dialog)
    changes = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))

    assert dialog.staff_combo.currentData() is None
    assert not _ok_button(dialog).isEnabled()
    dialog.staff_combo.setCurrentIndex(1)
    assert _ok_button(dialog).isEnabled()
    assert "取り消し後数量: 3 個" in dialog.resulting_quantity_label.text()
    dialog._save()

    assert dialog.result() == dialog.DialogCode.Accepted
    assert changes == [True]
    assert dialog_context.inventory.get_item(item.id).quantity == 3
    assert dialog_context.inventory.reversal_block_reason(movement.id) == (
        "この履歴はすでに取り消されています"
    )


def test_reversal_with_no_active_staff_is_disabled(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=1)
    row = dialog_context.inventory.list_history(item.id)[0]
    dialog_context.master.deactivate_staff(1)
    dialog_context.master.deactivate_staff(2)
    dialog = ReversalDialog(dialog_context, row)
    qtbot.addWidget(dialog)

    assert not _ok_button(dialog).isEnabled()
    assert "先に担当者マスタを登録してください" in dialog.error_label.text()


def test_service_domain_error_keeps_reversal_dialog_open_without_notification(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = _create_item(dialog_context, quantity=2)
    row = dialog_context.inventory.list_history(item.id)[0]
    dialog = ReversalDialog(dialog_context, row)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    warnings = []
    changes = []
    monkeypatch.setattr(
        dialog_context.inventory,
        "reverse",
        lambda *_args: (_ for _ in ()).throw(ValidationError("履歴はすでに更新されています")),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args) or QMessageBox.StandardButton.Ok,
    )
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog._save()

    assert dialog.isVisible()
    assert warnings and warnings[0][2] == "履歴はすでに更新されています"
    assert changes == []


def test_quantity_increase_after_reversal_dialog_opens_is_rejected_atomically(
    qtbot, dialog_context, seeded_conn, monkeypatch
) -> None:
    item = _create_item(dialog_context, quantity=MAX_STOCK_QUANTITY - 3)
    movement = dialog_context.inventory.issue(item.id, 1, 2, "テスト")
    row = dialog_context.inventory.list_history(item.id)[-1]
    dialog = ReversalDialog(dialog_context, row)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    assert _ok_button(dialog).isEnabled()
    dialog_context.inventory.stocktake(item.id, 1, MAX_STOCK_QUANTITY)
    before_history = dialog_context.inventory.list_history(item.id)
    before_aggregates = tuple(
        seeded_conn.execute(
            "SELECT inbound_quantity, outbound_quantity, disposed_quantity, expenditure, "
            "disposal_amount FROM total_aggregates WHERE id = 1"
        ).fetchone()
    )
    warnings = []
    changes = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args) or QMessageBox.StandardButton.Ok,
    )
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog._save()

    assert dialog.isVisible()
    assert warnings and warnings[0][2] == (
        "取り消し後の在庫数が上限(1,000,000)を超えるため実行できません"
    )
    assert dialog_context.inventory.get_item(item.id).quantity == MAX_STOCK_QUANTITY
    assert dialog_context.inventory.list_history(item.id) == before_history
    assert (
        tuple(
            seeded_conn.execute(
                "SELECT inbound_quantity, outbound_quantity, disposed_quantity, expenditure, "
                "disposal_amount FROM total_aggregates WHERE id = 1"
            ).fetchone()
        )
        == before_aggregates
    )
    assert changes == []
    assert dialog_context.inventory.reversal_block_reason(movement.id) == (
        "取り消し後の在庫数が上限(1,000,000)を超えるため実行できません"
    )


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_history_and_reversal_dialogs_fit_with_large_content(
    qtbot, dialog_context, scale: float
) -> None:
    item = _create_item(dialog_context, quantity=2)
    row = dialog_context.inventory.list_history(item.id)[0]
    history = HistoryDialog(dialog_context, item.id)
    reversal = ReversalDialog(dialog_context, row)
    qtbot.addWidget(history)
    qtbot.addWidget(reversal)
    original_font = history.font()
    available = history.screen().availableGeometry()
    try:
        font = QFont(original_font)
        font.setPointSizeF(original_font.pointSizeF() * scale)
        history.setFont(font)
        reversal.setFont(font)
        history.item_info_label.setText("長い品名" * 500)
        reversal.used_for_label.setText("長い使用先" * 500)
        reversal.note_edit.setPlainText("長いメモ" * 500)
        reversal.error_label.setText("長いエラー" * 500)
        for dialog in (history, reversal):
            dialog.show()
            qtbot.waitExposed(dialog)
            assert available.contains(dialog.frameGeometry())
            if isinstance(dialog, ReversalDialog):
                assert not dialog.scroll_area.isHidden()
            dialog.hide()
        assert not history.reverse_button.isHidden()
        assert not history.close_button.isHidden()
        assert not reversal.button_box.isHidden()
        assert reversal.scroll_area.verticalScrollBar().maximum() > 0
    finally:
        history.setFont(original_font)
        reversal.setFont(original_font)


def _patch_save_dialog(monkeypatch, selected: Path | None) -> None:
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda _parent, _caption, _default_name, _filter: (
            str(selected) if selected is not None else "",
            "",
        ),
    )


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def _history_dialog_with_two_items(qtbot, context: AppContext, monkeypatch):
    first = _create_item(context, quantity=2)
    context.inventory.receive(first.id, 1, 3)
    _create_item(context, quantity=1)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    dialog = HistoryDialog(context, first.id)
    qtbot.addWidget(dialog)
    return dialog, first


def test_history_csv_exports_only_current_item(
    qtbot, dialog_context, monkeypatch, tmp_path: Path
) -> None:
    dialog, first = _history_dialog_with_two_items(qtbot, dialog_context, monkeypatch)
    destination = tmp_path / "history.csv"
    _patch_save_dialog(monkeypatch, destination)

    dialog.export_button.click()

    rows = _read_csv(destination)
    assert rows[0][0] == "日時"
    assert len(rows) - 1 == 2
    assert {row[1] for row in rows[1:]} == {first.code}
    assert len(dialog_context.inventory.list_all_history()) == 3


def test_history_csv_cancel_creates_no_file(
    qtbot, dialog_context, monkeypatch, tmp_path: Path
) -> None:
    dialog, _first = _history_dialog_with_two_items(qtbot, dialog_context, monkeypatch)
    _patch_save_dialog(monkeypatch, None)

    dialog.export_button.click()

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
def test_history_csv_confirms_overwrite_only_after_extension_completion(
    qtbot, dialog_context, monkeypatch, tmp_path: Path, answer
) -> None:
    dialog, _first = _history_dialog_with_two_items(qtbot, dialog_context, monkeypatch)
    existing = tmp_path / "out.csv"
    existing.write_bytes(b"old")
    _patch_save_dialog(monkeypatch, tmp_path / "out")
    questions = []

    def fake_question(_parent, _title, _text, _buttons, default):
        questions.append(default)
        return answer

    monkeypatch.setattr(QMessageBox, "question", fake_question)

    dialog.export_button.click()

    assert questions == [QMessageBox.StandardButton.No]
    if answer == QMessageBox.StandardButton.Yes:
        assert _read_csv(existing)[0][0] == "日時"
    else:
        assert existing.read_bytes() == b"old"


def test_history_csv_regular_csv_path_and_new_completed_path_need_no_confirmation(
    qtbot, dialog_context, monkeypatch, tmp_path: Path
) -> None:
    dialog, _first = _history_dialog_with_two_items(qtbot, dialog_context, monkeypatch)

    def fail(*_args, **_kwargs):
        raise AssertionError("追加の上書き確認は表示されないはずです")

    monkeypatch.setattr(QMessageBox, "question", fail)
    existing = tmp_path / "regular.csv"
    existing.write_bytes(b"old")
    _patch_save_dialog(monkeypatch, existing)
    dialog.export_button.click()
    _patch_save_dialog(monkeypatch, tmp_path / "fresh")
    dialog.export_button.click()

    assert _read_csv(existing)[0][0] == "日時"
    assert _read_csv(tmp_path / "fresh.csv")[0][0] == "日時"


def test_history_csv_failure_shows_error_and_logs(
    qtbot, dialog_context, monkeypatch, tmp_path: Path, caplog
) -> None:
    dialog, _first = _history_dialog_with_two_items(qtbot, dialog_context, monkeypatch)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, message: warnings.append(message)
    )
    caplog.set_level(logging.INFO, logger="inventory_manager_mini.ui.csv_export")
    destination = tmp_path / "missing" / "out.csv"
    _patch_save_dialog(monkeypatch, destination)

    dialog.export_button.click()

    assert len(warnings) == 1
    assert str(destination) in warnings[0]
    levels = [r.levelno for r in caplog.records if r.name == "inventory_manager_mini.ui.csv_export"]
    assert levels == [logging.INFO, logging.ERROR]
    assert dialog.isEnabled()


@pytest.fixture
def paged_context(dialog_context, seeded_conn, fixed_clock) -> AppContext:
    dialog_context.reports = ReportService(
        seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock
    )
    return dialog_context


def _select_period_kind(dialog: HistoryDialog, kind: HistoryPeriodKind) -> None:
    combo = dialog.period_selector.period_kind_combo
    combo.setCurrentIndex(combo.findData(kind))


def _set_page_size(dialog: HistoryDialog, size: int) -> None:
    dialog.page_size_combo.setCurrentIndex(dialog.page_size_combo.findData(size))


def test_history_pagination_shows_pages_and_toggles_buttons(
    qtbot, paged_context, seeded_conn, bulk_history
) -> None:
    item = _create_item(paged_context)
    bulk_history(seeded_conn, item.id, 650)
    dialog = HistoryDialog(paged_context, item.id)
    qtbot.addWidget(dialog)

    assert dialog.page_size_combo.currentData() == 300
    assert dialog.model.rowCount() == 300
    assert dialog.page_label.text() == "1 / 3 ページ (全 650 件)"
    assert not dialog.prev_page_button.isEnabled()
    assert dialog.next_page_button.isEnabled()

    dialog.next_page_button.click()
    assert dialog.page_label.text() == "2 / 3 ページ (全 650 件)"
    assert dialog.prev_page_button.isEnabled()
    assert dialog.next_page_button.isEnabled()

    dialog.next_page_button.click()
    assert dialog.model.rowCount() == 50
    assert dialog.page_label.text() == "3 / 3 ページ (全 650 件)"
    assert dialog.prev_page_button.isEnabled()
    assert not dialog.next_page_button.isEnabled()

    dialog.prev_page_button.click()
    assert dialog.page_label.text() == "2 / 3 ページ (全 650 件)"


def test_history_page_size_change_returns_to_first_page(
    qtbot, paged_context, seeded_conn, bulk_history
) -> None:
    item = _create_item(paged_context)
    bulk_history(seeded_conn, item.id, 650)
    dialog = HistoryDialog(paged_context, item.id)
    qtbot.addWidget(dialog)
    dialog.next_page_button.click()
    assert dialog.page_label.text().startswith("2 / 3")

    _set_page_size(dialog, 500)

    assert dialog.model.rowCount() == 500
    assert dialog.page_label.text() == "1 / 2 ページ (全 650 件)"
    assert not dialog.prev_page_button.isEnabled()
    dialog.next_page_button.click()
    assert dialog.model.rowCount() == 150
    assert not dialog.next_page_button.isEnabled()


def test_history_without_rows_disables_page_buttons(qtbot, paged_context) -> None:
    item = _create_item(paged_context)
    dialog = HistoryDialog(paged_context, item.id)
    qtbot.addWidget(dialog)

    assert dialog.model.rowCount() == 0
    assert dialog.page_label.text() == "0 / 0 ページ (全 0 件)"
    assert not dialog.prev_page_button.isEnabled()
    assert not dialog.next_page_button.isEnabled()
    assert dialog.export_button.isEnabled()


def test_history_for_missing_item_disables_export_and_paging(
    qtbot, paged_context, monkeypatch
) -> None:
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: warnings.append(args))
    dialog = HistoryDialog(paged_context, 9999)
    qtbot.addWidget(dialog)

    assert dialog.item_info_label.text() == "品目が見つかりません"
    assert dialog.model.rowCount() == 0
    assert not dialog.export_button.isEnabled()
    assert not dialog.prev_page_button.isEnabled()
    assert not dialog.next_page_button.isEnabled()
    assert warnings == []


@pytest.mark.parametrize("page_size", [300, 500])
def test_reversal_reselects_original_moved_to_next_page(
    qtbot, paged_context, monkeypatch, page_size: int
) -> None:
    item = _create_item(paged_context)
    for _ in range(page_size * 2):
        paged_context.inventory.receive(item.id, 1, 1)
    dialog = HistoryDialog(paged_context, item.id)
    qtbot.addWidget(dialog)
    _set_page_size(dialog, page_size)
    dialog.next_page_button.click()
    original = dialog.model.row_at(dialog.model.rowCount() - 1)
    assert dialog.page_label.text().startswith("2 / 2")
    dialog.table.selectRow(dialog.model.rowCount() - 1)

    def execute_and_save(reversal_dialog: ReversalDialog) -> QDialog.DialogCode:
        reversal_dialog.staff_combo.setCurrentIndex(1)
        reversal_dialog._save()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReversalDialog, "exec", execute_and_save)
    dialog._open_reversal()

    assert dialog.page_label.text() == f"3 / 3 ページ (全 {page_size * 2 + 1:,} 件)"
    assert dialog.model.rowCount() == 1
    assert dialog.model.row_at(dialog.table.currentIndex().row()).id == original.id
    assert dialog.model.row_at(0).is_reversed


def _receive_at(context: AppContext, item_id: int, fixed_clock, moved_at: datetime) -> None:
    fixed_clock.current = moved_at
    context.inventory.receive(item_id, 1, 1)


def _history_with_periods(context: AppContext, fixed_clock) -> int:
    item = _create_item(context)
    for moved_at in (
        datetime(2024, 5, 10, 3, tzinfo=UTC),
        datetime(2025, 5, 10, 3, tzinfo=UTC),
        datetime(2025, 6, 10, 3, tzinfo=UTC),
        datetime(2026, 2, 10, 3, tzinfo=UTC),
    ):
        _receive_at(context, item.id, fixed_clock, moved_at)
    return item.id


def test_history_period_filter_updates_rows_and_controls(qtbot, paged_context, fixed_clock) -> None:
    item_id = _history_with_periods(paged_context, fixed_clock)
    dialog = HistoryDialog(paged_context, item_id)
    qtbot.addWidget(dialog)
    selector = dialog.period_selector
    assert dialog.model.rowCount() == 4
    assert not selector.year_combo.isEnabled()
    assert not selector.month_combo.isEnabled()

    _select_period_kind(dialog, HistoryPeriodKind.ANNUAL)
    assert dialog.model.rowCount() == 3
    assert selector.year_combo.isEnabled()
    assert not selector.month_combo.isEnabled()
    assert dialog.page_label.text() == "1 / 1 ページ (全 3 件)"

    _select_period_kind(dialog, HistoryPeriodKind.MONTHLY)
    selector.month_combo.setCurrentIndex(selector.month_combo.findData(5))
    assert selector.month_combo.isEnabled()
    assert dialog.model.rowCount() == 1

    selector.year_combo.setCurrentIndex(selector.year_combo.findData(2024))
    assert dialog.model.rowCount() == 1
    assert dialog.model.row_at(0).moved_at == "2024-05-10 03:00:00"

    _select_period_kind(dialog, HistoryPeriodKind.ALL)
    assert dialog.model.rowCount() == 4


def test_history_period_change_resets_to_first_page(
    qtbot, paged_context, seeded_conn, bulk_history
) -> None:
    item = _create_item(paged_context)
    bulk_history(seeded_conn, item.id, 650)
    dialog = HistoryDialog(paged_context, item.id)
    qtbot.addWidget(dialog)
    dialog.next_page_button.click()

    _select_period_kind(dialog, HistoryPeriodKind.ANNUAL)

    assert dialog.page_label.text() == "1 / 3 ページ (全 650 件)"


def test_year_change_loads_history_page_once(
    qtbot, paged_context, fixed_clock, monkeypatch
) -> None:
    item_id = _history_with_periods(paged_context, fixed_clock)
    dialog = HistoryDialog(paged_context, item_id)
    qtbot.addWidget(dialog)
    _select_period_kind(dialog, HistoryPeriodKind.MONTHLY)
    calls = []
    original = paged_context.reports.list_history_page

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(paged_context.reports, "list_history_page", spy)
    selector = dialog.period_selector
    selector.year_combo.setCurrentIndex(selector.year_combo.findData(2024))

    assert len(calls) == 1


def test_history_csv_exports_all_pages_of_selected_period(
    qtbot, paged_context, seeded_conn, bulk_history, monkeypatch, tmp_path: Path
) -> None:
    item = _create_item(paged_context)
    bulk_history(seeded_conn, item.id, 5, datetime(2024, 6, 1, tzinfo=UTC))
    bulk_history(seeded_conn, item.id, 650)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    dialog = HistoryDialog(paged_context, item.id)
    qtbot.addWidget(dialog)
    destination = tmp_path / "period.csv"
    _patch_save_dialog(monkeypatch, destination)

    _select_period_kind(dialog, HistoryPeriodKind.ANNUAL)
    assert dialog.model.rowCount() == 300
    dialog.export_button.click()
    assert len(_read_csv(destination)) - 1 == 650

    dialog.period_selector.year_combo.setCurrentIndex(
        dialog.period_selector.year_combo.findData(2024)
    )
    dialog.export_button.click()
    assert len(_read_csv(destination)) - 1 == 5

    _select_period_kind(dialog, HistoryPeriodKind.ALL)
    dialog.export_button.click()
    assert len(_read_csv(destination)) - 1 == 655
