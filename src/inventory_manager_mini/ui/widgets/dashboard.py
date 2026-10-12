from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import shiboken6
from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QBarSet, QChart, QChartView, QValueAxis
from PySide6.QtCore import QEvent, QIODevice, QObject, QSaveFile, Qt, QTimer
from PySide6.QtGui import QCursor, QFocusEvent, QHideEvent, QPainter, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.errors import FileOutputError
from inventory_manager_mini.core.models import (
    Category,
    Client,
    DashboardResult,
    DashboardRow,
    GroupAxis,
    ItemChoice,
    PeriodKind,
    Purchaser,
)
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.csv_export import export_csv_with_dialog, export_file_with_dialog
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.error_handling import run_guarded
from inventory_manager_mini.ui.models.dashboard_table_model import DashboardTableModel
from inventory_manager_mini.ui.widgets.category_picker import CategoryPicker

logger = logging.getLogger(__name__)

_GROUP_AXIS_LABELS: tuple[tuple[GroupAxis, str], ...] = (
    (GroupAxis.CLIENT, "クライアント"),
    (GroupAxis.PURCHASER, "発注主体"),
    (GroupAxis.CATEGORY, "カテゴリ"),
    (GroupAxis.ITEM, "品目"),
)


def _axis_parameters(maximum: int) -> tuple[int, int]:
    if maximum <= 0:
        return 1, 1

    scale = 1
    while True:
        interval = next(
            (multiplier * scale for multiplier in (1, 2, 5) if multiplier * scale * 5 >= maximum),
            0,
        )
        if interval:
            break
        scale *= 10

    upper = ((maximum + interval - 1) // interval) * interval
    return upper, interval


@dataclass(frozen=True, slots=True)
class _DashboardSnapshot:
    clients: tuple[Client, ...]
    purchasers: tuple[Purchaser, ...]
    categories: tuple[Category, ...]
    item_choices: tuple[ItemChoice, ...]
    years: tuple[int, ...]
    client_id: int | None
    purchaser_id: int | None
    category_id: int | None
    item_id: int | None
    fiscal_year: int
    kind: PeriodKind
    group_axes: frozenset[GroupAxis]
    result: DashboardResult


class DashboardWindow(QWidget):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.context = context
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setWindowTitle("ダッシュボード")
        self._snapshot: _DashboardSnapshot | None = None
        self._stale = True
        self._shutdown = False
        self._positioned = False
        self._signals_blocked = False
        self._item_id: int | None = None

        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.timeout.connect(self._refresh)
        self._build_ui()
        self.context.data_bus.data_changed.connect(self._data_changed)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        self.client_combo = QComboBox(self)
        self.purchaser_combo = QComboBox(self)
        self.category_picker = CategoryPicker(self)
        self.item_combo = QComboBox(self)
        self.item_combo.setEditable(True)
        self.item_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.item_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.item_combo.setMinimumContentsLength(16)
        completer = QCompleter(self.item_combo.model(), self.item_combo)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.item_combo.setCompleter(completer)
        line_edit = self.item_combo.lineEdit()
        if line_edit is not None:
            line_edit.installEventFilter(self)
            line_edit.returnPressed.connect(self._commit_item_input)
        self.item_combo.activated.connect(self._item_activated)
        self.year_combo = QComboBox(self)
        self.kind_combo = QComboBox(self)
        self.kind_combo.addItem("月次", PeriodKind.MONTHLY)
        self.kind_combo.addItem("年次", PeriodKind.ANNUAL)
        self.group_checks: dict[GroupAxis, QCheckBox] = {
            axis: QCheckBox(label, self) for axis, label in _GROUP_AXIS_LABELS
        }

        narrowing = QHBoxLayout()
        for label, widget, stretch in (
            ("クライアント", self.client_combo, 1),
            ("発注主体", self.purchaser_combo, 1),
            ("カテゴリ", self.category_picker, 2),
            ("品目", self.item_combo, 2),
        ):
            form = QFormLayout()
            form.addRow(label, widget)
            narrowing.addLayout(form, stretch)
        outer.addLayout(narrowing)

        display = QHBoxLayout()
        for label, combo in (("年度", self.year_combo), ("期間", self.kind_combo)):
            form = QFormLayout()
            form.addRow(label, combo)
            display.addLayout(form)
        display.addWidget(QLabel("内訳", self))
        for check in self.group_checks.values():
            display.addWidget(check)
        display.addStretch(1)
        outer.addLayout(display)

        self.totals_label = QLabel(
            "入庫数: --　出庫数: --　支出額: --　単価未登録数量: --　廃棄数: --　廃棄額: --",
            self,
        )
        self.totals_label.setWordWrap(True)
        outer.addWidget(self.totals_label)

        self.tabs = QTabWidget(self)
        self.chart_scroll = QScrollArea(self.tabs)
        self.chart_scroll.setWidgetResizable(True)
        self.chart_content = QWidget(self.chart_scroll)
        chart_layout = QVBoxLayout(self.chart_content)
        self.quantity_chart = QChartView(self.chart_content)
        self.amount_chart = QChartView(self.chart_content)
        for chart_view in (self.quantity_chart, self.amount_chart):
            chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
            chart_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            chart_view.setMinimumHeight(self.fontMetrics().height() * 16)
            chart_layout.addWidget(chart_view)
        self.chart_scroll.setWidget(self.chart_content)
        self.tabs.addTab(self.chart_scroll, "グラフ")

        self.breakdown_model = DashboardTableModel(parent=self)
        self.breakdown_table = QTableView(self.tabs)
        self.breakdown_table.setModel(self.breakdown_model)
        self.breakdown_table.setAlternatingRowColors(True)
        self.tabs.addTab(self.breakdown_table, "内訳")

        self.disposal_model = DashboardTableModel(disposal_only=True, parent=self)
        self.disposal_table = QTableView(self.tabs)
        self.disposal_table.setModel(self.disposal_model)
        self.disposal_table.setAlternatingRowColors(True)
        self.tabs.addTab(self.disposal_table, "廃棄")
        outer.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.output_status = QLabel(self)
        self.output_status.setWordWrap(True)
        buttons.addWidget(self.output_status, 1)
        buttons.addStretch(1)
        self.csv_button = QPushButton("CSV 出力", self)
        self.csv_button.clicked.connect(self._export_csv)
        buttons.addWidget(self.csv_button)
        self.png_button = QPushButton("PNG 保存", self)
        self.png_button.clicked.connect(self._export_png)
        buttons.addWidget(self.png_button)
        self.close_button = QPushButton("閉じる", self)
        self.close_button.clicked.connect(self.hide)
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)
        self.tabs.currentChanged.connect(self._update_export_buttons)
        self.csv_button.setEnabled(False)
        self.png_button.setEnabled(False)

        self._replace_charts(())
        self.year_combo.setEnabled(False)
        for combo in (
            self.client_combo,
            self.purchaser_combo,
            self.year_combo,
            self.kind_combo,
        ):
            combo.currentIndexChanged.connect(self._conditions_changed)
        for check in self.group_checks.values():
            check.toggled.connect(self._conditions_changed)
        self.category_picker.category_changed.connect(self._conditions_changed)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(event, QFocusEvent)
            and event.type() == QEvent.Type.FocusOut
            and event.reason() != Qt.FocusReason.PopupFocusReason
            and watched is self.item_combo.lineEdit()
        ):
            self._commit_item_input()
        return super().eventFilter(watched, event)

    def _item_activated(self, _index: int) -> None:
        self._commit_item_input()

    def _commit_item_input(self) -> None:
        if self._signals_blocked or self._shutdown:
            return
        index = self.item_combo.findText(
            self.item_combo.currentText(),
            Qt.MatchFlag.MatchExactly | Qt.MatchFlag.MatchCaseSensitive,
        )
        if index < 0:
            self._show_item_selection()
            return
        item_id = self.item_combo.itemData(index)
        if item_id == self._item_id:
            return
        self._item_id = item_id
        self._conditions_changed(index)

    def _show_item_selection(self) -> None:
        index = max(self.item_combo.findData(self._item_id), 0)
        self._signals_blocked = True
        try:
            self.item_combo.setCurrentIndex(index)
            self.item_combo.setEditText(self.item_combo.itemText(index))
        finally:
            self._signals_blocked = False

    def _conditions_changed(self, _index: object) -> None:
        if self._signals_blocked or self._shutdown:
            return
        self._stale = True
        self._rebuild_timer.stop()
        self._refresh()

    def _data_changed(self) -> None:
        if self._shutdown:
            return
        self._stale = True
        if self.isVisible():
            self._rebuild_timer.start(0)

    def _collect_snapshot(self) -> _DashboardSnapshot:
        clients = tuple(self.context.master.list_clients(include_inactive=True))
        purchasers = tuple(self.context.master.list_purchasers(include_inactive=True))
        categories = tuple(self.context.master.list_categories())
        item_choices = tuple(self.context.inventory.list_item_choices())
        years = tuple(self.context.reports.available_fiscal_years())
        current_year = self.context.reports.current_fiscal_year()

        selected_client = self.client_combo.currentData()
        client_id = selected_client if any(item.id == selected_client for item in clients) else None
        selected_purchaser = self.purchaser_combo.currentData()
        purchaser_id = (
            selected_purchaser
            if any(item.id == selected_purchaser for item in purchasers)
            else None
        )
        selected_category = self.category_picker.current_category_id()
        category_id = (
            selected_category
            if any(category.id == selected_category for category in categories)
            else None
        )
        item_id = (
            self._item_id if any(choice.id == self._item_id for choice in item_choices) else None
        )
        selected_year = self.year_combo.currentData()
        fiscal_year = selected_year if selected_year in years else current_year
        if fiscal_year not in years:
            fiscal_year = years[-1] if years else current_year
        kind = PeriodKind(self.kind_combo.currentData() or PeriodKind.MONTHLY)
        group_axes = frozenset(
            axis for axis, check in self.group_checks.items() if check.isChecked()
        )
        result = self.context.reports.dashboard_result(
            None if kind is PeriodKind.ANNUAL else fiscal_year,
            kind,
            client_id,
            purchaser_id,
            category_id,
            item_id,
            group_by=group_axes,
        )
        return _DashboardSnapshot(
            clients,
            purchasers,
            categories,
            item_choices,
            years,
            client_id,
            purchaser_id,
            category_id,
            item_id,
            fiscal_year,
            kind,
            group_axes,
            result,
        )

    def _refresh(self) -> None:
        if self._shutdown or not self.isVisible():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            succeeded, snapshot = run_guarded(self, self._collect_snapshot)
        finally:
            QApplication.restoreOverrideCursor()
        if not succeeded or snapshot is None:
            if self._snapshot is None:
                self._reset_unacquired_state()
            else:
                self._apply_snapshot(self._snapshot)
            self._stale = True
            return
        self._snapshot = snapshot
        self._apply_snapshot(snapshot)
        self._stale = False

    def _apply_snapshot(self, snapshot: _DashboardSnapshot) -> None:
        self._signals_blocked = True
        try:
            self._fill_master_combo(self.client_combo, snapshot.clients, snapshot.client_id)
            self._fill_master_combo(
                self.purchaser_combo, snapshot.purchasers, snapshot.purchaser_id
            )
            self.category_picker.setEnabled(True)
            self.category_picker.set_categories(list(snapshot.categories), leading_label="すべて")
            self.category_picker.set_current_category_id(snapshot.category_id)
            self._item_id = snapshot.item_id
            self.item_combo.setEnabled(True)
            self._fill_item_combo(self.item_combo, snapshot.item_choices, snapshot.item_id)
            self.year_combo.clear()
            for year in snapshot.years:
                self.year_combo.addItem(f"{year}年度", year)
            index = self.year_combo.findData(snapshot.fiscal_year)
            self.year_combo.setCurrentIndex(max(index, 0))
            self.year_combo.setEnabled(snapshot.kind is PeriodKind.MONTHLY)
            self.kind_combo.setCurrentIndex(max(self.kind_combo.findData(snapshot.kind), 0))
            for axis, check in self.group_checks.items():
                check.setChecked(axis in snapshot.group_axes)
        finally:
            self._signals_blocked = False

        self.breakdown_model.set_rows(snapshot.result.breakdown)
        self.disposal_model.set_rows(snapshot.result.breakdown)
        self._replace_charts(snapshot.result.totals)
        self.totals_label.setText(self._format_totals(snapshot.result.totals))
        self._update_export_buttons()

    def _reset_unacquired_state(self) -> None:
        self._signals_blocked = True
        try:
            for combo in (self.client_combo, self.purchaser_combo, self.year_combo):
                combo.clear()
            self.item_combo.clear()
            self.item_combo.setEnabled(False)
            self._item_id = None
            self.category_picker.set_categories([])
            self.category_picker.setEnabled(False)
            self.year_combo.setEnabled(False)
        finally:
            self._signals_blocked = False
        self.breakdown_model.set_rows(())
        self.disposal_model.set_rows(())
        self._replace_charts(())
        self.totals_label.setText(
            "入庫数: --　出庫数: --　支出額: --　単価未登録数量: --　廃棄数: --　廃棄額: --"
        )
        self._update_export_buttons()

    def _update_export_buttons(self, _index: int = 0) -> None:
        acquired = self._snapshot is not None
        self.csv_button.setEnabled(acquired)
        self.png_button.setEnabled(acquired and self.tabs.currentIndex() == 0)

    def _export_csv(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        path = export_csv_with_dialog(
            self,
            "ダッシュボード",
            "ダッシュボード",
            lambda destination: self.context.reports.export_dashboard_csv(
                destination, snapshot.result.breakdown
            ),
        )
        if path is not None:
            self.output_status.setText(f"CSV を出力しました: {path}")

    def _export_png(self) -> None:
        if self._snapshot is None or self.tabs.currentIndex() != 0:
            return
        path = export_file_with_dialog(
            self,
            "ダッシュボード",
            "ダッシュボード",
            ".png",
            "PNG ファイル (*.png)",
            self._save_chart_png,
        )
        if path is not None:
            self.output_status.setText(f"PNG を保存しました: {path}")

    def _save_chart_png(self, path: Path) -> None:
        output = QSaveFile(str(path))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise FileOutputError(f"PNGファイルを書き込めません: {path} ({output.errorString()})")
        try:
            image = self.chart_content.grab()
            if image.isNull() or not image.save(output, "PNG"):
                output.cancelWriting()
                raise FileOutputError(f"PNG画像を保存できません: {path}")
            if not output.commit():
                output.cancelWriting()
                raise FileOutputError(
                    f"PNGファイルを確定できません: {path} ({output.errorString()})"
                )
        except FileOutputError:
            raise
        except Exception as error:
            output.cancelWriting()
            raise FileOutputError(f"PNG画像を保存できません: {path} ({error})") from error

    @staticmethod
    def _fill_master_combo(
        combo: QComboBox,
        records: tuple[Client, ...] | tuple[Purchaser, ...],
        selected_id: int | None,
    ) -> None:
        combo.clear()
        combo.addItem("すべて", None)
        for record in records:
            label = record.name if record.is_active else f"{record.name} (無効)"
            combo.addItem(label, record.id)
        index = combo.findData(selected_id)
        combo.setCurrentIndex(max(index, 0))

    @staticmethod
    def _fill_item_combo(
        combo: QComboBox, choices: tuple[ItemChoice, ...], selected_id: int | None
    ) -> None:
        combo.clear()
        combo.addItem("すべて", None)
        for choice in choices:
            label = f"{choice.code} {choice.name}"
            combo.addItem(label if choice.is_active else f"{label} (無効)", choice.id)
        index = max(combo.findData(selected_id), 0)
        combo.setCurrentIndex(index)
        combo.setEditText(combo.itemText(index))

    @staticmethod
    def _format_totals(rows: Sequence[DashboardRow]) -> str:
        values = [0, 0, 0, 0, 0, 0]
        for row in rows:
            values[0] += row.inbound_quantity
            values[1] += row.outbound_quantity
            values[2] += row.expenditure
            values[3] += row.unpriced_issue_quantity
            values[4] += row.disposed_quantity
            values[5] += row.disposal_amount
        return (
            f"入庫数: {values[0]:,}　出庫数: {values[1]:,}　支出額: {values[2]:,}円　"
            f"単価未登録数量: {values[3]:,}　廃棄数: {values[4]:,}　廃棄額: {values[5]:,}円"
        )

    def _replace_charts(self, rows: Sequence[DashboardRow]) -> None:
        self._replace_chart(self.quantity_chart, rows, ("inbound_quantity", "outbound_quantity"))
        self._replace_chart(self.amount_chart, rows, ("expenditure",))

    @staticmethod
    def _replace_chart(
        view: QChartView, rows: Sequence[DashboardRow], fields: tuple[str, ...]
    ) -> None:
        chart = QChart()
        chart.setTitle("数量" if fields[0] == "inbound_quantity" else "支出額")
        chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        if rows:
            categories = [row.period_label for row in rows]
            maximum = max((getattr(row, field) for row in rows for field in fields), default=0)
            axis_x = QBarCategoryAxis(chart)
            axis_x.append(categories)
            axis_x.setLabelsAngle(-45)
            axis_y = QValueAxis(chart)
            upper, interval = _axis_parameters(maximum)
            axis_y.setRange(0, upper)
            axis_y.setTickCount(upper // interval + 1)
            axis_y.setMinorTickCount(0)
            axis_y.setLabelFormat("%'d")
            series = QBarSeries(chart)
            for field, label in (
                ("inbound_quantity", "入庫数"),
                ("outbound_quantity", "出庫数"),
                ("expenditure", "支出額"),
            ):
                if field in fields:
                    bar_set = QBarSet(label, series)
                    bar_set.append([getattr(row, field) for row in rows])
                    series.append(bar_set)
            unit = "円" if fields == ("expenditure",) else ""

            def show_tooltip(status: bool, index: int, bar_set: QBarSet) -> None:
                if not status:
                    QToolTip.hideText()
                    return
                if 0 <= index < len(categories):
                    value = int(bar_set.at(index))
                    QToolTip.showText(
                        QCursor.pos(),
                        f"{categories[index]}\n{bar_set.label()}: {value:,}{unit}",
                        view,
                    )

            series.hovered.connect(show_tooltip)
            chart.addSeries(series)
            chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
            chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
            series.attachAxis(axis_x)
            series.attachAxis(axis_y)
        chart.legend().setVisible(bool(rows))
        old_chart = view.chart()
        view.setChart(chart)
        if old_chart is not None and shiboken6.isValid(old_chart):
            old_chart.deleteLater()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._shutdown:
            self.hide()
            return
        if not self._positioned:
            center_dialog(self)
            self._positioned = True
        if not event.spontaneous() and self._stale:
            self._refresh()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        if not event.spontaneous() and self._rebuild_timer.isActive():
            self._rebuild_timer.stop()
            self._stale = True

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._rebuild_timer.stop()
        self.context.data_bus.data_changed.disconnect(self._data_changed)
        self.hide()
        self.deleteLater()
