from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QTableWidget

from inventory_manager_mini.core.mobile_stocktake import (
    dump_stocktake_payload_json,
    encode_stocktake_payload,
)
from inventory_manager_mini.core.models import (
    Item,
    MobileStocktakeEntry,
    MobileStocktakePayload,
    NewItem,
)
from inventory_manager_mini.core.services import InventoryService, MasterService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs import mobile_stocktake_dialog as dialog_module
from inventory_manager_mini.ui.dialogs.mobile_stocktake_dialog import MobileStocktakeDialog
from tests.conftest import create_test_context

_SHEET_QUESTION = "棚卸シートの再作成"
_FOLDER_QUESTION = "保存先フォルダ"
_CANCEL_QUESTION = "棚卸の中止"


class _Dialogs:
    def __init__(self) -> None:
        self.questions: list[tuple[str, str, Any]] = []
        self.information: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.answers: dict[str, QMessageBox.StandardButton] = {}
        self.save_calls: list[tuple[Any, ...]] = []
        self.save_path: Path | None = None
        self.open_path: Path | None = None
        self.opened_urls: list[QUrl] = []


@pytest.fixture
def dialogs(monkeypatch: pytest.MonkeyPatch) -> _Dialogs:
    record = _Dialogs()

    def question(_parent, title, text, _buttons, default):
        record.questions.append((title, text, default))
        return record.answers.get(title, QMessageBox.StandardButton.No)

    def get_save_file_name(*args, **_kwargs):
        record.save_calls.append(args)
        return (str(record.save_path) if record.save_path is not None else "", "")

    def get_open_file_name(*_args, **_kwargs):
        return (str(record.open_path) if record.open_path is not None else "", "")

    monkeypatch.setattr(QMessageBox, "question", question)
    monkeypatch.setattr(
        QMessageBox, "information", lambda _p, title, text: record.information.append((title, text))
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _p, title, text: record.warnings.append((title, text))
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", get_save_file_name)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", get_open_file_name)
    monkeypatch.setattr(
        dialog_module.QDesktopServices,
        "openUrl",
        lambda url: record.opened_urls.append(url) or True,
    )
    return record


@pytest.fixture
def context(seeded_conn, inventory: InventoryService, master: MasterService, tmp_path: Path):
    return create_test_context(
        seeded_conn, inventory=inventory, master=master, db_path=tmp_path / "test.db"
    )


def _new_item(inventory: InventoryService, name: str, quantity: int) -> Item:
    return inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name=name,
            category_id=2,
            initial_quantity=quantity,
            initial_staff_id=1,
        )
    )


def _result_text(sheet, entries: tuple[MobileStocktakeEntry, ...]) -> str:
    return encode_stocktake_payload(
        MobileStocktakePayload(sheet.session_id, sheet.client_id, entries)
    )


def _open_dialog(qtbot, context: AppContext) -> MobileStocktakeDialog:
    dialog = MobileStocktakeDialog(context)
    qtbot.addWidget(dialog)
    return dialog


def _select_export(dialog: MobileStocktakeDialog, client_id: int, staff_id: int | None) -> None:
    dialog.client_combo.setCurrentIndex(dialog.client_combo.findData(client_id))
    dialog.export_staff_combo.setCurrentIndex(dialog.export_staff_combo.findData(staff_id))


def _track_changes(context: AppContext) -> list[bool]:
    changes: list[bool] = []
    context.data_bus.data_changed.connect(lambda: changes.append(True))
    return changes


def _cell(table: QTableWidget, row: int, column: int) -> str:
    cell = table.item(row, column)
    assert cell is not None
    return cell.text()


def _quantity(inventory: InventoryService, item: Item) -> int:
    current = inventory.get_item(item.id)
    assert current is not None
    return current.quantity


@pytest.fixture
def started(inventory: InventoryService, tmp_path: Path):
    unchanged = _new_item(inventory, "変更なし", 2)
    decreased = _new_item(inventory, "減少", 5)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "started.html", 1, 1)
    text = _result_text(
        sheet,
        (MobileStocktakeEntry(unchanged.id, 2), MobileStocktakeEntry(decreased.id, 3, "棚の下")),
    )
    return unchanged, decreased, sheet, text


def test_editing_result_after_preview_discards_confirmation(
    qtbot, context: AppContext, started
) -> None:
    *_items, text = started
    dialog = _open_dialog(qtbot, context)

    dialog.result_edit.setPlainText(text)
    dialog.preview_button.click()

    assert dialog._preview is not None
    assert dialog._preview_raw_text == text
    assert dialog.confirm_button.isEnabled()
    assert dialog.preview_table.rowCount() == 2

    dialog.result_edit.appendPlainText("変更")

    assert dialog._preview is None
    assert dialog._preview_raw_text is None
    assert not dialog.confirm_button.isEnabled()
    assert dialog.preview_table.rowCount() == 0


