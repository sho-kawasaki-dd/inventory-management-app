from pathlib import Path

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import (
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    REASON_LABELS,
    FifoEstimate,
    NewItem,
    Reason,
)
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.stock_move_dialog import StockMoveDialog
from inventory_manager_mini.ui.signals import DataBus
from tests.conftest import unchecked_constraints


@pytest.fixture
def dialog_context(inventory, master, settings) -> AppContext:
    return AppContext(
        inventory=inventory,
        master=master,
        settings=settings,
        data_bus=DataBus(),
        db_path=Path(":memory:"),
        schema_version=3,
        app_version="0.1.0",
    )


def _create_item(context: AppContext, quantity: int = 0, reference_price: int | None = None):
    return context.inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="テスト品目",
            category_id=1,
            initial_quantity=quantity,
            initial_staff_id=1 if quantity else None,
            reference_price=reference_price,
        )
    )


def _ok_button(dialog: StockMoveDialog):
    return dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)


@pytest.mark.parametrize("reason", list(Reason))
def test_reason_combo_uses_shared_labels_and_initial_reason(
    qtbot, reason: Reason, inventory, master, settings
) -> None:
    context = AppContext(
        inventory=inventory,
        master=master,
        settings=settings,
        data_bus=DataBus(),
        db_path=Path(":memory:"),
        schema_version=3,
        app_version="0.1.0",
    )
    dialog = StockMoveDialog(context, reason)
    qtbot.addWidget(dialog)

    assert dialog.reason_combo.count() == len(Reason)
    assert [dialog.reason_combo.itemText(index) for index in range(len(Reason))] == [
        REASON_LABELS[value] for value in Reason
    ]
    assert dialog.reason_combo.currentData() == reason


@pytest.mark.parametrize(
    ("reason", "initial_quantity", "input_quantity", "expected_delta", "used_for"),
    [
        (Reason.IN, 5, 3, 3, None),
        (Reason.OUT, 5, 3, -3, "会議室"),
        (Reason.RETURN, 5, 3, -3, None),
        (Reason.DISPOSE, 5, 3, -3, None),
        (Reason.ADJUST, 5, 8, 3, None),
    ],
)
def test_each_stock_operation_saves_and_emits_change(
    qtbot, dialog_context, reason, initial_quantity, input_quantity, expected_delta, used_for
) -> None:
    item = _create_item(dialog_context, initial_quantity, reference_price=500)
    changes = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog = StockMoveDialog(dialog_context, reason, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.quantity_spin.setValue(input_quantity)
    dialog.note_edit.setPlainText("記録メモ")
    if used_for is not None:
        dialog.used_for_edit.setText(used_for)
    if reason is Reason.IN:
        dialog.unit_price_edit.setText("500")

    assert _ok_button(dialog).isEnabled()
    dialog._save()

    movement = dialog_context.inventory.list_history(item.id)[-1]
    assert movement.reason is reason
    assert movement.delta == expected_delta
    assert movement.staff_id == 1
    assert movement.used_for == used_for
    expected_price = 500 if reason in (Reason.IN, Reason.ADJUST) and expected_delta > 0 else None
    assert movement.unit_price == expected_price
    if reason in (Reason.OUT, Reason.DISPOSE):
        assert movement.cost_amount == abs(expected_delta) * 500
    else:
        assert movement.cost_amount is None
    assert movement.note == "記録メモ"
    assert dialog_context.inventory.get_item(item.id).quantity == initial_quantity + expected_delta
    assert changes == [True]
    assert dialog.result() == dialog.DialogCode.Accepted


def test_stocktake_defaults_to_current_quantity_and_shows_signed_delta(
    qtbot, dialog_context
) -> None:
    item = _create_item(dialog_context, quantity=9)
    dialog = StockMoveDialog(dialog_context, Reason.ADJUST, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)

    assert dialog.quantity_spin.minimum() == 0
    assert dialog.quantity_spin.value() == 9
    assert "+0 個" in dialog.preview_label.text()
    dialog.quantity_spin.setValue(4)
    assert "差分: -5 個" in dialog.preview_label.text()
    assert _ok_button(dialog).isEnabled()
    dialog._save()
    assert dialog_context.inventory.list_history(item.id)[-1].delta == -5


def test_reason_switch_updates_visible_fields(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=5)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)

    assert dialog.form_layout.isRowVisible(dialog.unit_price_edit)
    assert not dialog.form_layout.isRowVisible(dialog.estimate_label)
    assert not dialog.form_layout.isRowVisible(dialog.used_for_edit)
    dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findData(Reason.OUT))
    assert not dialog.form_layout.isRowVisible(dialog.unit_price_edit)
    assert dialog.form_layout.isRowVisible(dialog.estimate_label)
    assert dialog.form_layout.isRowVisible(dialog.used_for_edit)
    dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findData(Reason.ADJUST))
    assert dialog.quantity_label.text() == "実数"
    assert dialog.quantity_spin.minimum() == 0
    assert not dialog.form_layout.isRowVisible(dialog.estimate_label)
    dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findData(Reason.DISPOSE))
    assert dialog.form_layout.isRowVisible(dialog.estimate_label)


