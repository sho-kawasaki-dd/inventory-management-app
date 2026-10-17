from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox, QPushButton

from inventory_manager_mini.core.models import Category, NewItem
from inventory_manager_mini.core.services import PreparedRestore
from inventory_manager_mini.db.migrations import SCHEMA_VERSION
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.history_export_dialog import HistoryExportDialog
from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog
from inventory_manager_mini.ui.dialogs.master_dialog import MasterDialog, MasterTab
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from inventory_manager_mini.ui.display_settings import DisplaySettings, apply_display_settings
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.ui.widgets.category_picker import CategoryPickerDialog
from tests.conftest import assert_buttons_reachable, assert_fits_work_area, create_test_context

SCALES = [100, 150]


@pytest.fixture
def dialog_context(seeded_conn, inventory, master, settings, tmp_path: Path) -> AppContext:
    return create_test_context(
        seeded_conn,
        inventory=inventory,
        master=master,
        settings=settings,
        db_path=tmp_path / "test.db",
        schema_version=SCHEMA_VERSION,
    )


def _create_item(context: AppContext):
    return context.inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="文字サイズ検証品目",
            category_id=1,
            initial_quantity=2,
            initial_staff_id=1,
        )
    )


def _ok_cancel(button_box: QDialogButtonBox) -> tuple[QPushButton, QPushButton]:
    ok = button_box.button(QDialogButtonBox.StandardButton.Ok)
    cancel = button_box.button(QDialogButtonBox.StandardButton.Cancel)
    return ok, cancel


@pytest.mark.parametrize("scale", SCALES)
def test_new_dialogs_follow_application_font_scale(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int
) -> None:
    base_size = restore_app_font.pointSizeF()
    apply_display_settings(DisplaySettings(None, scale))
    dialog = MasterDialog(dialog_context, MasterTab.CLIENT)
    qtbot.addWidget(dialog)

    if base_size > 0:
        assert dialog.font().pointSizeF() == pytest.approx(base_size * scale / 100)
    else:
        assert dialog.font().pixelSize() == QApplication.font().pixelSize()


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("editing", [False, True])
def test_item_dialog_fits_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int, editing: bool
) -> None:
    item_id = _create_item(dialog_context).id if editing else None
    apply_display_settings(DisplaySettings(None, scale))
    dialog = ItemDialog(dialog_context, item_id)
    qtbot.addWidget(dialog)
    dialog.client_notice.setText("クライアントの注意" * 100)
    dialog.purchaser_notice.setText("発注主体の注意" * 100)
    dialog.error_label.setText("入力エラー" * 100)

    dialog.show()
    qtbot.waitExposed(dialog)

    assert_fits_work_area(dialog)
    assert_buttons_reachable(dialog, *_ok_cancel(dialog.button_box))
    assert dialog.scroll_area.verticalScrollBar().maximum() > 0


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("tab", list(MasterTab))
def test_master_dialog_tabs_fit_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int, tab: MasterTab
) -> None:
    apply_display_settings(DisplaySettings(None, scale))
    dialog = MasterDialog(dialog_context, tab)
    qtbot.addWidget(dialog)

    dialog.show()
    qtbot.waitExposed(dialog)

    current = dialog.tabs.currentWidget()
    assert current is not None
    buttons = current.findChildren(QPushButton)
    assert buttons
    assert_fits_work_area(dialog)
    assert_buttons_reachable(dialog, *buttons)


@pytest.mark.parametrize("scale", SCALES)
def test_master_dialog_all_tabs_fit_together(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int
) -> None:
    apply_display_settings(DisplaySettings(None, scale))
    dialog = MasterDialog(dialog_context, MasterTab.CLIENT)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        widget = dialog.tabs.currentWidget()
        assert widget is not None
        assert_fits_work_area(dialog)
        assert_buttons_reachable(dialog, *widget.findChildren(QPushButton))


@pytest.mark.parametrize("scale", SCALES)
def test_reversal_dialog_fits_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int
) -> None:
    item = _create_item(dialog_context)
    movement = dialog_context.inventory.list_history(item.id)[0]
    apply_display_settings(DisplaySettings(None, scale))
    dialog = ReversalDialog(dialog_context, movement)
    qtbot.addWidget(dialog)
    dialog.used_for_label.setText("長い使用先" * 100)
    dialog.error_label.setText("長いエラー" * 100)

    dialog.show()
    qtbot.waitExposed(dialog)

    assert_fits_work_area(dialog)
    assert_buttons_reachable(dialog, *_ok_cancel(dialog.button_box))


@pytest.mark.parametrize("scale", SCALES)
def test_history_dialog_fits_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int
) -> None:
    item = _create_item(dialog_context)
    apply_display_settings(DisplaySettings(None, scale))
    dialog = HistoryDialog(dialog_context, item.id)
    qtbot.addWidget(dialog)

    dialog.show()
    qtbot.waitExposed(dialog)

    assert_fits_work_area(dialog)
    assert_buttons_reachable(
        dialog,
        dialog.prev_page_button,
        dialog.next_page_button,
        dialog.export_button,
        dialog.reverse_button,
        dialog.close_button,
    )


@pytest.mark.parametrize("scale", SCALES)
def test_history_export_dialog_fits_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int
) -> None:
    apply_display_settings(DisplaySettings(None, scale))
    dialog = HistoryExportDialog(dialog_context)
    qtbot.addWidget(dialog)

    dialog.show()
    qtbot.waitExposed(dialog)

    assert_fits_work_area(dialog)
    assert_buttons_reachable(dialog, *_ok_cancel(dialog.button_box))


@pytest.mark.parametrize("scale", SCALES)
def test_category_picker_dialog_fits_work_area(qtbot, restore_app_font: QFont, scale: int) -> None:
    categories = [Category(1, None, "親カテゴリ" * 10, "PA", 1)]
    categories += [
        Category(index, 1, f"子カテゴリ{index}" * 5, f"C{index}", 1) for index in range(2, 60)
    ]
    apply_display_settings(DisplaySettings(None, scale))
    dialog = CategoryPickerDialog(categories, "(すべて)", 2)
    qtbot.addWidget(dialog)

    dialog.show()
    qtbot.waitExposed(dialog)

    assert_fits_work_area(dialog)
    assert_buttons_reachable(dialog, *_ok_cancel(dialog.buttons))


@pytest.mark.parametrize("scale", SCALES)
def test_restore_confirmation_fits_work_area(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scale: int,
) -> None:
    apply_display_settings(DisplaySettings(None, scale))
    window = MainWindow(dialog_context)
    qtbot.addWidget(window)
    source = tmp_path / ("長いフォルダ名" * 10) / "inventory_20261011_120000.db"
    prepared = PreparedRestore(
        source, 3, 5000, 100000, "2026-10-11 03:00:00", tempfile.TemporaryDirectory()
    )
    checked: list[str] = []

    def question(parent, title, text, buttons, default):
        box = QMessageBox(QMessageBox.Icon.Question, title, text, buttons, parent)
        box.setDefaultButton(default)
        qtbot.addWidget(box)
        box.show()
        qtbot.waitExposed(box)
        assert_fits_work_area(box)
        assert_buttons_reachable(
            box,
            box.button(QMessageBox.StandardButton.Yes),
            box.button(QMessageBox.StandardButton.No),
        )
        checked.append(text)
        box.close()
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)
    try:
        assert window._confirm_restore(source, prepared) is False
    finally:
        prepared.close()

    assert checked
