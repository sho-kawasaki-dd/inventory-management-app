from pathlib import Path

import pytest
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import (
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    ItemFilter,
    NewItem,
)
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog
from inventory_manager_mini.ui.signals import DataBus
from inventory_manager_mini.ui.widgets.category_picker import CategoryPickerDialog
from tests.conftest import unchecked_constraints


@pytest.fixture
def dialog_context(
    inventory: InventoryService,
    master: MasterService,
    settings: SettingsService,
) -> AppContext:
    return AppContext(inventory, master, settings, DataBus(), Path("test.db"), 1, "0.1.0")


def _fill_required(dialog: ItemDialog) -> None:
    dialog.client_combo.setCurrentIndex(0)
    dialog.name_edit.setText("新しい品目")
    dialog.category_picker.set_current_category_id(1)
    dialog.purchaser_combo.setCurrentIndex(0)


def _ok_button(dialog: ItemDialog):
    return dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)


def test_new_item_dialog_validates_initial_staff_and_records_quantity(
    qtbot, dialog_context
) -> None:
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)
    initial_quantity_spin = dialog.initial_quantity_spin
    initial_staff_combo = dialog.initial_staff_combo
    assert initial_quantity_spin is not None
    assert initial_staff_combo is not None
    assert dialog.quantity_label is None
    assert dialog.last_purchase_label is None
    _fill_required(dialog)

    assert dialog.unit_label.text() == "個"
    assert not initial_staff_combo.isEnabled()
    assert _ok_button(dialog).isEnabled()
    initial_quantity_spin.setValue(2)
    assert initial_staff_combo.isEnabled()
    assert not _ok_button(dialog).isEnabled()
    initial_staff_combo.setCurrentIndex(1)
    assert _ok_button(dialog).isEnabled()

    dialog._save()
    item = dialog_context.inventory.list_items(ItemFilter())[0]
    movements = dialog_context.inventory.list_history(item.id)
    assert item.quantity == 2
    assert movements[0].staff_id == 1
    assert dialog.result() == dialog.DialogCode.Accepted


def test_category_selected_via_picker_dialog_enables_ok(qtbot, dialog_context, monkeypatch) -> None:
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)
    dialog.client_combo.setCurrentIndex(0)
    dialog.name_edit.setText("新しい品目")
    dialog.purchaser_combo.setCurrentIndex(0)
    assert "カテゴリを選択してください" in dialog.error_label.text()
    assert not _ok_button(dialog).isEnabled()

    def fake_exec(picker_dialog: CategoryPickerDialog) -> QDialog.DialogCode:
        picker_dialog.tree.setCurrentItem(picker_dialog._items[1])
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CategoryPickerDialog, "exec", fake_exec)
    dialog.category_picker.open_dialog()

    assert dialog.category_picker.current_category_id() == 1
    assert "カテゴリを選択してください" not in dialog.error_label.text()
    assert _ok_button(dialog).isEnabled()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https:///missing-host"])
def test_invalid_url_keeps_ok_disabled(qtbot, dialog_context, url: str) -> None:
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)
    _fill_required(dialog)
    dialog.purchase_url_edit.setText(url)

    assert not _ok_button(dialog).isEnabled()
    assert "販売ページ URL" in dialog.error_label.text()


def test_edit_keeps_only_current_inactive_masters_and_saves(qtbot, dialog_context) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="既存品目", category_id=1)
    )
    dialog_context.master.deactivate_client(1)
    dialog_context.master.deactivate_purchaser(1)
    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.client_combo.count() == 2
    assert dialog.client_combo.currentText() == "総務 (無効)"
    assert dialog.client_combo.findData(3) == -1
    assert "変更する場合は有効なものを選択してください" in dialog.client_notice.text()
    assert dialog.purchaser_combo.count() == 2
    assert dialog.purchaser_combo.currentText() == "本部 (無効)"
    assert dialog.purchaser_combo.findData(3) == -1
    assert "変更する場合は有効なものを選択してください" in dialog.purchaser_notice.text()
    assert dialog.initial_staff_combo is None
    assert dialog.initial_staff_row is None
    assert dialog.initial_quantity_spin is None
    assert dialog.initial_quantity_row is None
    assert dialog.unit_label.text() == "個"
    assert dialog.quantity_label is not None
    assert dialog.quantity_label.text() == "0 個"
    assert _ok_button(dialog).isEnabled()
    dialog.name_edit.setText("更新後")
    dialog._save()

    assert dialog_context.inventory.get_item(item.id).name == "更新後"
    assert dialog.result() == dialog.DialogCode.Accepted