def test_export_creates_sheet_locks_client_and_reports_result(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, tmp_path: Path
) -> None:
    _new_item(inventory, "品目", 1)
    changes = _track_changes(context)
    dialog = _open_dialog(qtbot, context)
    _select_export(dialog, 1, 2)
    output = tmp_path / "out" / "sheet.html"
    output.parent.mkdir()
    dialogs.save_path = output

    dialog.export_button.click()

    assert output.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html>")
    stocktake = inventory.get_active_stocktake(1)
    assert stocktake is not None
    assert stocktake.staff_id == 2
    assert changes == [True]
    assert dialog.active_table.rowCount() == 1
    assert _cell(dialog.active_table, 0, 0) == "総務"
    assert _cell(dialog.active_table, 0, 1) == "担当B"
    assert "(棚卸中)" in dialog.client_combo.itemText(dialog.client_combo.findData(1))
    assert len(dialogs.information) == 1
    assert str(output) in dialogs.information[0][1]
    assert [(title, default) for title, _text, default in dialogs.questions] == [
        (_FOLDER_QUESTION, QMessageBox.StandardButton.No)
    ]
    assert dialogs.opened_urls == []


def test_export_suggests_unique_html_names_and_can_open_folder(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, tmp_path: Path
) -> None:
    _new_item(inventory, "品目", 1)
    dialog = _open_dialog(qtbot, context)
    _select_export(dialog, 1, 1)
    dialogs.answers[_FOLDER_QUESTION] = QMessageBox.StandardButton.Yes
    dialogs.answers[_SHEET_QUESTION] = QMessageBox.StandardButton.Yes
    dialogs.save_path = tmp_path / "first.html"

    dialog.export_button.click()
    dialogs.save_path = tmp_path / "second.html"
    dialog.export_button.click()

    first_name, second_name = (Path(call[2]).name for call in dialogs.save_calls)
    assert first_name.startswith("棚卸シート_総務_")
    assert first_name.endswith(".html")
    assert first_name != second_name
    assert dialogs.opened_urls[0] == QUrl.fromLocalFile(str(tmp_path))


def test_export_requires_client_and_staff_selection(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs
) -> None:
    _new_item(inventory, "品目", 1)
    dialog = _open_dialog(qtbot, context)
    _select_export(dialog, 1, None)

    dialog.export_button.click()

    assert dialogs.save_calls == []
    assert len(dialogs.warnings) == 1
    assert inventory.get_active_stocktake(1) is None


def test_export_declining_reissue_keeps_session_and_accepting_invalidates_old_sheet(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, tmp_path: Path
) -> None:
    item = _new_item(inventory, "品目", 2)
    old_sheet = inventory.export_mobile_stocktake_html(tmp_path / "old.html", 1, 1)
    old_text = _result_text(old_sheet, (MobileStocktakeEntry(item.id, 3),))
    dialog = _open_dialog(qtbot, context)
    _select_export(dialog, 1, 1)
    dialogs.save_path = tmp_path / "new.html"

    dialog.export_button.click()

    assert [(title, default) for title, _text, default in dialogs.questions] == [
        (_SHEET_QUESTION, QMessageBox.StandardButton.No)
    ]
    assert "前のシートは無効" in dialogs.questions[0][1]
    assert dialogs.save_calls == []
    assert not (tmp_path / "new.html").exists()
    active = inventory.get_active_stocktake(1)
    assert active is not None and active.session_id == old_sheet.session_id

    dialogs.answers[_SHEET_QUESTION] = QMessageBox.StandardButton.Yes
    dialog.export_button.click()

    active = inventory.get_active_stocktake(1)
    assert active is not None and active.session_id != old_sheet.session_id
    assert (tmp_path / "new.html").exists()
    dialog.result_edit.setPlainText(old_text)
    dialog.preview_button.click()
    assert dialog._preview is None
    assert not dialog.confirm_button.isEnabled()
    assert any("無効" in text for _title, text in dialogs.warnings)


def test_export_to_existing_path_keeps_file_and_leaves_no_lock(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, tmp_path: Path
) -> None:
    _new_item(inventory, "品目", 1)
    existing = tmp_path / "existing.html"
    existing.write_text("既存の内容", encoding="utf-8")
    changes = _track_changes(context)
    dialog = _open_dialog(qtbot, context)
    _select_export(dialog, 1, 1)
    dialogs.save_path = existing

    dialog.export_button.click()

    assert existing.read_text(encoding="utf-8") == "既存の内容"
    assert inventory.get_active_stocktake(1) is None
    assert len(dialogs.warnings) == 1
    assert dialogs.information == []
    assert changes == []


