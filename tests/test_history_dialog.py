from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import MAX_STOCK_QUANTITY, NewItem
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from inventory_manager_mini.ui.signals import DataBus


@pytest.fixture
def dialog_context(inventory, master, settings) -> AppContext:
    return AppContext(inventory, master, settings, DataBus(), Path(":memory:"), 3, "0.1.0")


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
