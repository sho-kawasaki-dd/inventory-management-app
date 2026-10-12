from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import shiboken6
from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QChartView, QValueAxis
from PySide6.QtCore import QCoreApplication, QEvent, QModelIndex, Qt
from PySide6.QtGui import QFocusEvent, QHideEvent, QImage, QShowEvent
from PySide6.QtWidgets import QApplication, QComboBox, QFileDialog, QMessageBox
from pytestqt.qtbot import QtBot

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import GroupAxis, NewItem, PeriodKind
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.display_settings import (
    DisplaySettings,
    apply_display_settings,
)
from inventory_manager_mini.ui.models.dashboard_table_model import DashboardTableModel
from inventory_manager_mini.ui.widgets import dashboard as dashboard_module
from inventory_manager_mini.ui.widgets.dashboard import DashboardWindow, _axis_parameters
from tests.conftest import (
    FixedClock,
    assert_buttons_reachable,
    assert_fits_work_area,
    create_test_context,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class GroupBy:
    NONE: frozenset[GroupAxis] = frozenset()
    CLIENT = frozenset({GroupAxis.CLIENT})
    PURCHASER = frozenset({GroupAxis.PURCHASER})
    CLIENT_PURCHASER = frozenset({GroupAxis.CLIENT, GroupAxis.PURCHASER})


FISCAL_2025_LABELS = [f"2025年{month}月" for month in range(4, 13)] + [
    f"2026年{month}月" for month in range(1, 4)
]


@dataclass
class Env:
    context: AppContext
    clock: FixedClock
    inventory: InventoryService
    master: MasterService
    settings: SettingsService
    items: dict[str, int]


class _SpontaneousHide(QHideEvent):
    def spontaneous(self) -> bool:
        return True


class _SpontaneousShow(QShowEvent):
    def spontaneous(self) -> bool:
        return True


class ReportProbe:
    """`dashboard_result` の呼び出し回数を数え、必要なら上限超過の失敗を差し込む。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, reports: ReportService) -> None:
        self.calls = 0
        self.fail = False
        self._original = reports.dashboard_result
        monkeypatch.setattr(reports, "dashboard_result", self)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.fail:
            raise ValidationError("集計値が 0〜1,000,000,000,000 の範囲を超えます")
        return self._original(*args, **kwargs)


def _at(clock: FixedClock, day: str) -> None:
    clock.current = datetime.fromisoformat(f"{day}T12:00:00+00:00")


def _new_item(inventory: InventoryService, name: str, client_id: int, purchaser_id: int) -> int:
    return inventory.create_item(
        NewItem(client_id=client_id, purchaser_id=purchaser_id, name=name, category_id=2)
    ).id


def _seed_history(inventory: InventoryService, clock: FixedClock) -> dict[str, int]:
    _at(clock, "2025-05-01")
    a = _new_item(inventory, "品目A", 1, 1)
    b = _new_item(inventory, "品目B", 2, 2)
    c = _new_item(inventory, "品目C", 1, 2)
    _at(clock, "2025-05-10")
    inventory.receive(a, 1, 10, unit_price=100)
    inventory.receive(b, 1, 5, unit_price=200)
    _at(clock, "2025-05-20")
    inventory.issue(a, 1, 4, "部署X")
    _at(clock, "2025-06-01")
    inventory.receive(a, 1, 6, unit_price=150)
    _at(clock, "2025-06-05")
    inventory.issue(a, 1, 8, "部署X")
    _at(clock, "2025-06-10")
    inventory.dispose(b, 1, 2)
    _at(clock, "2025-06-15")
    reversed_issue = inventory.issue(b, 1, 1, "部署Y")
    _at(clock, "2025-07-01")
    inventory.receive(c, 1, 3)
    _at(clock, "2025-07-02")
    inventory.issue(c, 1, 2, "部署Z")
    inventory.reverse(reversed_issue.id, 1)
    _at(clock, "2025-08-01")
    inventory.return_to_supplier(b, 1, 1)
    _at(clock, "2026-04-02")
    inventory.receive(a, 1, 5, unit_price=120)
    return {"a": a, "b": b, "c": c}


def _make_env(seeded_conn: sqlite3.Connection, clock: FixedClock, *, seed: bool) -> Env:
    inventory = InventoryService(seeded_conn, clock=clock)
    master = MasterService(seeded_conn)
    settings = SettingsService(seeded_conn)
    items = _seed_history(inventory, clock) if seed else {}
    context = create_test_context(
        seeded_conn, inventory=inventory, master=master, settings=settings
    )
    context.reports = ReportService(seeded_conn, tz=UTC, clock=lambda: NOW)
    return Env(context, clock, inventory, master, settings, items)


@pytest.fixture
def env(seeded_conn: sqlite3.Connection, fixed_clock: FixedClock) -> Env:
    return _make_env(seeded_conn, fixed_clock, seed=True)


@pytest.fixture
def empty_env(seeded_conn: sqlite3.Connection, fixed_clock: FixedClock) -> Env:
    return _make_env(seeded_conn, fixed_clock, seed=False)


@pytest.fixture
def messages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    recorded: list[str] = []
    for kind in ("warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, kind, lambda _parent, _title, text, *_args: recorded.append(text)
        )
    return recorded


def _open(qtbot: QtBot, env: Env) -> DashboardWindow:
    window = DashboardWindow(env.context)
    qtbot.addWidget(window)
    window.show()
    return window


def _select(combo: QComboBox, data: object) -> None:
    index = combo.findData(data)
    assert index >= 0
    combo.setCurrentIndex(index)


def _set_group(window: DashboardWindow, axes: frozenset[GroupAxis]) -> None:
    for axis, check in window.group_checks.items():
        check.blockSignals(True)
        check.setChecked(axis in axes)
        check.blockSignals(False)
    window._conditions_changed(0)


def _group_of(window: DashboardWindow) -> frozenset[GroupAxis]:
    return frozenset(axis for axis, check in window.group_checks.items() if check.isChecked())


ALL_AXES = frozenset(GroupAxis)


def _pick_item(window: DashboardWindow, item_id: int | None) -> None:
    """候補の選択(コンボのドロップダウンまたは補完候補の確定)を再現する。"""
    index = window.item_combo.findData(item_id)
    assert index >= 0
    window.item_combo.setCurrentIndex(index)
    window.item_combo.activated.emit(index)


def _pick_category(window: DashboardWindow, category_id: int | None) -> None:
    window.category_picker.set_current_category_id(category_id)
    window.category_picker.category_changed.emit(category_id)


def _type_item_text(window: DashboardWindow, text: str) -> None:
    line_edit = window.item_combo.lineEdit()
    assert line_edit is not None
    line_edit.setText(text)


def _press_enter_in_item_combo(qtbot: QtBot, window: DashboardWindow) -> None:
    line_edit = window.item_combo.lineEdit()
    assert line_edit is not None
    completer = window.item_combo.completer()
    popup = completer.popup() if completer is not None else None
    if popup is not None:
        popup.hide()
    qtbot.keyClick(line_edit, Qt.Key.Key_Return)


def _move_focus_out_of_item_combo(window: DashboardWindow) -> None:
    line_edit = window.item_combo.lineEdit()
    assert line_edit is not None
    QApplication.sendEvent(
        line_edit, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.TabFocusReason)
    )


def _add_category_tree(env: Env) -> dict[str, int]:
    """備品 > 文具 > 筆記具 と 備品 > 電気 を追加し、末端カテゴリに品目 D・E の履歴を入れる。"""
    pen_category = env.master.add_category("筆記具", "PN", parent_id=2).id
    electric_category = env.master.add_category("電気", "EL", parent_id=1).id
    item_d = env.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="品目D", category_id=pen_category)
    ).id
    item_e = env.inventory.create_item(
        NewItem(client_id=2, purchaser_id=2, name="品目E", category_id=electric_category)
    ).id
    _at(env.clock, "2025-05-12")
    env.inventory.receive(item_d, 1, 3, unit_price=100)
    env.inventory.receive(item_e, 1, 7, unit_price=100)
    _at(env.clock, "2025-05-25")
    env.inventory.issue(item_e, 1, 2, "部署W")
    return {"pen": pen_category, "electric": electric_category, "d": item_d, "e": item_e}


def _totals_text(
    inbound: int,
    outbound: int,
    expenditure: int,
    unpriced: int,
    disposed: int,
    disposal_amount: int,
) -> str:
    return (
        f"入庫数: {inbound:,}　出庫数: {outbound:,}　支出額: {expenditure:,}円　"
        f"単価未登録数量: {unpriced:,}　廃棄数: {disposed:,}　廃棄額: {disposal_amount:,}円"
    )


def _table_rows(model: DashboardTableModel) -> list[list[Any]]:
    return [
        [model.data(model.index(row, column)) for column in range(model.columnCount())]
        for row in range(model.rowCount())
    ]


def _bar_series(view: QChartView) -> dict[str, list[int]]:
    series = view.chart().series()
    if not series:
        return {}
    bar_series = series[0]
    assert isinstance(bar_series, QBarSeries)
    return {
        bar_set.label(): [int(bar_set.at(index)) for index in range(bar_set.count())]
        for bar_set in bar_series.barSets()
    }


def _category_axis(view: QChartView) -> QBarCategoryAxis:
    axis = view.chart().axes(Qt.Orientation.Horizontal)[0]
    assert isinstance(axis, QBarCategoryAxis)
    return axis


def _value_axis(view: QChartView) -> QValueAxis:
    axis = view.chart().axes(Qt.Orientation.Vertical)[0]
    assert isinstance(axis, QValueAxis)
    return axis


@pytest.mark.parametrize(
    ("maximum", "expected_upper", "expected_interval"),
    [
        (0, 1, 1),
        (1, 1, 1),
        (5, 5, 1),
        (7, 8, 2),
        (15, 15, 5),
        (900, 1000, 200),
        (1_234_567, 1_500_000, 500_000),
    ],
)
def test_axis_parameters_use_nice_integer_ticks(
    maximum: int, expected_upper: int, expected_interval: int
) -> None:
    upper, interval = _axis_parameters(maximum)

    assert (upper, interval) == (expected_upper, expected_interval)
    assert upper >= maximum
    assert upper // interval + 1 <= 6
    assert upper % interval == 0


def _padded(values: dict[int, int], length: int = 12) -> list[int]:
    return [values.get(index, 0) for index in range(length)]


def _column_sum(model: DashboardTableModel, column: int) -> int:
    return sum(
        int(str(row[column]).replace(",", "").removesuffix("円")) for row in _table_rows(model)
    )


def _settle(qtbot: QtBot, probe: ReportProbe, expected_calls: int) -> None:
    qtbot.waitUntil(lambda: probe.calls >= expected_calls, timeout=1500)
    qtbot.wait(30)
    assert probe.calls == expected_calls


def test_initial_state_before_show_is_not_acquired(qtbot: QtBot, env: Env) -> None:
    window = DashboardWindow(env.context)
    qtbot.addWidget(window)
    assert window.totals_label.text().count("--") == 6
    assert window.breakdown_model.rowCount() == 0
    assert window.year_combo.count() == 0
    assert not window.year_combo.isEnabled()


def test_monthly_values_match_hand_calculation(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)

    assert window.totals_label.text() == _totals_text(24, 14, 1300, 2, 2, 400)
    assert window.tabs.tabText(0) == "グラフ"
    inbound = _padded({1: 15, 2: 6, 3: 3})
    outbound = _padded({1: 4, 2: 8, 3: 2})
    expenditure = _padded({1: 400, 2: 900})
    unpriced = _padded({3: 2})

    expected_rows = [
        [
            label,
            "(すべて)",
            "(すべて)",
            "(すべて)",
            "(すべて)",
            f"{inbound[index]:,}",
            f"{outbound[index]:,}",
            f"{expenditure[index]:,}円",
            f"{unpriced[index]:,}",
        ]
        for index, label in enumerate(FISCAL_2025_LABELS)
    ]
    assert _table_rows(window.breakdown_model) == expected_rows
    assert _table_rows(window.disposal_model) == [
        ["2025年6月", "(すべて)", "(すべて)", "(すべて)", "(すべて)", "2", "400円"]
    ]

    assert _bar_series(window.quantity_chart) == {"入庫数": inbound, "出庫数": outbound}
    assert _bar_series(window.amount_chart) == {"支出額": expenditure}
    assert _category_axis(window.quantity_chart).categories() == FISCAL_2025_LABELS
    assert _category_axis(window.amount_chart).categories() == FISCAL_2025_LABELS
    assert _value_axis(window.quantity_chart).min() == 0
    assert _value_axis(window.quantity_chart).max() == 15
    assert _value_axis(window.amount_chart).max() == 1000
    assert _value_axis(window.quantity_chart).tickCount() == 4
    assert _value_axis(window.amount_chart).tickCount() == 6
    assert _value_axis(window.quantity_chart).minorTickCount() == 0
    assert _value_axis(window.amount_chart).minorTickCount() == 0


def test_disposal_table_excludes_rows_without_disposal(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    _set_group(window, GroupBy.CLIENT)

    assert window.breakdown_model.rowCount() > window.disposal_model.rowCount()
    assert _table_rows(window.disposal_model) == [
        ["2025年6月", "経理", "(すべて)", "(すべて)", "(すべて)", "2", "400円"]
    ]


def test_chart_replacement_deletes_old_chart_and_rotates_labels(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    old_charts = [window.quantity_chart.chart(), window.amount_chart.chart()]
    assert _category_axis(window.quantity_chart).labelsAngle() == -45
    assert _category_axis(window.amount_chart).labelsAngle() == -45

    _set_group(window, GroupBy.CLIENT)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert all(not shiboken6.isValid(chart) for chart in old_charts)
    assert window.quantity_chart.chart() not in old_charts
    assert _category_axis(window.quantity_chart).labelsAngle() == -45


def test_bar_tooltips_show_period_series_value_and_survive_chart_replacement(
    qtbot: QtBot,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shown: list[tuple[str, object]] = []
    hidden: list[bool] = []

    class ToolTipProbe:
        @staticmethod
        def showText(_position: object, text: str, widget: object) -> None:
            shown.append((text, widget))

        @staticmethod
        def hideText() -> None:
            hidden.append(True)

    monkeypatch.setattr(dashboard_module, "QToolTip", ToolTipProbe)
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)

    quantity_series = window.quantity_chart.chart().series()[0]
    assert isinstance(quantity_series, QBarSeries)
    inbound_set, outbound_set = quantity_series.barSets()
    quantity_series.hovered.emit(True, 1, inbound_set)
    quantity_series.hovered.emit(True, 2, outbound_set)

    amount_series = window.amount_chart.chart().series()[0]
    assert isinstance(amount_series, QBarSeries)
    amount_series.hovered.emit(True, 2, amount_series.barSets()[0])
    assert [text for text, _widget in shown] == [
        "2025年5月\n入庫数: 15",
        "2025年6月\n出庫数: 8",
        "2025年6月\n支出額: 900円",
    ]
    assert all(widget is window.quantity_chart for _text, widget in shown[:2])
    assert shown[2][1] is window.amount_chart

    quantity_series.hovered.emit(False, 1, inbound_set)
    assert hidden == [True]

    _select(window.kind_combo, PeriodKind.ANNUAL)
    annual_amount_series = window.amount_chart.chart().series()[0]
    assert isinstance(annual_amount_series, QBarSeries)
    annual_amount_series.hovered.emit(True, 0, annual_amount_series.barSets()[0])
    assert shown[-1][0] == "2025年度\n支出額: 1,300円"

    _select(window.kind_combo, PeriodKind.MONTHLY)
    old_chart = window.quantity_chart.chart()
    _set_group(window, GroupBy.CLIENT)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(old_chart)
    new_series = window.quantity_chart.chart().series()[0]
    assert isinstance(new_series, QBarSeries)
    new_series.hovered.emit(True, 1, new_series.barSets()[0])
    assert shown[-1][0] == "2025年5月\n入庫数: 15"


def test_group_switch_changes_only_table_rows(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    label = window.totals_label.text()
    quantity = _bar_series(window.quantity_chart)
    amount = _bar_series(window.amount_chart)

    assert window.breakdown_model.rowCount() == 12
    for group_by in (GroupBy.CLIENT, GroupBy.PURCHASER, GroupBy.CLIENT_PURCHASER):
        _set_group(window, group_by)

        assert window.totals_label.text() == label
        assert _bar_series(window.quantity_chart) == quantity
        assert _bar_series(window.amount_chart) == amount
        assert window.breakdown_model.rowCount() == 6
        assert _column_sum(window.breakdown_model, 5) == 24
        assert _column_sum(window.breakdown_model, 6) == 14
        assert _column_sum(window.breakdown_model, 7) == 1300
        assert _column_sum(window.breakdown_model, 8) == 2

    _set_group(window, GroupBy.CLIENT)
    rows = _table_rows(window.breakdown_model)
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("2025年5月", "総務", "(すべて)"),
        ("2025年5月", "経理", "(すべて)"),
        ("2025年6月", "総務", "(すべて)"),
        ("2025年6月", "経理", "(すべて)"),
        ("2025年7月", "総務", "(すべて)"),
        ("2025年8月", "経理", "(すべて)"),
    ]
    assert [row[5:] for row in rows[:3]] == [
        ["10", "4", "400円", "0"],
        ["5", "0", "0円", "0"],
        ["6", "8", "900円", "0"],
    ]


def test_annual_lists_all_fiscal_years_and_disables_year_selection(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    assert window.year_combo.isEnabled()
    assert [window.year_combo.itemData(index) for index in range(window.year_combo.count())] == [
        2025,
        2026,
    ]
    assert window.year_combo.currentData() == 2026
    assert _category_axis(window.quantity_chart).categories()[0] == "2026年4月"

    _select(window.kind_combo, PeriodKind.ANNUAL)

    assert not window.year_combo.isEnabled()
    assert _category_axis(window.quantity_chart).categories() == ["2025年度", "2026年度"]
    assert _bar_series(window.quantity_chart) == {"入庫数": [24, 5], "出庫数": [14, 0]}
    assert _bar_series(window.amount_chart) == {"支出額": [1300, 0]}
    assert window.breakdown_model.rowCount() == 2
    assert window.totals_label.text() == _totals_text(29, 14, 1300, 2, 2, 400)

    _select(window.kind_combo, PeriodKind.MONTHLY)
    assert window.year_combo.isEnabled()
    _select(window.year_combo, 2025)
    assert _category_axis(window.quantity_chart).categories() == FISCAL_2025_LABELS
    _select(window.year_combo, 2026)
    categories = _category_axis(window.quantity_chart).categories()
    assert len(categories) == 12
    assert categories[0] == "2026年4月"
    assert _bar_series(window.quantity_chart)["入庫数"] == _padded({0: 5})


def test_inactive_master_records_are_selectable_and_aggregated(qtbot: QtBot, env: Env) -> None:
    env.master.deactivate_client(2)
    env.master.deactivate_purchaser(2)
    window = _open(qtbot, env)
    _select(window.kind_combo, PeriodKind.ANNUAL)

    assert window.client_combo.findText("経理 (無効)") >= 0
    assert window.purchaser_combo.findText("部門 (無効)") >= 0
    assert window.client_combo.findText("経理") == -1

    _select(window.client_combo, 2)
    assert window.totals_label.text() == _totals_text(5, 0, 0, 0, 2, 400)

    _select(window.client_combo, None)
    _select(window.purchaser_combo, 2)
    assert window.totals_label.text() == _totals_text(8, 2, 0, 2, 2, 400)
    row = _table_rows(window.breakdown_model)[0]
    assert row[1:3] == ["(すべて)", "部門"]


def test_empty_history_uses_zero_to_one_axis(qtbot: QtBot, empty_env: Env) -> None:
    window = _open(qtbot, empty_env)

    assert window.totals_label.text() == _totals_text(0, 0, 0, 0, 0, 0)
    assert window.breakdown_model.rowCount() == 12
    assert window.disposal_model.rowCount() == 0
    for view in (window.quantity_chart, window.amount_chart):
        axis = _value_axis(view)
        assert (axis.min(), axis.max()) == (0, 1)
        assert axis.tickCount() == 2
        assert axis.minorTickCount() == 0


@pytest.mark.parametrize("scale", [100, 150])
def test_dashboard_fits_work_area_and_monthly_labels_render(
    qtbot: QtBot,
    env: Env,
    restore_app_font: Any,
    scale: int,
) -> None:
    apply_display_settings(DisplaySettings(font_scale_percent=scale))
    window = _open(qtbot, env)
    qtbot.waitExposed(window)
    qtbot.wait(30)

    assert_fits_work_area(window)
    assert_buttons_reachable(
        window,
        window.csv_button,
        window.png_button,
        window.close_button,
        window.client_combo,
        window.purchaser_combo,
        window.category_picker.button,
        window.item_combo,
        window.year_combo,
        window.kind_combo,
        *window.group_checks.values(),
    )
    for view in (window.quantity_chart, window.amount_chart):
        axis = _category_axis(view)
        assert len(axis.categories()) == 12
        assert axis.labelsAngle() == -45
        image = view.grab().toImage()
        assert not image.isNull()
        assert (
            len(
                {
                    image.pixel(x, y)
                    for x in range(0, image.width(), 20)
                    for y in range(0, image.height(), 20)
                }
            )
            > 1
        )


def test_dashboard_breakdown_table_scrolls_when_grouped_rows_overflow(
    qtbot: QtBot, env: Env
) -> None:
    window = _open(qtbot, env)
    _set_group(window, GroupBy.CLIENT_PURCHASER)
    assert window._snapshot is not None
    window.breakdown_model.set_rows(window._snapshot.result.breakdown * 50)
    window.tabs.setCurrentIndex(1)
    qtbot.wait(30)

    assert window.breakdown_model.rowCount() > 30
    assert window.breakdown_table.verticalScrollBar().maximum() > 0
    table_top_left = window.breakdown_table.mapTo(window, window.breakdown_table.rect().topLeft())
    assert window.rect().contains(table_top_left)
    assert_fits_work_area(window)
    assert_buttons_reachable(window, window.csv_button, window.png_button, window.close_button)


def test_dashboard_csv_exports_the_displayed_breakdown(
    qtbot: QtBot,
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    window = _open(qtbot, env)
    _select(window.client_combo, 1)
    _set_group(window, GroupBy.CLIENT)
    destination = tmp_path / "dashboard.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))

    assert window.csv_button.isEnabled()
    window.csv_button.click()

    with destination.open(encoding="utf-8-sig", newline="") as file:
        output = list(csv.reader(file))
    assert output[0] == [
        "年度/月",
        "クライアント",
        "発注主体",
        "カテゴリ",
        "管理番号",
        "品名",
        "入庫数",
        "出庫数",
        "支出額",
        "単価未登録数量",
        "廃棄数",
        "廃棄額",
    ]
    assert window._snapshot is not None
    expected = [
        [
            row.period_label,
            row.client_name,
            row.purchaser_name if row.purchaser_id is not None else "(すべて)",
            "(すべて)",
            "(すべて)",
            "(すべて)",
            str(row.inbound_quantity),
            str(row.outbound_quantity),
            str(row.expenditure),
            str(row.unpriced_issue_quantity),
            str(row.disposed_quantity),
            str(row.disposal_amount),
        ]
        for row in window._snapshot.result.breakdown
    ]
    assert output[1:] == expected
    assert len(output) == window.breakdown_model.rowCount() + 1
    assert "CSV を出力しました" in window.output_status.text()
    assert any("CSV出力を開始" in record.message for record in caplog.records)
    assert any("CSV出力が完了" in record.message for record in caplog.records)


def test_dashboard_csv_cancel_does_not_create_a_file(
    qtbot: QtBot, env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    destination = tmp_path / "cancelled.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: ("", ""))

    window.csv_button.click()

    assert not destination.exists()
    assert not window.output_status.text()


def test_dashboard_png_exports_full_chart_content_at_any_scroll_position(
    qtbot: QtBot,
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    restore_app_font: Any,
) -> None:
    caplog.set_level("INFO")
    window = _open(qtbot, env)
    destination = tmp_path / "dashboard"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))

    assert window.png_button.isEnabled()
    window.png_button.click()

    first_path = tmp_path / "dashboard.png"
    first_bytes = first_path.read_bytes()
    image = QImage(str(first_path))
    assert not image.isNull()
    ratio = window.chart_content.devicePixelRatioF()
    assert image.width() == round(window.chart_content.width() * ratio)
    assert image.height() == round(window.chart_content.height() * ratio)

    def sampled_colors(top: int, bottom: int) -> set[int]:
        return {
            image.pixel(x, y)
            for x in range(0, image.width(), max(1, image.width() // 30))
            for y in range(top, bottom, max(1, (bottom - top) // 20))
        }

    assert len(sampled_colors(0, image.height() // 2)) > 1
    assert len(sampled_colors(image.height() // 2, image.height())) > 1

    scrollbar = window.chart_scroll.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
    bottom_path = tmp_path / "dashboard-bottom.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(bottom_path), ""))
    window.png_button.click()
    assert bottom_path.read_bytes() == first_bytes
    assert "PNG を保存しました" in window.output_status.text()

    apply_display_settings(DisplaySettings(font_scale_percent=150))
    qtbot.wait(30)
    scaled_path = tmp_path / "dashboard-150.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(scaled_path), ""))
    window.png_button.click()
    scaled_image = QImage(str(scaled_path))
    assert not scaled_image.isNull()
    scaled_ratio = window.chart_content.devicePixelRatioF()
    assert scaled_image.width() == round(window.chart_content.width() * scaled_ratio)
    assert scaled_image.height() == round(window.chart_content.height() * scaled_ratio)
    assert any("PNG出力を開始" in record.message for record in caplog.records)
    assert any("PNG出力が完了" in record.message for record in caplog.records)


def test_dashboard_png_failure_preserves_existing_file(
    qtbot: QtBot,
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    messages: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from PySide6.QtGui import QPixmap

    caplog.set_level("INFO")
    window = _open(qtbot, env)
    destination = tmp_path / "existing.png"
    original = b"existing image contents"
    destination.write_bytes(original)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))
    monkeypatch.setattr(window.chart_content, "grab", lambda: QPixmap())

    window.png_button.click()

    assert destination.read_bytes() == original
    assert len(messages) == 1
    assert "PNG画像を保存できません" in messages[0]
    assert window.isVisible()
    assert any("PNG出力を開始" in record.message for record in caplog.records)
    assert any("PNG出力に失敗" in record.message for record in caplog.records)


def test_exports_after_aggregation_failure_use_last_successful_snapshot(
    qtbot: QtBot,
    env: Env,
    tmp_path: Path,
    messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _open(qtbot, env)
    probe = ReportProbe(monkeypatch, env.context.reports)
    selected_path = {"value": tmp_path / "before.csv"}
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *_args: (str(selected_path["value"]), ""),
    )
    window.csv_button.click()
    csv_before = selected_path["value"].read_bytes()
    selected_path["value"] = tmp_path / "before.png"
    window.png_button.click()
    png_before = selected_path["value"].read_bytes()

    probe.fail = True
    _set_group(window, GroupBy.CLIENT)
    assert len(messages) == 1
    assert _group_of(window) == GroupBy.NONE

    selected_path["value"] = tmp_path / "after.csv"
    window.csv_button.click()
    assert selected_path["value"].read_bytes() == csv_before
    selected_path["value"] = tmp_path / "after.png"
    window.png_button.click()
    assert selected_path["value"].read_bytes() == png_before


def test_export_buttons_follow_acquisition_and_active_tab(
    qtbot: QtBot, env: Env, messages: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = ReportProbe(monkeypatch, env.context.reports)
    probe.fail = True
    window = _open(qtbot, env)

    assert len(messages) == 1
    assert not window.csv_button.isEnabled()
    assert not window.png_button.isEnabled()

    probe.fail = False
    _set_group(window, GroupBy.CLIENT)
    assert window.csv_button.isEnabled()
    assert window.png_button.isEnabled()
    window.tabs.setCurrentIndex(1)
    assert window.csv_button.isEnabled()
    assert not window.png_button.isEnabled()
    window.tabs.setCurrentIndex(0)
    assert window.png_button.isEnabled()


def test_data_changed_updates_once_and_keeps_selection(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    _select(window.client_combo, 1)
    _set_group(window, GroupBy.CLIENT)
    probe = ReportProbe(monkeypatch, env.context.reports)
    assert window.totals_label.text() == _totals_text(5, 0, 0, 0, 0, 0)

    _at(env.clock, "2026-10-12")
    env.inventory.receive(env.items["a"], 1, 7, unit_price=100)
    for _ in range(3):
        env.context.data_bus.data_changed.emit()
    assert probe.calls == 0
    _settle(qtbot, probe, 1)
    assert window.totals_label.text() == _totals_text(12, 0, 0, 0, 0, 0)
    assert window.client_combo.currentData() == 1
    assert _group_of(window) == GroupBy.CLIENT

    env.master.deactivate_purchaser(1)
    env.context.data_bus.data_changed.emit()
    _settle(qtbot, probe, 2)
    assert window.purchaser_combo.findText("本部 (無効)") >= 0
    assert window.client_combo.currentData() == 1

    env.settings.set_fiscal_year_start_month(1)
    env.context.data_bus.data_changed.emit()
    _settle(qtbot, probe, 3)
    assert _category_axis(window.quantity_chart).categories()[0] == "2026年1月"
    assert window.client_combo.currentData() == 1
    assert _group_of(window) == GroupBy.CLIENT


def test_hidden_window_defers_aggregation_until_shown(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    probe = ReportProbe(monkeypatch, env.context.reports)
    window.hide()
    _at(env.clock, "2026-10-12")
    env.inventory.receive(env.items["a"], 1, 7, unit_price=100)

    env.context.data_bus.data_changed.emit()
    qtbot.wait(30)
    assert probe.calls == 0

    window.show()
    assert probe.calls == 1
    assert window.totals_label.text() == _totals_text(12, 0, 0, 0, 0, 0)


def test_hide_and_show_without_changes_does_not_aggregate(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    probe = ReportProbe(monkeypatch, env.context.reports)

    window.hide()
    window.show()

    assert probe.calls == 0


def test_spontaneous_hide_and_show_do_not_aggregate(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    probe = ReportProbe(monkeypatch, env.context.reports)

    env.context.data_bus.data_changed.emit()
    window.hideEvent(_SpontaneousHide())
    assert window._rebuild_timer.isActive()
    window.showEvent(_SpontaneousShow())
    assert probe.calls == 0
    _settle(qtbot, probe, 1)

    window.hide()
    env.context.data_bus.data_changed.emit()
    window.showEvent(_SpontaneousShow())
    assert probe.calls == 1
    window.show()
    assert probe.calls == 2


def test_close_right_after_scheduling_stops_pending_aggregation(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    probe = ReportProbe(monkeypatch, env.context.reports)

    env.context.data_bus.data_changed.emit()
    window.close_button.click()
    assert not window.isVisible()
    qtbot.wait(30)
    assert probe.calls == 0

    window.show()
    assert probe.calls == 1


def test_shutdown_is_idempotent_and_rejects_later_use(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    probe = ReportProbe(monkeypatch, env.context.reports)
    deleted: list[bool] = []
    monkeypatch.setattr(window, "deleteLater", lambda: deleted.append(True))

    env.context.data_bus.data_changed.emit()
    window.shutdown()
    window.shutdown()
    qtbot.wait(30)

    assert deleted == [True]
    assert probe.calls == 0
    env.context.data_bus.data_changed.emit()
    window.show()
    window._refresh()
    assert not window.isVisible()
    assert probe.calls == 0


def test_aggregation_error_restores_last_successful_state(
    qtbot: QtBot,
    env: Env,
    messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _open(qtbot, env)
    _select(window.client_combo, 1)
    _select(window.year_combo, 2025)
    label = window.totals_label.text()
    rows = _table_rows(window.breakdown_model)
    quantity = _bar_series(window.quantity_chart)
    probe = ReportProbe(monkeypatch, env.context.reports)
    probe.fail = True

    _set_group(window, GroupBy.PURCHASER)
    assert len(messages) == 1
    assert "範囲を超えます" in messages[0]
    assert probe.calls == 1
    assert _group_of(window) == GroupBy.NONE
    assert window.client_combo.currentData() == 1
    assert window.year_combo.currentData() == 2025

    _select(window.kind_combo, PeriodKind.ANNUAL)
    assert len(messages) == 2
    assert probe.calls == 2
    assert window.kind_combo.currentData() == PeriodKind.MONTHLY
    assert window.year_combo.isEnabled()
    assert window.isVisible()

    _select(window.client_combo, None)
    assert len(messages) == 3
    assert window.client_combo.currentData() == 1
    assert window.totals_label.text() == label
    assert _table_rows(window.breakdown_model) == rows
    assert _bar_series(window.quantity_chart) == quantity

    probe.fail = False
    _set_group(window, GroupBy.PURCHASER)
    assert len(messages) == 3
    assert _group_of(window) == GroupBy.PURCHASER


def test_first_aggregation_failure_keeps_not_acquired_state_until_success(
    qtbot: QtBot,
    env: Env,
    messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = ReportProbe(monkeypatch, env.context.reports)
    probe.fail = True
    window = _open(qtbot, env)

    assert len(messages) == 1
    assert window.totals_label.text().count("--") == 6
    assert window._snapshot is None
    assert window.year_combo.count() == 0
    assert not window.year_combo.isEnabled()
    assert window.item_combo.count() == 0
    assert not window.item_combo.isEnabled()
    assert not window.category_picker.isEnabled()
    assert window.breakdown_model.rowCount() == 0
    assert window.disposal_model.rowCount() == 0
    assert _bar_series(window.quantity_chart) == {}

    window.hide()
    window.show()
    window.hide()
    window.show()
    assert len(messages) == 3
    assert probe.calls == 3

    probe.fail = False
    window.hide()
    window.show()
    assert len(messages) == 3
    assert window._snapshot is not None
    assert window.totals_label.text() == _totals_text(5, 0, 0, 0, 0, 0)
    assert window.year_combo.count() == 2
    assert window.breakdown_model.rowCount() == 12


def test_condition_change_retries_after_first_failure(
    qtbot: QtBot,
    env: Env,
    messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = ReportProbe(monkeypatch, env.context.reports)
    probe.fail = True
    window = _open(qtbot, env)
    assert len(messages) == 1

    probe.fail = False
    _set_group(window, GroupBy.CLIENT)

    assert len(messages) == 1
    assert window._snapshot is not None
    assert window.totals_label.text().count("--") == 0


@pytest.mark.parametrize(
    ("label", "item_key", "category_key", "expected_totals", "inbound_by_month"),
    [
        ("品目 A", "a", None, (16, 12, 1300, 0, 0, 0), {1: 10, 2: 6}),
        ("品目 D", "d", None, (3, 0, 0, 0, 0, 0), {1: 3}),
        ("筆記具", None, "pen", (3, 0, 0, 0, 0, 0), {1: 3}),
        ("文具(子孫を含む)", None, 2, (27, 14, 1300, 2, 2, 400), {1: 18, 2: 6, 3: 3}),
        ("電気(兄弟)", None, "electric", (7, 2, 200, 0, 0, 0), {1: 7}),
        ("備品(親)", None, 1, (34, 16, 1500, 2, 2, 400), {1: 25, 2: 6, 3: 3}),
        ("D ∧ 文具", "d", 2, (3, 0, 0, 0, 0, 0), {1: 3}),
        ("A ∧ 筆記具(配下にない)", "a", "pen", (0, 0, 0, 0, 0, 0), {}),
    ],
)
def test_item_and_category_filters_match_hand_calculation(
    qtbot: QtBot,
    env: Env,
    label: str,
    item_key: str | None,
    category_key: str | int | None,
    expected_totals: tuple[int, int, int, int, int, int],
    inbound_by_month: dict[int, int],
) -> None:
    ids = {**env.items, **_add_category_tree(env)}
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)

    if item_key is not None:
        _pick_item(window, ids[item_key])
    if category_key is not None:
        _pick_category(window, ids[category_key] if isinstance(category_key, str) else category_key)

    assert window.totals_label.text() == _totals_text(*expected_totals), label
    inbound, outbound, expenditure, unpriced, disposed, disposal_amount = expected_totals
    assert sum(_bar_series(window.quantity_chart)["入庫数"]) == inbound
    assert _bar_series(window.quantity_chart)["入庫数"] == _padded(inbound_by_month)
    assert sum(_bar_series(window.quantity_chart)["出庫数"]) == outbound
    assert sum(_bar_series(window.amount_chart)["支出額"]) == expenditure
    assert window.breakdown_model.rowCount() == 12
    assert _column_sum(window.breakdown_model, 5) == inbound
    assert _column_sum(window.breakdown_model, 6) == outbound
    assert _column_sum(window.breakdown_model, 7) == expenditure
    assert _column_sum(window.breakdown_model, 8) == unpriced
    disposal_rows = _table_rows(window.disposal_model)
    assert (len(disposal_rows) > 0) == (disposed > 0)
    assert sum(int(row[5].replace(",", "")) for row in disposal_rows) == disposed
    assert (
        sum(int(row[6].replace(",", "").removesuffix("円")) for row in disposal_rows)
        == disposal_amount
    )


def test_filtered_axes_appear_in_ungrouped_rows(qtbot: QtBot, env: Env) -> None:
    ids = _add_category_tree(env)
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)

    _pick_item(window, ids["d"])
    may = _table_rows(window.breakdown_model)[1]
    assert may == [
        "2025年5月",
        "(すべて)",
        "(すべて)",
        "(すべて)",
        "PN-0001 品目D",
        "3",
        "0",
        "0円",
        "0",
    ]

    _pick_item(window, None)
    _pick_category(window, ids["pen"])
    may = _table_rows(window.breakdown_model)[1]
    assert may[3:5] == ["備品 > 文具 > 筆記具", "(すべて)"]


@pytest.mark.parametrize("axes", [frozenset(), ALL_AXES], ids=("all-off", "all-on"))
def test_group_checks_change_only_table_rows_and_columns(
    qtbot: QtBot, env: Env, axes: frozenset[GroupAxis]
) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    _set_group(window, GroupBy.CLIENT)
    label = window.totals_label.text()
    quantity = _bar_series(window.quantity_chart)
    amount = _bar_series(window.amount_chart)

    _set_group(window, axes)

    assert _group_of(window) == axes
    assert window.totals_label.text() == label
    assert _bar_series(window.quantity_chart) == quantity
    assert _bar_series(window.amount_chart) == amount
    assert window.breakdown_model.rowCount() == (6 if axes else 12)
    assert _column_sum(window.breakdown_model, 5) == 24
    assert _column_sum(window.breakdown_model, 6) == 14
    assert _column_sum(window.breakdown_model, 7) == 1300
    assert _column_sum(window.breakdown_model, 8) == 2
    first = _table_rows(window.breakdown_model)[0]
    if axes:
        assert first == [
            "2025年5月",
            "総務",
            "本部",
            "備品 > 文具",
            "ST-0001 品目A",
            "10",
            "4",
            "400円",
            "0",
        ]
        assert _table_rows(window.disposal_model) == [
            [
                "2025年6月",
                "経理",
                "部門",
                "備品 > 文具",
                "ST-0002 品目B",
                "2",
                "400円",
            ]
        ]
    else:
        assert first[1:5] == ["(すべて)"] * 4


def test_category_and_item_axes_can_be_combined(qtbot: QtBot, env: Env) -> None:
    ids = _add_category_tree(env)
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)

    _set_group(window, frozenset({GroupAxis.CATEGORY}))
    rows = _table_rows(window.breakdown_model)
    assert {(row[3], row[4]) for row in rows} == {
        ("備品 > 文具", "(すべて)"),
        ("備品 > 文具 > 筆記具", "(すべて)"),
        ("備品 > 電気", "(すべて)"),
    }

    _set_group(window, frozenset({GroupAxis.ITEM}))
    assert {row[4] for row in _table_rows(window.breakdown_model)} == {
        "ST-0001 品目A",
        "ST-0002 品目B",
        "ST-0003 品目C",
        "PN-0001 品目D",
        "EL-0001 品目E",
    }
    assert ids["d"] > 0


def test_item_combo_lists_inactive_items_and_completes_by_partial_text(
    qtbot: QtBot, env: Env
) -> None:
    env.inventory.deactivate_item(env.items["c"])
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)

    assert [window.item_combo.itemText(index) for index in range(window.item_combo.count())] == [
        "すべて",
        "ST-0001 品目A",
        "ST-0002 品目B",
        "ST-0003 品目C (無効)",
    ]
    completer = window.item_combo.completer()
    assert completer is not None

    def completions(prefix: str) -> list[str]:
        completer.setCompletionPrefix(prefix)
        model = completer.completionModel()
        return [model.index(row, 0).data() for row in range(completer.completionCount())]

    assert completions("目あ") == []
    assert completions("目b") == ["ST-0002 品目B"]
    assert completions("st-0003") == ["ST-0003 品目C (無効)"]
    assert len(completions("品目")) == 3

    _pick_item(window, env.items["c"])
    assert window._item_id == env.items["c"]
    assert window.item_combo.currentText() == "ST-0003 品目C (無効)"
    assert window.totals_label.text() == _totals_text(3, 2, 0, 2, 0, 0)


def test_item_input_is_committed_only_by_enter_focus_out_or_candidate_selection(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    probe = ReportProbe(monkeypatch, env.context.reports)
    line_edit = window.item_combo.lineEdit()
    assert line_edit is not None

    line_edit.selectAll()
    qtbot.keyClicks(line_edit, "st-000")
    qtbot.wait(30)
    assert window._item_id is None
    assert probe.calls == 0

    _type_item_text(window, "ST-0002 品目B")
    assert window._item_id is None
    assert probe.calls == 0

    _press_enter_in_item_combo(qtbot, window)
    assert window._item_id == env.items["b"]
    assert probe.calls == 1
    assert window.totals_label.text() == _totals_text(5, 0, 0, 0, 2, 400)

    _press_enter_in_item_combo(qtbot, window)
    _move_focus_out_of_item_combo(window)
    assert probe.calls == 1

    _type_item_text(window, "ST-0001 品目A")
    _move_focus_out_of_item_combo(window)
    assert window._item_id == env.items["a"]
    assert probe.calls == 2
    _move_focus_out_of_item_combo(window)
    _press_enter_in_item_combo(qtbot, window)
    assert probe.calls == 2

    _pick_item(window, env.items["c"])
    assert probe.calls == 3
    _pick_item(window, env.items["c"])
    assert probe.calls == 3

    completer = window.item_combo.completer()
    assert completer is not None
    completer.setCompletionPrefix("st-0002")
    cast(Any, completer).activated[QModelIndex].emit(completer.completionModel().index(0, 0))
    qtbot.wait(30)
    assert window._item_id == env.items["b"]
    assert probe.calls == 4


@pytest.mark.parametrize("initial_item", ["a", None], ids=("item-selected", "all-selected"))
@pytest.mark.parametrize("invalid_text", ["存在しない品目", "", "ST-9999"])
@pytest.mark.parametrize("commit", ["enter", "focus-out"])
def test_unknown_item_text_reverts_to_confirmed_selection(
    qtbot: QtBot,
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    initial_item: str | None,
    invalid_text: str,
    commit: str,
) -> None:
    window = _open(qtbot, env)
    if initial_item is not None:
        _pick_item(window, env.items[initial_item])
    expected_id = window._item_id
    expected_text = window.item_combo.currentText()
    count = window.item_combo.count()
    probe = ReportProbe(monkeypatch, env.context.reports)

    _type_item_text(window, invalid_text)
    if commit == "enter":
        _press_enter_in_item_combo(qtbot, window)
    else:
        _move_focus_out_of_item_combo(window)

    assert window._item_id == expected_id
    assert window.item_combo.currentText() == expected_text
    assert window.item_combo.count() == count
    assert probe.calls == 0


def test_data_changed_follows_item_and_category_changes_and_keeps_selection(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _add_category_tree(env)
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    _pick_category(window, 2)
    _pick_item(window, ids["d"])
    probe = ReportProbe(monkeypatch, env.context.reports)
    assert window.totals_label.text() == _totals_text(3, 0, 0, 0, 0, 0)

    new_item = env.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="品目F", category_id=ids["pen"])
    ).id
    env.context.data_bus.data_changed.emit()
    _settle(qtbot, probe, 1)
    assert window.item_combo.findData(new_item) >= 0
    assert window._item_id == ids["d"]
    assert window.item_combo.currentText() == "PN-0001 品目D"
    assert window.category_picker.current_category_id() == 2

    env.inventory.deactivate_item(ids["d"])
    env.context.data_bus.data_changed.emit()
    _settle(qtbot, probe, 2)
    assert window.item_combo.currentText() == "PN-0001 品目D (無効)"
    assert window._item_id == ids["d"]
    assert window.totals_label.text() == _totals_text(3, 0, 0, 0, 0, 0)

    env.master.move_category(ids["pen"], ids["electric"])
    env.context.data_bus.data_changed.emit()
    _settle(qtbot, probe, 3)
    assert window.totals_label.text() == _totals_text(0, 0, 0, 0, 0, 0)
    assert window.category_picker.current_category_id() == 2
    assert window._item_id == ids["d"]

    _pick_category(window, ids["electric"])
    assert window.totals_label.text() == _totals_text(3, 0, 0, 0, 0, 0)


def test_selection_returns_to_all_when_selected_choices_disappear(
    qtbot: QtBot, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    removable_category = env.master.add_category("空", "EM", parent_id=1).id
    removable_item = env.inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="消える品目", category_id=2)
    ).id
    env.context.data_bus.data_changed.emit()
    probe = ReportProbe(monkeypatch, env.context.reports)
    _settle(qtbot, probe, 1)
    _pick_category(window, removable_category)
    _pick_item(window, removable_item)
    assert window.totals_label.text() == _totals_text(0, 0, 0, 0, 0, 0)

    env.master.delete_category(removable_category)
    env.inventory.conn.execute("DELETE FROM items WHERE id = ?", (removable_item,))
    env.context.data_bus.data_changed.emit()
    _settle(qtbot, probe, 4)

    assert window.category_picker.current_category_id() is None
    assert window.category_picker.display.text() == "すべて"
    assert window._item_id is None
    assert window.item_combo.currentText() == "すべて"
    assert window.totals_label.text() == _totals_text(24, 14, 1300, 2, 2, 400)


def test_aggregation_error_restores_item_category_and_group_checks(
    qtbot: QtBot,
    env: Env,
    messages: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _add_category_tree(env)
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    _pick_category(window, 2)
    _pick_item(window, ids["d"])
    _set_group(window, frozenset({GroupAxis.CATEGORY, GroupAxis.ITEM}))
    label = window.totals_label.text()
    rows = _table_rows(window.breakdown_model)
    disposal_rows = _table_rows(window.disposal_model)
    quantity = _bar_series(window.quantity_chart)
    amount = _bar_series(window.amount_chart)
    probe = ReportProbe(monkeypatch, env.context.reports)
    probe.fail = True

    def assert_restored() -> None:
        assert window._item_id == ids["d"]
        assert window.item_combo.currentText() == "PN-0001 品目D"
        assert window.category_picker.current_category_id() == 2
        assert _group_of(window) == frozenset({GroupAxis.CATEGORY, GroupAxis.ITEM})
        assert window.totals_label.text() == label
        assert _table_rows(window.breakdown_model) == rows
        assert _table_rows(window.disposal_model) == disposal_rows
        assert _bar_series(window.quantity_chart) == quantity
        assert _bar_series(window.amount_chart) == amount

    _pick_item(window, env.items["a"])
    assert (len(messages), probe.calls) == (1, 1)
    assert_restored()

    _pick_category(window, ids["electric"])
    assert (len(messages), probe.calls) == (2, 2)
    assert_restored()

    _set_group(window, GroupBy.CLIENT)
    assert (len(messages), probe.calls) == (3, 3)
    assert_restored()

    probe.fail = False
    _pick_item(window, env.items["a"])
    assert len(messages) == 3
    assert window._item_id == env.items["a"]


def test_dashboard_csv_matches_rows_for_item_category_filters_and_axes(
    qtbot: QtBot,
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_category_tree(env)
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    _pick_category(window, 2)
    _set_group(window, frozenset({GroupAxis.CATEGORY, GroupAxis.ITEM, GroupAxis.PURCHASER}))
    destination = tmp_path / "filtered.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_args: (str(destination), ""))

    window.csv_button.click()

    with destination.open(encoding="utf-8-sig", newline="") as file:
        output = list(csv.reader(file))
    assert window._snapshot is not None
    expected = [
        [
            row.period_label,
            row.client_name or "(すべて)",
            row.purchaser_name or "(すべて)",
            row.category_name or "(すべて)",
            row.item_code or "(すべて)",
            row.item_name or "(すべて)",
            str(row.inbound_quantity),
            str(row.outbound_quantity),
            str(row.expenditure),
            str(row.unpriced_issue_quantity),
            str(row.disposed_quantity),
            str(row.disposal_amount),
        ]
        for row in window._snapshot.result.breakdown
    ]
    assert len(output) == window.breakdown_model.rowCount() + 1
    assert output[1:] == expected
    assert all(row[1] == "(すべて)" for row in output[1:])
    assert {row[3] for row in output[1:]} == {
        "備品 > 文具",
        "備品 > 文具 > 筆記具",
    }
    assert all(row[4] != "(すべて)" for row in output[1:])
