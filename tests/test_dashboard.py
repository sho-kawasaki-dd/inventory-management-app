from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import shiboken6
from PySide6.QtCharts import QBarCategoryAxis, QBarSeries, QChartView, QValueAxis
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QHideEvent, QImage, QShowEvent
from PySide6.QtWidgets import QComboBox, QFileDialog, QMessageBox
from pytestqt.qtbot import QtBot

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import GroupBy, NewItem, PeriodKind
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.display_settings import DisplaySettings, apply_display_settings
from inventory_manager_mini.ui.models.dashboard_table_model import DashboardTableModel
from inventory_manager_mini.ui.widgets.dashboard import DashboardWindow
from tests.conftest import FixedClock, create_test_context

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
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
            f"{inbound[index]:,}",
            f"{outbound[index]:,}",
            f"{expenditure[index]:,}円",
            f"{unpriced[index]:,}",
        ]
        for index, label in enumerate(FISCAL_2025_LABELS)
    ]
    assert _table_rows(window.breakdown_model) == expected_rows
    assert _table_rows(window.disposal_model) == [
        ["2025年6月", "(すべて)", "(すべて)", "2", "400円"]
    ]

    assert _bar_series(window.quantity_chart) == {"入庫数": inbound, "出庫数": outbound}
    assert _bar_series(window.amount_chart) == {"支出額": expenditure}
    assert _category_axis(window.quantity_chart).categories() == FISCAL_2025_LABELS
    assert _category_axis(window.amount_chart).categories() == FISCAL_2025_LABELS
    assert _value_axis(window.quantity_chart).min() == 0
    assert _value_axis(window.quantity_chart).max() == 15
    assert _value_axis(window.amount_chart).max() == 900


def test_disposal_table_excludes_rows_without_disposal(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    _select(window.group_combo, GroupBy.CLIENT)

    assert window.breakdown_model.rowCount() > window.disposal_model.rowCount()
    assert _table_rows(window.disposal_model) == [["2025年6月", "経理", "(すべて)", "2", "400円"]]


def test_chart_replacement_deletes_old_chart_and_rotates_labels(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    old_charts = [window.quantity_chart.chart(), window.amount_chart.chart()]
    assert _category_axis(window.quantity_chart).labelsAngle() == -45
    assert _category_axis(window.amount_chart).labelsAngle() == -45

    _select(window.group_combo, GroupBy.CLIENT)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    assert all(not shiboken6.isValid(chart) for chart in old_charts)
    assert window.quantity_chart.chart() not in old_charts
    assert _category_axis(window.quantity_chart).labelsAngle() == -45


def test_group_switch_changes_only_table_rows(qtbot: QtBot, env: Env) -> None:
    window = _open(qtbot, env)
    _select(window.year_combo, 2025)
    label = window.totals_label.text()
    quantity = _bar_series(window.quantity_chart)
    amount = _bar_series(window.amount_chart)

    assert window.breakdown_model.rowCount() == 12
    for group_by in (GroupBy.CLIENT, GroupBy.PURCHASER, GroupBy.CLIENT_PURCHASER):
        _select(window.group_combo, group_by)

        assert window.totals_label.text() == label
        assert _bar_series(window.quantity_chart) == quantity
        assert _bar_series(window.amount_chart) == amount
        assert window.breakdown_model.rowCount() == 6
        assert _column_sum(window.breakdown_model, 3) == 24
        assert _column_sum(window.breakdown_model, 4) == 14
        assert _column_sum(window.breakdown_model, 5) == 1300
        assert _column_sum(window.breakdown_model, 6) == 2

    _select(window.group_combo, GroupBy.CLIENT)
    rows = _table_rows(window.breakdown_model)
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("2025年5月", "総務", "(すべて)"),
        ("2025年5月", "経理", "(すべて)"),
        ("2025年6月", "総務", "(すべて)"),
        ("2025年6月", "経理", "(すべて)"),
        ("2025年7月", "総務", "(すべて)"),
        ("2025年8月", "経理", "(すべて)"),
    ]
    assert [row[3:] for row in rows[:3]] == [
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
    _select(window.group_combo, GroupBy.CLIENT)
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
    _select(window.group_combo, GroupBy.CLIENT)
    assert len(messages) == 1
    assert window.group_combo.currentData() == GroupBy.NONE

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
    _select(window.group_combo, GroupBy.CLIENT)
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
    _select(window.group_combo, GroupBy.CLIENT)
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
    assert window.group_combo.currentData() == GroupBy.CLIENT

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
    assert window.group_combo.currentData() == GroupBy.CLIENT


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

    _select(window.group_combo, GroupBy.PURCHASER)
    assert len(messages) == 1
    assert "範囲を超えます" in messages[0]
    assert probe.calls == 1
    assert window.group_combo.currentData() == GroupBy.NONE
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
    _select(window.group_combo, GroupBy.PURCHASER)
    assert len(messages) == 3
    assert window.group_combo.currentData() == GroupBy.PURCHASER


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
    _select(window.group_combo, GroupBy.CLIENT)

    assert len(messages) == 1
    assert window._snapshot is not None
    assert window.totals_label.text().count("--") == 0