@pytest.mark.parametrize("quantity", [MAX_STOCK_QUANTITY - 1, MAX_STOCK_QUANTITY])
def test_stocktake_at_upper_bound_defaults_exactly_and_records_zero_delta(
    qtbot, dialog_context, quantity
) -> None:
    item = _create_item(dialog_context, quantity=quantity)
    dialog = StockMoveDialog(dialog_context, Reason.ADJUST, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)

    assert dialog.quantity_spin.value() == quantity
    assert _ok_button(dialog).isEnabled()
    dialog._save()
    assert dialog_context.inventory.list_history(item.id)[-1].delta == 0


def test_required_staff_and_issue_destination_update_ok_live(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=5)
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)

    assert not _ok_button(dialog).isEnabled()
    dialog.staff_combo.setCurrentIndex(1)
    assert not _ok_button(dialog).isEnabled()
    dialog.used_for_edit.setText("  ")
    assert not _ok_button(dialog).isEnabled()
    dialog.used_for_edit.setText("設備A")
    assert _ok_button(dialog).isEnabled()
    dialog.used_for_edit.clear()
    assert not _ok_button(dialog).isEnabled()


def test_quantity_boundaries_and_operation_amount_are_validated(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=MAX_STOCK_QUANTITY - 1)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.quantity_spin.setValue(1)
    assert _ok_button(dialog).isEnabled()
    dialog.quantity_spin.setValue(2)
    assert not _ok_button(dialog).isEnabled()
    assert f"{MAX_STOCK_QUANTITY:,}" in dialog.error_label.text()

    expensive_item = _create_item(dialog_context, quantity=10_001, reference_price=10_000)
    for reason in (Reason.OUT, Reason.DISPOSE):
        amount_dialog = StockMoveDialog(dialog_context, reason, expensive_item.id)
        qtbot.addWidget(amount_dialog)
        amount_dialog.staff_combo.setCurrentIndex(1)
        if reason is Reason.OUT:
            amount_dialog.used_for_edit.setText("設備A")
        amount_dialog.quantity_spin.setValue(10_000)
        assert MAX_MOVEMENT_AMOUNT == 10_000 * 10_000
        assert _ok_button(amount_dialog).isEnabled()
        amount_dialog.quantity_spin.setValue(10_001)
        assert not _ok_button(amount_dialog).isEnabled()
        assert f"{MAX_MOVEMENT_AMOUNT:,}" in amount_dialog.error_label.text()


def test_fifo_limit_validation_ignores_changed_reference_price(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=10_000, reference_price=10_000)
    dialog_context.inventory.conn.execute(
        "UPDATE items SET reference_price = ? WHERE id = ?", (MAX_UNIT_PRICE, item.id)
    )
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.used_for_edit.setText("設備A")
    dialog.quantity_spin.setValue(10_000)

    assert _ok_button(dialog).isEnabled()
    assert dialog.estimate_label.text() == "見積原価: 100,000,000 円 / 単価未登録数量: 0 個"
    estimate = dialog_context.inventory.estimate_outflow(item.id, 10_000)
    assert estimate.cost_amount == MAX_MOVEMENT_AMOUNT


