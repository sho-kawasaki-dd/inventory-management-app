from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QWidget

from inventory_manager_mini.core.models import HistoryPeriod, HistoryPeriodKind
from inventory_manager_mini.ui.context import AppContext


class HistoryPeriodSelector(QWidget):
    periodChanged = Signal()

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context

        self.period_kind_combo = QComboBox(self)
        self.period_kind_combo.addItem("全期間", HistoryPeriodKind.ALL)
        self.period_kind_combo.addItem("年次", HistoryPeriodKind.ANNUAL)
        self.period_kind_combo.addItem("月次", HistoryPeriodKind.MONTHLY)

        self.year_combo = QComboBox(self)
        for year in context.reports.available_fiscal_years():
            self.year_combo.addItem(f"{year}年度", year)
        current_year_index = self.year_combo.findData(context.reports.current_fiscal_year())
        self.year_combo.setCurrentIndex(max(current_year_index, 0))

        self.month_combo = QComboBox(self)
        self._fill_months(context.reports.current_local_month())

        self._update_enabled()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for label, combo in (
            ("期間", self.period_kind_combo),
            ("年度", self.year_combo),
            ("月", self.month_combo),
        ):
            layout.addWidget(QLabel(label, self))
            layout.addWidget(combo)
        layout.addStretch(1)

        self.period_kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        self.year_combo.currentIndexChanged.connect(self._on_year_changed)
        self.month_combo.currentIndexChanged.connect(self._on_month_changed)

    def period(self) -> HistoryPeriod:
        kind = self._kind()
        if kind is HistoryPeriodKind.ANNUAL:
            return HistoryPeriod(kind, self.year_combo.currentData())
        if kind is HistoryPeriodKind.MONTHLY:
            return HistoryPeriod(
                kind, self.year_combo.currentData(), self.month_combo.currentData()
            )
        return HistoryPeriod()

    def _fill_months(self, selected_month: int | None) -> None:
        fiscal_year = self.year_combo.currentData()
        with QSignalBlocker(self.month_combo):
            self.month_combo.clear()
            if fiscal_year is None:
                return
            for month, label in self.context.reports.fiscal_year_months(fiscal_year):
                self.month_combo.addItem(label, month)
            index = self.month_combo.findData(selected_month)
            self.month_combo.setCurrentIndex(max(index, 0))

    def _kind(self) -> HistoryPeriodKind:
        return HistoryPeriodKind(self.period_kind_combo.currentData())

    def _update_enabled(self) -> None:
        kind = self._kind()
        self.year_combo.setEnabled(kind is not HistoryPeriodKind.ALL)
        self.month_combo.setEnabled(kind is HistoryPeriodKind.MONTHLY)

    def _on_kind_changed(self, _index: int) -> None:
        self._update_enabled()
        self.periodChanged.emit()

    def _on_year_changed(self, _index: int) -> None:
        self._fill_months(self.month_combo.currentData())
        self.periodChanged.emit()

    def _on_month_changed(self, _index: int) -> None:
        self.periodChanged.emit()
