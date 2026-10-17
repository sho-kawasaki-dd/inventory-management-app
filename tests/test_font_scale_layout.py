from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QPushButton

from inventory_manager_mini.core.mobile_stocktake import encode_stocktake_payload
from inventory_manager_mini.core.models import (
    Category,
    MobileStocktakeEntry,
    MobileStocktakePayload,
    NewItem,
)
from inventory_manager_mini.core.services import PreparedRestore
from inventory_manager_mini.db.migrations import SCHEMA_VERSION
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.history_export_dialog import HistoryExportDialog
from inventory_manager_mini.ui.dialogs.item_dialog import ItemDialog
from inventory_manager_mini.ui.dialogs.master_dialog import MasterDialog, MasterTab
from inventory_manager_mini.ui.dialogs.mobile_stocktake_dialog import MobileStocktakeDialog
from inventory_manager_mini.ui.dialogs.restore_confirmation_dialog import RestoreConfirmationDialog
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from inventory_manager_mini.ui.display_settings import DisplaySettings, apply_display_settings
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.ui.widgets.category_picker import CategoryPickerDialog
from tests.conftest import (
    WORK_AREA_HEIGHT,
    WORK_AREA_WIDTH,
    assert_buttons_reachable,
    assert_fits_work_area,
    create_test_context,
)

SCALES = [100, 150]


@pytest.mark.skipif(os.environ.get("QT_QPA_PLATFORM") != "offscreen", reason="offscreen 専用")
def test_offscreen_matches_supported_work_area(qapp: QApplication) -> None:
    screen = qapp.primaryScreen()
    assert screen is not None
    available = screen.availableGeometry()
    assert available.width() == WORK_AREA_WIDTH
    assert available.height() == WORK_AREA_HEIGHT


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
def test_mobile_stocktake_dialog_tabs_fit_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, tmp_path: Path, scale: int
) -> None:
    items = [_create_item(dialog_context) for _ in range(12)]
    sheet = dialog_context.inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = encode_stocktake_payload(
        MobileStocktakePayload(
            sheet.session_id,
            sheet.client_id,
            tuple(
                MobileStocktakeEntry(item.id, 1 + index % 3, "長いメモ" * 20)
                for index, item in enumerate(items)
            ),
        )
    )
    apply_display_settings(DisplaySettings(None, scale))
    dialog = MobileStocktakeDialog(dialog_context)
    qtbot.addWidget(dialog)
    dialog.result_edit.setPlainText(result)
    dialog.preview_button.click()
    assert dialog.preview_table.rowCount() == len(items)
    dialog.show()
    qtbot.waitExposed(dialog)

    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        widget = dialog.tabs.currentWidget()
        assert widget is not None
        assert_fits_work_area(dialog)
        assert_buttons_reachable(dialog, *widget.findChildren(QPushButton))


@pytest.mark.parametrize("scale", SCALES)
def test_main_window_stocktake_banner_fits_work_area(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    tmp_path: Path,
    scale: int,
) -> None:
    _create_item(dialog_context)
    dialog_context.inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    apply_display_settings(DisplaySettings(None, scale))
    window = MainWindow(dialog_context)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)

    assert window.stocktake_banner_widget.isVisible()
    assert "総務" in window.stocktake_banner_label.text()
    assert_fits_work_area(window)
    assert_buttons_reachable(window, window.stocktake_banner_button)


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
@pytest.mark.parametrize("with_stocktakes", [False, True])
def test_restore_confirmation_fits_work_area(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scale: int,
    with_stocktakes: bool,
) -> None:
    if with_stocktakes:
        _create_item(dialog_context)
        dialog_context.inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    apply_display_settings(DisplaySettings(None, scale))
    window = MainWindow(dialog_context)
    qtbot.addWidget(window)
    source = tmp_path / ("長いフォルダ名" * 10) / "inventory_20261011_120000.db"
    restored_names = tuple(f"復元元の長いクライアント名{number}" * 2 for number in range(100))
    prepared = PreparedRestore(
        source,
        3,
        5000,
        100000,
        "2026-10-11 03:00:00",
        tempfile.TemporaryDirectory(),
        restored_names if with_stocktakes else (),
    )
    checked: list[str] = []

    def question(box: RestoreConfirmationDialog):
        qtbot.addWidget(box)
        box.show()
        qtbot.waitExposed(box)
        assert_fits_work_area(box)
        assert_buttons_reachable(
            box,
            box.button_box.button(QDialogButtonBox.StandardButton.Yes),
            box.button_box.button(QDialogButtonBox.StandardButton.No),
        )
        assert box.button_box.button(QDialogButtonBox.StandardButton.No).isDefault()
        if with_stocktakes:
            scrollbar = box.message_edit.verticalScrollBar()
            assert scrollbar.maximum() > 0
            scrollbar.setValue(scrollbar.maximum())
            assert_buttons_reachable(
                box,
                box.button_box.button(QDialogButtonBox.StandardButton.Yes),
                box.button_box.button(QDialogButtonBox.StandardButton.No),
            )
        checked.append(box.message_edit.toPlainText())
        box.close()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(RestoreConfirmationDialog, "exec", question)
    try:
        assert window._confirm_restore(source, prepared) is False
    finally:
        prepared.close()

    assert checked
    assert ("復元元の長いクライアント名0" in checked[0]) is with_stocktakes
    assert (restored_names[-1] in checked[0]) is with_stocktakes
    assert ("現在の棚卸手続き中クライアント: 総務" in checked[0]) is with_stocktakes


@pytest.mark.parametrize("action", ["yes", "no", "enter", "escape", "close"])
def test_restore_confirmation_requires_explicit_acceptance(qtbot, action: str) -> None:
    dialog = RestoreConfirmationDialog("復元しますか？")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    if action in ("yes", "no"):
        button = (
            QDialogButtonBox.StandardButton.Yes
            if action == "yes"
            else QDialogButtonBox.StandardButton.No
        )
        qtbot.mouseClick(dialog.button_box.button(button), Qt.MouseButton.LeftButton)
    elif action == "close":
        dialog.close()
    else:
        key = Qt.Key.Key_Return if action == "enter" else Qt.Key.Key_Escape
        qtbot.keyClick(dialog, key)

    assert not dialog.isVisible()
    expected = QDialog.DialogCode.Accepted if action == "yes" else QDialog.DialogCode.Rejected
    assert dialog.result() == expected
