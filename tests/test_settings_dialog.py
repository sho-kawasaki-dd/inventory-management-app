from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.ui import display_settings
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs import settings_dialog as settings_dialog_module
from inventory_manager_mini.ui.dialogs.settings_dialog import OS_DEFAULT_FONT_LABEL, SettingsDialog
from inventory_manager_mini.ui.display_settings import (
    DisplaySettings,
    apply_display_settings,
    capture_base_font,
)
from inventory_manager_mini.ui.main_window import MainWindow
from tests.conftest import create_test_context


@pytest.fixture
def restore_app_font(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[QFont]:
    original = QFont(qapp.font())
    monkeypatch.setattr(display_settings, "_base_font", None)
    yield original
    QApplication.setFont(original)


@pytest.fixture
def dialog_context(seeded_conn, inventory, master, settings, tmp_path: Path) -> AppContext:
    return create_test_context(
        seeded_conn,
        inventory=inventory,
        master=master,
        settings=settings,
        db_path=tmp_path / "test.db",
        schema_version=1,
    )


@pytest.fixture
def warnings(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: calls.append(args) or QMessageBox.StandardButton.Ok,
    )
    return calls


def _open(qtbot, context: AppContext, settings: DisplaySettings | None = None) -> SettingsDialog:
    dialog = SettingsDialog(context, settings or DisplaySettings(), parent=None)
    qtbot.addWidget(dialog)
    return dialog


def _select_font(dialog: SettingsDialog, family: str | None) -> None:
    dialog.font_combo.setCurrentIndex(dialog.font_combo.findData(family))


def _select_scale(dialog: SettingsDialog, percent: int) -> None:
    dialog.scale_combo.setCurrentIndex(dialog.scale_combo.findData(percent))


def _saved_json(context: AppContext) -> dict[str, object]:
    return json.loads(context.paths.ui_settings_path.read_text(encoding="utf-8"))


def test_initial_values_show_current_settings(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    family = QFontDatabase.families()[-1]
    dialog_context.settings.set_fiscal_year_start_month(10)

    dialog = _open(qtbot, dialog_context, DisplaySettings(family, 125))

    assert dialog.month_combo.currentData() == 10
    assert [dialog.month_combo.itemData(i) for i in range(12)] == list(range(1, 13))
    assert dialog.font_combo.itemText(0) == OS_DEFAULT_FONT_LABEL
    assert dialog.font_combo.currentData() == family
    assert [dialog.scale_combo.itemText(i) for i in range(4)] == ["90%", "100%", "125%", "150%"]
    assert dialog.scale_combo.currentData() == 125


def test_unknown_font_and_invalid_scale_fall_back_to_defaults(qtbot, dialog_context) -> None:
    dialog = _open(qtbot, dialog_context, DisplaySettings("存在しないフォント-xyz", 110))

    assert dialog.font_combo.currentData() is None
    assert dialog.font_combo.currentText() == OS_DEFAULT_FONT_LABEL
    assert dialog.scale_combo.currentData() == 100


def test_fiscal_year_month_is_saved_and_reloaded_and_emits_change(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    changes: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog = _open(qtbot, dialog_context)
    dialog.month_combo.setCurrentIndex(dialog.month_combo.findData(7))

    dialog._save()

    assert dialog_context.settings.get_fiscal_year_start_month() == 7
    assert changes == [True]
    assert dialog.result() == dialog.DialogCode.Accepted
    assert _open(qtbot, dialog_context).month_combo.currentData() == 7


def test_unchanged_month_does_not_emit_change(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    changes: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog = _open(qtbot, dialog_context)

    dialog._save()

    assert changes == []


def test_font_and_scale_are_saved_and_applied_immediately(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    base_size = capture_base_font().pointSizeF()
    family = QFontDatabase.families()[-1]
    dialog = _open(qtbot, dialog_context)
    _select_font(dialog, family)
    _select_scale(dialog, 150)

    dialog._save()

    assert _saved_json(dialog_context) == {"font_family": family, "font_scale_percent": 150}
    assert dialog.display_settings == DisplaySettings(family, 150)
    applied = QApplication.font()
    assert applied.family() == family
    if base_size > 0:
        assert applied.pointSizeF() == pytest.approx(base_size * 1.5)


def test_reset_only_resets_font_and_scale_not_month(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    dialog_context.settings.set_fiscal_year_start_month(9)
    dialog = _open(qtbot, dialog_context, DisplaySettings(QFontDatabase.families()[-1], 150))
    dialog.month_combo.setCurrentIndex(dialog.month_combo.findData(2))

    dialog.reset_button.click()

    assert dialog.font_combo.currentData() is None
    assert dialog.scale_combo.currentData() == 100
    assert dialog.month_combo.currentData() == 2
    assert dialog.reset_button.text() == "既定に戻す"


def test_reset_then_cancel_changes_nothing(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    apply_display_settings(DisplaySettings(None, 150))
    font_before = QFont(QApplication.font())
    dialog = _open(qtbot, dialog_context, DisplaySettings(QFontDatabase.families()[-1], 150))

    dialog.reset_button.click()
    dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel).click()

    assert dialog.result() == dialog.DialogCode.Rejected
    assert not dialog_context.paths.ui_settings_path.exists()
    assert QApplication.font() == font_before
    assert dialog_context.settings.get_fiscal_year_start_month() == 4


def test_reset_then_ok_saves_and_applies_defaults(
    qtbot, dialog_context: AppContext, restore_app_font: QFont
) -> None:
    base_size = capture_base_font().pointSizeF()
    apply_display_settings(DisplaySettings(None, 150))
    dialog = _open(qtbot, dialog_context, DisplaySettings(QFontDatabase.families()[-1], 150))

    dialog.reset_button.click()
    dialog.button_box.button(QDialogButtonBox.StandardButton.Ok).click()

    assert _saved_json(dialog_context) == {"font_family": None, "font_scale_percent": 100}
    assert dialog.result() == dialog.DialogCode.Accepted
    if base_size > 0:
        assert QApplication.font().pointSizeF() == pytest.approx(base_size)


def test_display_save_failure_after_month_saved_reports_month_and_keeps_dialog_open(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[tuple[object, ...]],
) -> None:
    def fail(path: Path, settings: DisplaySettings) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(settings_dialog_module, "save_display_settings", fail)
    font_before = QFont(QApplication.font())
    dialog = _open(qtbot, dialog_context)
    dialog.month_combo.setCurrentIndex(dialog.month_combo.findData(6))
    _select_scale(dialog, 150)

    dialog._save()

    assert len(warnings) == 1
    message = str(warnings[0][2])
    assert "保存できません" in str(warnings[0][1])
    assert "年度開始月は保存済み" in message
    assert "denied" in message
    assert dialog_context.settings.get_fiscal_year_start_month() == 6
    assert dialog.result() != dialog.DialogCode.Accepted
    assert QApplication.font() == font_before


def test_display_save_failure_without_month_change_does_not_claim_month_saved(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[tuple[object, ...]],
) -> None:
    def fail(path: Path, settings: DisplaySettings) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(settings_dialog_module, "save_display_settings", fail)
    dialog = _open(qtbot, dialog_context)
    _select_scale(dialog, 90)

    dialog._save()

    assert len(warnings) == 1
    assert "年度開始月" not in str(warnings[0][2])
    assert "disk full" in str(warnings[0][2])


def test_month_save_failure_does_not_write_display_settings(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
    warnings: list[tuple[object, ...]],
) -> None:
    def fail(month: int) -> None:
        raise ValidationError("年度開始月を保存できません")

    monkeypatch.setattr(dialog_context.settings, "set_fiscal_year_start_month", fail)
    changes: list[bool] = []
    dialog_context.data_bus.data_changed.connect(lambda: changes.append(True))
    dialog = _open(qtbot, dialog_context)
    dialog.month_combo.setCurrentIndex(dialog.month_combo.findData(8))
    _select_scale(dialog, 150)

    dialog._save()

    assert warnings and warnings[0][2] == "年度開始月を保存できません"
    assert not dialog_context.paths.ui_settings_path.exists()
    assert changes == []
    assert dialog.result() != dialog.DialogCode.Accepted


@pytest.mark.parametrize("scale", [100, 150])
def test_dialog_fits_1366x768_work_area(
    qtbot, dialog_context: AppContext, restore_app_font: QFont, scale: int
) -> None:
    apply_display_settings(DisplaySettings(None, scale))
    dialog = _open(qtbot, dialog_context)

    dialog.show()
    qtbot.waitExposed(dialog)

    frame = dialog.frameGeometry()
    assert dialog.screen().availableGeometry().contains(frame)
    assert frame.width() <= 1366
    assert frame.height() <= 768 - 40
    for button in (
        dialog.button_box.button(QDialogButtonBox.StandardButton.Ok),
        dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel),
        dialog.reset_button,
    ):
        assert button.isVisible()
        assert dialog.rect().contains(button.geometry())


def test_main_window_tools_menu_opens_dialog_and_applies_font_to_open_window(
    qtbot,
    dialog_context: AppContext,
    restore_app_font: QFont,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_size = capture_base_font().pointSizeF()
    window = MainWindow(dialog_context)
    qtbot.addWidget(window)
    opened: list[SettingsDialog] = []

    def save_on_exec(dialog: SettingsDialog) -> int:
        opened.append(dialog)
        _select_scale(dialog, 150)
        dialog._save()
        return dialog.result()

    monkeypatch.setattr(SettingsDialog, "exec", save_on_exec)
    tools_menu_titles = [action.text() for action in window.menuBar().actions()]

    window.settings_action.trigger()

    assert "ツール" in tools_menu_titles
    assert window.settings_action.text() == "設定"
    assert len(opened) == 1
    assert opened[0].parent() is window
    assert _saved_json(dialog_context)["font_scale_percent"] == 150
    if base_size > 0:
        # アプリフォントの変更は既存ウィジェットへイベントループ経由で伝播する
        qtbot.waitUntil(
            lambda: window.font().pointSizeF() == pytest.approx(base_size * 1.5), timeout=2000
        )