def test_unpriced_only_fifo_outflow_remains_saveable(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=MAX_STOCK_QUANTITY, reference_price=None)
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.used_for_edit.setText("設備A")
    dialog.quantity_spin.setValue(MAX_STOCK_QUANTITY)

    assert _ok_button(dialog).isEnabled()
    assert dialog.estimate_label.text() == "見積原価: 0 円 / 単価未登録数量: 1,000,000 個"
    dialog._save()
    movement = dialog_context.inventory.list_history(item.id)[-1]
    assert (movement.unit_price, movement.cost_amount) == (None, 0)


@pytest.mark.parametrize("reason", [Reason.OUT, Reason.DISPOSE])
def test_multi_lot_fifo_estimate_is_exact_to_one_yen(qtbot, dialog_context, reason) -> None:
    def build(second_price: int):
        item = _create_item(dialog_context, quantity=10, reference_price=9_999_999)
        dialog_context.inventory.receive(item.id, 1, 1, unit_price=second_price)
        return item

    def open_dialog(item):
        dialog = StockMoveDialog(dialog_context, reason, item.id)
        qtbot.addWidget(dialog)
        dialog.staff_combo.setCurrentIndex(1)
        if reason is Reason.OUT:
            dialog.used_for_edit.setText("設備A")
        dialog.quantity_spin.setValue(11)
        return dialog

    exact = open_dialog(build(10))
    assert _ok_button(exact).isEnabled()
    assert "見積原価: 100,000,000 円" in exact.estimate_label.text()
    assert "単価未登録数量: 0 個" in exact.estimate_label.text()

    over = open_dialog(build(11))
    assert not _ok_button(over).isEnabled()
    assert "見積原価: 100,000,001 円" in over.estimate_label.text()
    assert f"{MAX_MOVEMENT_AMOUNT:,}" in over.error_label.text()


def test_fifo_estimate_shows_unpriced_quantity_across_lots(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=2)
    dialog_context.inventory.receive(item.id, 1, 3, unit_price=7)
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.used_for_edit.setText("設備A")
    dialog.quantity_spin.setValue(4)

    assert _ok_button(dialog).isEnabled()
    assert dialog.estimate_label.text() == "見積原価: 14 円 / 単価未登録数量: 2 個"