def test_cancel_stocktake_requires_confirmation_defaulting_to_no(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, started
) -> None:
    changes = _track_changes(context)
    dialog = _open_dialog(qtbot, context)
    assert not dialog.cancel_stocktake_button.isEnabled()
    dialog.active_table.selectRow(0)
    assert dialog.cancel_stocktake_button.isEnabled()

    dialog.cancel_stocktake_button.click()

    title, text, default = dialogs.questions[0]
    assert (title, default) == (_CANCEL_QUESTION, QMessageBox.StandardButton.No)
    assert "総務の棚卸を中止します" in text
    assert inventory.get_active_stocktake(1) is not None
    assert changes == []

    dialogs.answers[_CANCEL_QUESTION] = QMessageBox.StandardButton.Yes
    dialog.cancel_stocktake_button.click()

    assert inventory.get_active_stocktake(1) is None
    assert dialog.active_table.rowCount() == 0
    assert changes == [True]
    assert not dialog.cancel_stocktake_button.isEnabled()


def test_go_to_import_tab_button_switches_tab(qtbot, context: AppContext) -> None:
    dialog = _open_dialog(qtbot, context)

    dialog.go_to_import_button.click()

    assert dialog.tabs.currentIndex() == 1


def test_paste_from_clipboard_previews_and_confirms_all_items(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, started
) -> None:
    unchanged, decreased, _sheet, text = started
    changes = _track_changes(context)
    dialog = _open_dialog(qtbot, context)
    QApplication.clipboard().setText(text)

    dialog.paste_button.click()

    assert dialog._preview is not None
    assert "総務" in dialog.preview_status_label.text()
    assert dialog.preview_summary_label.text() == (
        "対象 2 件 / 変動なし 1 件 / 増加 0 件 / 減少 1 件"
    )
    assert dialog.preview_table.rowCount() == 2
    assert dialog.confirm_staff_combo.currentData() == 1
    assert dialog.confirm_button.isEnabled()
    dialog.differences_only_checkbox.setChecked(True)
    assert dialog.preview_table.rowCount() == 1
    assert _cell(dialog.preview_table, 0, 1) == "減少"
    assert _cell(dialog.preview_table, 0, 5) == "-2"
    assert _cell(dialog.preview_table, 0, 6) == "棚の下"
    first_cell = dialog.preview_table.item(0, 0)
    assert first_cell is not None
    assert not first_cell.flags() & Qt.ItemFlag.ItemIsEditable
    dialog.differences_only_checkbox.setChecked(False)

    dialog.confirm_staff_combo.setCurrentIndex(dialog.confirm_staff_combo.findData(2))
    dialog.confirm_button.click()

    assert [_quantity(inventory, unchanged), _quantity(inventory, decreased)] == [2, 3]
    movement = inventory.list_history(decreased.id)[-1]
    assert (movement.staff_id, movement.delta, movement.note) == (2, -2, "棚の下")
    assert inventory.get_active_stocktake(1) is None
    assert changes == [True]
    assert len(dialogs.information) == 1
    assert "2 件確定" in dialogs.information[0][1]
    assert dialog.result() == dialog.DialogCode.Accepted


def test_load_json_file_previews_result(
    qtbot, context: AppContext, dialogs: _Dialogs, started, tmp_path: Path
) -> None:
    unchanged, decreased, sheet, _text = started
    payload = MobileStocktakePayload(
        sheet.session_id,
        sheet.client_id,
        (MobileStocktakeEntry(unchanged.id, 4), MobileStocktakeEntry(decreased.id, 5)),
    )
    result_file = tmp_path / "結果.json"
    result_file.write_text(dump_stocktake_payload_json(payload), encoding="utf-8")
    dialogs.open_path = result_file
    dialog = _open_dialog(qtbot, context)

    dialog.load_file_button.click()

    assert dialog._preview is not None
    assert dialog.preview_summary_label.text() == (
        "対象 2 件 / 変動なし 1 件 / 増加 1 件 / 減少 0 件"
    )
    assert dialog.confirm_button.isEnabled()


def test_unreadable_result_file_discards_previous_preview(
    qtbot, context: AppContext, dialogs: _Dialogs, started, tmp_path: Path
) -> None:
    *_items, text = started
    dialog = _open_dialog(qtbot, context)
    dialog.result_edit.setPlainText(text)
    dialog.preview_button.click()
    assert dialog.confirm_button.isEnabled()
    dialogs.open_path = tmp_path / "missing.json"

    dialog.load_file_button.click()

    assert dialog._preview is None
    assert dialog._preview_raw_text is None
    assert not dialog.confirm_button.isEnabled()
    assert dialog.preview_table.rowCount() == 0
    assert len(dialogs.warnings) == 1


