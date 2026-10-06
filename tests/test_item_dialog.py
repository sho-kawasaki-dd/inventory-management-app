from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import ItemFilter, NewItem
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog
from inventory_manager_mini.ui.signals import DataBus
from inventory_manager_mini.ui.widgets.category_picker import CategoryPickerDialog


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
    _fill_required(dialog)

    assert dialog.unit_label.text() == "個"
    assert not dialog.initial_staff_combo.isEnabled()
    assert _ok_button(dialog).isEnabled()
    dialog.initial_quantity_spin.setValue(2)
    assert dialog.initial_staff_combo.isEnabled()
    assert not _ok_button(dialog).isEnabled()
    dialog.initial_staff_combo.setCurrentIndex(1)
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

    assert dialog.client_combo.count() == 2
    assert dialog.client_combo.currentText() == "総務 (無効)"
    assert dialog.client_combo.findData(3) == -1
    assert "変更する場合は有効なものを選択してください" in dialog.client_notice.text()
    assert dialog.purchaser_combo.count() == 2
    assert dialog.purchaser_combo.currentText() == "本部 (無効)"
    assert dialog.purchaser_combo.findData(3) == -1
    assert "変更する場合は有効なものを選択してください" in dialog.purchaser_notice.text()
    assert dialog.unit_label.text() == "個"
    assert not dialog.initial_quantity_row.isVisible()
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