def test_new_dialog_requires_master_choices_and_emits_change_only_on_save(
    qtbot, dialog_context
) -> None:
    dialog_context.master.deactivate_client(1)
    dialog_context.master.deactivate_client(2)
    dialog_context.master.deactivate_purchaser(1)
    dialog_context.master.deactivate_purchaser(2)
    changes = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)

    assert not _ok_button(dialog).isEnabled()
    assert "先にクライアントマスタを登録してください" in dialog.error_label.text()
    assert "先に発注主体マスタを登録してください" in dialog.error_label.text()
    assert changes == []


def test_new_dialog_without_categories_requires_master_setup(qtbot, dialog_context) -> None:
    dialog_context.master.delete_category(2)
    dialog_context.master.delete_category(1)
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)

    assert not _ok_button(dialog).isEnabled()
    assert "先にカテゴリマスタを登録してください" in dialog.error_label.text()


def test_service_validation_error_keeps_edit_dialog_open(
    qtbot, dialog_context, monkeypatch
) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="既存品目", category_id=1)
    )
    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        dialog_context.inventory,
        "update_item",
        lambda _update: (_ for _ in ()).throw(ValidationError("担当者が無効です")),
    )
    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args) or QMessageBox.StandardButton.Ok,
    )
    dialog.show()
    qtbot.waitExposed(dialog)

    dialog._save()

    assert dialog.isVisible()
    assert warnings and warnings[0][2] == "担当者が無効です"


def test_edit_purchase_info_and_dialog_size(qtbot, dialog_context) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="購入品", category_id=1)
    )
    dialog_context.inventory.receive(item.id, staff_id=1, quantity=3)
    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.last_purchase_label is not None
    assert "個" in dialog.last_purchase_label.text()
    assert dialog.sizeHint().width() <= 1366
    assert dialog.sizeHint().height() <= 768


def test_reference_price_zero_is_distinct_from_empty(qtbot, dialog_context) -> None:
    zero_dialog = ItemDialog(dialog_context)
    qtbot.addWidget(zero_dialog)
    _fill_required(zero_dialog)
    zero_dialog.reference_price_edit.setText("0")
    zero_dialog._save()

    empty_dialog = ItemDialog(dialog_context)
    qtbot.addWidget(empty_dialog)
    _fill_required(empty_dialog)
    empty_dialog._save()

    items = dialog_context.inventory.list_items(ItemFilter())
    assert [item.reference_price for item in items] == [0, None]


def test_numeric_inputs_match_business_limits_and_reject_group_separators(
    qtbot, dialog_context
) -> None:
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)

    assert dialog.initial_quantity_spin is not None
    assert dialog.initial_quantity_spin.maximum() == MAX_STOCK_QUANTITY
    assert dialog.threshold_spin.maximum() == MAX_STOCK_QUANTITY
    reorder_validator = dialog.reorder_quantity_edit.validator()
    reference_price_validator = dialog.reference_price_edit.validator()
    assert isinstance(reorder_validator, QIntValidator)
    assert isinstance(reference_price_validator, QIntValidator)
    assert reorder_validator.top() == MAX_STOCK_QUANTITY
    assert reference_price_validator.top() == MAX_UNIT_PRICE
    _fill_required(dialog)

    dialog.reference_price_edit.setText("10000001")
    assert not _ok_button(dialog).isEnabled()
    assert "参考価格" in dialog.error_label.text()
    dialog.reference_price_edit.clear()
    assert _ok_button(dialog).isEnabled()

    dialog.reference_price_edit.setText("1,000")
    assert not dialog.reference_price_edit.hasAcceptableInput()
    assert not _ok_button(dialog).isEnabled()


@pytest.mark.parametrize("text", ["0", "1000001", "-1", "1,000", "abc"])
def test_invalid_reorder_quantity_disables_ok(qtbot, dialog_context, text: str) -> None:
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)
    _fill_required(dialog)
    assert _ok_button(dialog).isEnabled()

    dialog.reorder_quantity_edit.setText(text)

    assert not _ok_button(dialog).isEnabled()
    assert "推奨発注数" in dialog.error_label.text()
    dialog.reorder_quantity_edit.setText("1")
    assert _ok_button(dialog).isEnabled()


def test_new_item_saves_values_at_the_business_limits(qtbot, dialog_context) -> None:
    dialog = ItemDialog(dialog_context)
    qtbot.addWidget(dialog)
    _fill_required(dialog)
    assert dialog.initial_quantity_spin is not None
    assert dialog.initial_staff_combo is not None
    dialog.initial_quantity_spin.setValue(MAX_STOCK_QUANTITY)
    dialog.initial_staff_combo.setCurrentIndex(1)
    dialog.threshold_spin.setValue(MAX_STOCK_QUANTITY)
    dialog.reorder_quantity_edit.setText(str(MAX_STOCK_QUANTITY))
    dialog.reference_price_edit.setText(str(MAX_UNIT_PRICE))
    assert _ok_button(dialog).isEnabled()

    dialog._save()

    item = dialog_context.inventory.list_items(ItemFilter())[0]
    assert (
        item.quantity,
        item.reorder_threshold,
        item.reorder_quantity,
        item.reference_price,
    ) == (MAX_STOCK_QUANTITY, MAX_STOCK_QUANTITY, MAX_STOCK_QUANTITY, MAX_UNIT_PRICE)
    assert dialog.result() == dialog.DialogCode.Accepted