@pytest.mark.parametrize("source", ["clipboard", "file_cancel", "file_invalid"])
def test_loading_another_input_discards_previous_preview_even_when_it_fails(
    qtbot, context: AppContext, dialogs: _Dialogs, started, tmp_path: Path, source: str
) -> None:
    *_items, text = started
    dialog = _open_dialog(qtbot, context)
    dialog.result_edit.setPlainText(text)
    dialog.preview_button.click()
    assert dialog.confirm_button.isEnabled()
    if source == "clipboard":
        QApplication.clipboard().setText("IMM1:壊れた結果:END")
        dialog.paste_button.click()
    elif source == "file_cancel":
        dialogs.open_path = None
        dialog.load_file_button.click()
    else:
        invalid = tmp_path / "invalid.json"
        invalid.write_text("{}", encoding="utf-8")
        dialogs.open_path = invalid
        dialog.load_file_button.click()

    assert dialog._preview is None
    assert dialog._preview_raw_text is None
    assert dialog.preview_table.rowCount() == 0
    assert dialog.preview_summary_label.text() == ""
    assert not dialog.confirm_button.isEnabled()


def test_confirm_uses_text_of_last_successful_preview(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, started, monkeypatch
) -> None:
    *_items, text = started
    dialog = _open_dialog(qtbot, context)
    dialog.result_edit.setPlainText(text)
    dialog.preview_button.click()
    dialog.result_edit.blockSignals(True)
    dialog.result_edit.setPlainText("IMM1:別の入力:END")
    dialog.result_edit.blockSignals(False)
    received: list[tuple[str, int]] = []
    original = inventory.complete_mobile_stocktake

    def record(raw_text: str, staff_id: int):
        received.append((raw_text, staff_id))
        return original(raw_text, staff_id)

    monkeypatch.setattr(inventory, "complete_mobile_stocktake", record)

    dialog.confirm_button.click()

    assert received == [(text, 1)]


def test_changed_input_requires_new_preview_before_confirming(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, started
) -> None:
    unchanged, decreased, sheet, text = started
    changed = _result_text(
        sheet,
        (MobileStocktakeEntry(unchanged.id, 2), MobileStocktakeEntry(decreased.id, 4, "変更後")),
    )
    dialog = _open_dialog(qtbot, context)
    dialog.result_edit.setPlainText(text)
    dialog.preview_button.click()
    assert dialog.confirm_button.isEnabled()

    dialog.result_edit.setPlainText(changed)

    assert not dialog.confirm_button.isEnabled()
    dialog.confirm_button.click()
    assert _quantity(inventory, decreased) == 5
    assert inventory.get_active_stocktake(1) is not None

    dialog.preview_button.click()

    assert dialog._preview_raw_text == changed
    assert dialog.confirm_button.isEnabled()
    dialog.confirm_button.click()
    assert _quantity(inventory, decreased) == 4
    assert inventory.list_history(decreased.id)[-1].note == "変更後"


def test_confirm_requires_staff_and_leaves_state_when_service_rejects(
    qtbot, context: AppContext, inventory: InventoryService, dialogs: _Dialogs, started
) -> None:
    _unchanged, decreased, _sheet, text = started
    context.master.deactivate_staff(1)
    changes = _track_changes(context)
    dialog = _open_dialog(qtbot, context)
    dialog.result_edit.setPlainText(text)
    dialog.preview_button.click()

    assert dialog._preview is not None
    assert dialog.confirm_staff_combo.currentData() is None
    assert not dialog.confirm_button.isEnabled()

    dialog.confirm_staff_combo.setCurrentIndex(dialog.confirm_staff_combo.findData(2))
    assert dialog.confirm_button.isEnabled()
    context.master.deactivate_staff(2)
    dialog.confirm_button.click()

    assert len(dialogs.warnings) == 1
    assert dialogs.information == []
    assert changes == []
    assert _quantity(inventory, decreased) == 5
    assert inventory.get_active_stocktake(1) is not None
    assert dialog.result() != dialog.DialogCode.Accepted


def test_empty_input_cannot_be_previewed(qtbot, context: AppContext, dialogs: _Dialogs) -> None:
    dialog = _open_dialog(qtbot, context)

    dialog.preview_button.click()

    assert dialog._preview is None
    assert not dialog.confirm_button.isEnabled()
    assert dialogs.warnings == []