def test_estimate_failures_disable_ok_and_show_the_reason(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = _create_item(dialog_context, quantity=3, reference_price=100)
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.used_for_edit.setText("設備A")
    dialog.quantity_spin.setValue(4)
    assert not _ok_button(dialog).isEnabled()
    assert "在庫が不足しています" in dialog.error_label.text()

    def fail(item_id: int, quantity: int):
        raise ValidationError("見積を計算できません")

    monkeypatch.setattr(dialog_context.inventory, "estimate_outflow", fail)
    dialog.quantity_spin.setValue(3)
    assert not _ok_button(dialog).isEnabled()
    assert "見積を計算できません" in dialog.error_label.text()


def test_save_revalidates_the_estimate_and_does_not_record_over_limit(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = _create_item(dialog_context, quantity=3, reference_price=100)
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.used_for_edit.setText("設備A")
    dialog.quantity_spin.setValue(2)
    assert _ok_button(dialog).isEnabled()
    changes: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    history_before = dialog_context.inventory.list_history(item.id)

    monkeypatch.setattr(
        dialog_context.inventory,
        "estimate_outflow",
        lambda item_id, quantity: FifoEstimate(MAX_MOVEMENT_AMOUNT + 1, 0),
    )
    dialog._save()

    assert dialog_context.inventory.list_history(item.id) == history_before
    assert changes == []
    assert dialog.result() == 0
    assert not _ok_button(dialog).isEnabled()
    assert f"{MAX_MOVEMENT_AMOUNT:,}" in dialog.error_label.text()


def test_inbound_price_validation_and_other_reasons_ignore_price(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=2, reference_price=0)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    assert dialog.unit_price_edit.text() == "0"
    dialog.unit_price_edit.setText("1,000")
    assert not _ok_button(dialog).isEnabled()
    dialog.unit_price_edit.setText(str(MAX_UNIT_PRICE))
    assert _ok_button(dialog).isEnabled()
    dialog.unit_price_edit.setText(str(MAX_UNIT_PRICE + 1))
    assert not _ok_button(dialog).isEnabled()

    dialog.reason_combo.setCurrentIndex(dialog.reason_combo.findData(Reason.RETURN))
    assert _ok_button(dialog).isEnabled()


@pytest.mark.parametrize("price_text", ["", "0"])
def test_reference_price_confirmation_is_skipped_for_empty_or_unchanged_price(
    qtbot, dialog_context, monkeypatch, price_text
) -> None:
    item = _create_item(dialog_context, quantity=1, reference_price=0)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.unit_price_edit.setText(price_text)
    questions = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: questions.append(args))

    dialog._save()

    assert questions == []
    assert dialog.result() == dialog.DialogCode.Accepted


def test_inbound_return_and_stocktake_are_not_blocked_by_operation_amount(
    qtbot, dialog_context
) -> None:
    item = _create_item(dialog_context, quantity=20, reference_price=MAX_UNIT_PRICE)
    for reason in (Reason.IN, Reason.RETURN, Reason.ADJUST):
        dialog = StockMoveDialog(dialog_context, reason, item.id)
        qtbot.addWidget(dialog)
        dialog.staff_combo.setCurrentIndex(1)
        if reason is Reason.IN:
            dialog.quantity_spin.setValue(20)
            dialog.unit_price_edit.setText(str(MAX_UNIT_PRICE))
        assert 20 * MAX_UNIT_PRICE > MAX_MOVEMENT_AMOUNT
        assert _ok_button(dialog).isEnabled()


def test_no_active_staff_disables_save_with_setup_message(qtbot, dialog_context) -> None:
    dialog_context.master.deactivate_staff(1)
    dialog_context.master.deactivate_staff(2)
    item = _create_item(dialog_context)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)

    assert dialog.staff_combo.count() == 1
    assert "先に担当者マスタを登録してください" in dialog.error_label.text()
    assert not _ok_button(dialog).isEnabled()


@pytest.mark.parametrize(
    ("answer", "expected_price", "expected_saved"),
    [
        (QMessageBox.StandardButton.Yes, 200, True),
        (QMessageBox.StandardButton.No, 100, True),
        (QMessageBox.StandardButton.Cancel, 100, False),
    ],
)
def test_reference_price_confirmation_choices(
    qtbot, dialog_context, monkeypatch, answer, expected_price, expected_saved
) -> None:
    item = _create_item(dialog_context, reference_price=100)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.unit_price_edit.setText("200")
    questions = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args: questions.append(args) or answer,
    )

    dialog.show()
    qtbot.waitExposed(dialog)
    dialog._save()

    assert len(questions) == 1
    assert questions[0][2] == "参考価格を更新しますか"
    assert dialog_context.inventory.get_item(item.id).reference_price == expected_price
    assert bool(dialog_context.inventory.list_history(item.id)) is expected_saved
    if expected_saved:
        assert dialog.result() == dialog.DialogCode.Accepted
    else:
        assert dialog.result() == dialog.DialogCode.Rejected
        assert dialog.isVisible()


def test_search_is_debounced_and_selected_item_survives_filtering(qtbot, dialog_context) -> None:
    first = _create_item(dialog_context)
    second = dialog_context.inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="検索対象",
            category_id=1,
            manufacturer_part_number="PART-XYZ",
        )
    )
    dialog = StockMoveDialog(dialog_context, Reason.IN, first.id)
    qtbot.addWidget(dialog)
    selected_item = dialog._selected_item
    assert selected_item is not None and selected_item.id == first.id

    dialog.search_edit.setText("PART-XYZ")
    qtbot.waitUntil(lambda: dialog.item_list.model().rowCount() == 1, timeout=1500)
    assert dialog._candidate_items[0].id == second.id
    dialog.item_list.setCurrentIndex(dialog.item_list.model().index(0, 0))
    selected_item = dialog._selected_item
    assert selected_item is not None and selected_item.id == second.id

    dialog.search_edit.setText("no matching item")
    qtbot.waitUntil(lambda: dialog.item_list.model().rowCount() == 0, timeout=1500)
    selected_item = dialog._selected_item
    assert selected_item is not None and selected_item.id == second.id
    assert second.code in dialog.selected_item_label.text()