def test_edit_dialog_keeps_values_at_the_business_limits_without_rounding(
    qtbot, dialog_context
) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="上限品目",
            category_id=1,
            initial_quantity=MAX_STOCK_QUANTITY,
            initial_staff_id=1,
            reorder_threshold=MAX_STOCK_QUANTITY,
            reorder_quantity=MAX_STOCK_QUANTITY,
            reference_price=MAX_UNIT_PRICE,
        )
    )
    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    assert dialog.quantity_label is not None
    assert dialog.quantity_label.text() == f"{MAX_STOCK_QUANTITY:,} 個"
    assert dialog.threshold_spin.value() == MAX_STOCK_QUANTITY
    assert dialog.reorder_quantity_edit.text() == str(MAX_STOCK_QUANTITY)
    assert dialog.reference_price_edit.text() == str(MAX_UNIT_PRICE)
    assert _ok_button(dialog).isEnabled()

    dialog.name_edit.setText("上限品目(改)")
    dialog._save()

    saved = dialog_context.inventory.get_item(item.id)
    assert saved is not None
    assert saved.name == "上限品目(改)"
    assert (
        saved.quantity,
        saved.reorder_threshold,
        saved.reorder_quantity,
        saved.reference_price,
    ) == (MAX_STOCK_QUANTITY, MAX_STOCK_QUANTITY, MAX_STOCK_QUANTITY, MAX_UNIT_PRICE)


def test_edit_dialog_shows_out_of_range_reference_price_without_rounding(
    qtbot, dialog_context, seeded_conn
) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="異常価格", category_id=1)
    )
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(
            "UPDATE items SET reference_price = ? WHERE id = ?", (MAX_UNIT_PRICE + 1, item.id)
        )

    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    assert dialog.reference_price_edit.text() == str(MAX_UNIT_PRICE + 1)
    assert not _ok_button(dialog).isEnabled()
    assert "参考価格" in dialog.error_label.text()
    dialog._save()
    assert dialog_context.inventory.get_item(item.id).reference_price == MAX_UNIT_PRICE + 1  # type: ignore[union-attr]


@pytest.mark.parametrize("threshold", [-1, MAX_STOCK_QUANTITY + 1])
def test_edit_dialog_blocks_and_preserves_out_of_range_threshold(
    qtbot, dialog_context, seeded_conn, threshold: int
) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="異常閾値", category_id=1)
    )
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(
            "UPDATE items SET reorder_threshold = ? WHERE id = ?", (threshold, item.id)
        )

    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    assert dialog.threshold_spin.value() == threshold
    assert not _ok_button(dialog).isEnabled()
    assert f"現在値: {threshold}" in dialog.error_label.text()
    dialog._save()
    assert dialog_context.inventory.get_item(item.id).reorder_threshold == threshold  # type: ignore[union-attr]
    assert dialog.result() == dialog.DialogCode.Rejected


def test_edit_dialog_enables_save_after_correcting_out_of_range_threshold(
    qtbot, dialog_context, seeded_conn
) -> None:
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="異常閾値", category_id=1)
    )
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(
            "UPDATE items SET reorder_threshold = ? WHERE id = ?",
            (MAX_STOCK_QUANTITY + 1, item.id),
        )

    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)
    assert not _ok_button(dialog).isEnabled()

    dialog.threshold_spin.setValue(MAX_STOCK_QUANTITY)

    assert _ok_button(dialog).isEnabled()
    assert "閾値が" not in dialog.error_label.text()
    dialog.name_edit.setText("閾値修正済み")
    dialog._save()
    saved = dialog_context.inventory.get_item(item.id)
    assert saved is not None
    assert saved.name == "閾値修正済み"
    assert saved.reorder_threshold == MAX_STOCK_QUANTITY


def test_edit_dialog_warns_when_threshold_exceeds_spinbox_integer_range(
    qtbot, dialog_context, seeded_conn
) -> None:
    threshold = 2**40
    item = dialog_context.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="巨大閾値", category_id=1)
    )
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(
            "UPDATE items SET reorder_threshold = ? WHERE id = ?", (threshold, item.id)
        )

    dialog = ItemDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    assert not dialog.threshold_spin.isEnabled()
    assert not _ok_button(dialog).isEnabled()
    assert f"現在値: {threshold}" in dialog.error_label.text()
    dialog._save()
    assert dialog_context.inventory.get_item(item.id).reorder_threshold == threshold  # type: ignore[union-attr]
