from __future__ import annotations

import logging

from PySide6.QtGui import QFontDatabase, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QMessageBox,
    QVBoxLayout,
)

from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.display_settings import (
    ALLOWED_FONT_SCALES,
    DEFAULT_FONT_SCALE_PERCENT,
    DisplaySettings,
    apply_display_settings,
    save_display_settings,
)
from inventory_manager_mini.ui.error_handling import run_guarded

logger = logging.getLogger(__name__)

DEFAULT_FISCAL_YEAR_START_MONTH = 4
OS_DEFAULT_FONT_LABEL = "OS 既定"


class SettingsDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        display_settings: DisplaySettings,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.display_settings = display_settings
        self.setWindowTitle("設定")

        succeeded, month = run_guarded(self, context.settings.get_fiscal_year_start_month)
        self._saved_month = month if succeeded and month is not None else None
        self._month_saved_in_dialog = False
        self._build_ui()
        self._select_month(
            DEFAULT_FISCAL_YEAR_START_MONTH if self._saved_month is None else self._saved_month
        )
        self._select_display_settings(display_settings)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        form = QFormLayout()

        self.month_combo = QComboBox(self)
        for month in range(1, 13):
            self.month_combo.addItem(f"{month} 月", month)
        form.addRow("年度開始月", self.month_combo)

        self.font_combo = QComboBox(self)
        self.font_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.font_combo.setMinimumContentsLength(20)
        self.font_combo.addItem(OS_DEFAULT_FONT_LABEL, None)
        for family in QFontDatabase.families():
            self.font_combo.addItem(family, family)
        form.addRow("フォント", self.font_combo)

        self.scale_combo = QComboBox(self)
        for percent in ALLOWED_FONT_SCALES:
            self.scale_combo.addItem(f"{percent}%", percent)
        form.addRow("文字サイズ", self.scale_combo)
        outer.addLayout(form)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults,
            parent=self,
        )
        self.reset_button = self.button_box.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        self.reset_button.setText("既定に戻す")
        self.reset_button.clicked.connect(self._reset_display_choices)
        self.button_box.accepted.connect(self._save)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)

    def _select_month(self, month: int) -> None:
        index = self.month_combo.findData(month)
        self.month_combo.setCurrentIndex(max(index, 0))

    def _select_display_settings(self, settings: DisplaySettings) -> None:
        font_index = self.font_combo.findData(settings.font_family)
        self.font_combo.setCurrentIndex(font_index if font_index >= 0 else 0)
        scale_index = self.scale_combo.findData(settings.font_scale_percent)
        if scale_index < 0:
            scale_index = self.scale_combo.findData(DEFAULT_FONT_SCALE_PERCENT)
        self.scale_combo.setCurrentIndex(scale_index)

    def _reset_display_choices(self) -> None:
        self._select_display_settings(DisplaySettings())

    def _save(self) -> None:
        month = self.month_combo.currentData()
        if month != self._saved_month:
            succeeded, _result = run_guarded(
                self, lambda: self.context.settings.set_fiscal_year_start_month(month)
            )
            if not succeeded:
                return
            self._saved_month = month
            self._month_saved_in_dialog = True
            self.context.data_bus.data_changed.emit()

        new_settings = DisplaySettings(
            font_family=self.font_combo.currentData(),
            font_scale_percent=self.scale_combo.currentData(),
        )
        try:
            save_display_settings(self.context.paths.ui_settings_path, new_settings)
        except OSError as error:
            logger.exception("UI 表示設定を保存できませんでした")
            message = "フォントと文字サイズを保存できませんでした。"
            if self._month_saved_in_dialog:
                message += "\n年度開始月は保存済みです。"
            QMessageBox.warning(self, "設定を保存できません", f"{message}\n原因: {error}")
            return
        apply_display_settings(new_settings)
        self.display_settings = new_settings
        self.accept()