def test_inactive_item_is_not_initially_selected(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context)
    dialog_context.inventory.deactivate_item(item.id)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)

    assert dialog._selected_item is None
    assert dialog.item_id is None
    assert dialog._candidate_items == []
    assert dialog.item_list.model().rowCount() == 0
    assert not _ok_button(dialog).isEnabled()


def test_abnormal_quantities_and_prices_are_not_rounded(qtbot, dialog_context) -> None:
    item = _create_item(dialog_context, quantity=2)
    with unchecked_constraints(dialog_context.inventory.conn):
        dialog_context.inventory.conn.execute(
            "UPDATE items SET quantity = ? WHERE id = ?", (MAX_STOCK_QUANTITY + 1, item.id)
        )
    dialog = StockMoveDialog(dialog_context, Reason.ADJUST, item.id)
    qtbot.addWidget(dialog)
    changes = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    history_before = dialog_context.inventory.list_history(item.id)

    assert dialog.quantity_spin.value() == MAX_STOCK_QUANTITY + 1
    assert not dialog.quantity_spin.isEnabled()
    assert f"{MAX_STOCK_QUANTITY + 1:,}" in dialog.selected_item_label.text()
    assert not _ok_button(dialog).isEnabled()
    dialog._save()
    assert dialog_context.inventory.get_item(item.id).quantity == MAX_STOCK_QUANTITY + 1
    assert dialog_context.inventory.list_history(item.id) == history_before
    assert changes == []

    with unchecked_constraints(dialog_context.inventory.conn):
        dialog_context.inventory.conn.execute(
            "UPDATE items SET quantity = 0, reference_price = ? WHERE id = ?",
            (MAX_UNIT_PRICE + 1, item.id),
        )
    price_dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(price_dialog)
    assert price_dialog.unit_price_edit.text() == str(MAX_UNIT_PRICE + 1)
    assert not _ok_button(price_dialog).isEnabled()


def test_domain_error_keeps_dialog_open_and_does_not_emit_change(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = _create_item(dialog_context)
    dialog = StockMoveDialog(dialog_context, Reason.IN, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    warnings = []
    changes = []
    monkeypatch.setattr(
        dialog_context.inventory,
        "receive",
        lambda *args: (_ for _ in ()).throw(ValidationError("保存できません")),
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

    assert warnings and warnings[0][2] == "保存できません"
    assert dialog.isVisible()
    assert changes == []


def test_dialog_fits_available_area_with_large_font_and_scrollable_form(
    qtbot, dialog_context
) -> None:
    item = _create_item(dialog_context, quantity=1)
    dialog = StockMoveDialog(dialog_context, Reason.OUT, item.id)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(1)
    dialog.used_for_edit.setText("設備A")
    original_font = dialog.font()
    available = dialog.screen().availableGeometry()
    try:
        for scale in (1.0, 1.5):
            font = QFont(original_font)
            font.setPointSizeF(original_font.pointSizeF() * scale)
            dialog.setFont(font)
            dialog.selected_item_label.setText("品名" * 500)
            dialog.note_edit.setPlainText("長いメモ" * 500)
            dialog.error_label.setText("入力エラー" * 500)
            assert dialog.form_layout.isRowVisible(dialog.estimate_label)
            assert "単価未登録数量: 1 個" in dialog.estimate_label.text()
            dialog.show()
            qtbot.waitExposed(dialog)
            frame = dialog.frameGeometry()
            assert available.contains(frame)
            assert dialog.scroll_area.widget() is not None
            assert dialog.button_box.isVisible()
            assert dialog.scroll_area.verticalScrollBar().maximum() > 0
            dialog.hide()
    finally:
        dialog.setFont(original_font)
