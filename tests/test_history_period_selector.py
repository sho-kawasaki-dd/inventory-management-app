from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from inventory_manager_mini.core.models import HistoryPeriod, HistoryPeriodKind, NewItem
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.widgets.history_period_selector import HistoryPeriodSelector
from tests.conftest import create_test_context


@pytest.fixture
def selector_context(seeded_conn, inventory, master, settings, fixed_clock, bulk_history):
    item = inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="期間テスト品目", category_id=1)
    )
    bulk_history(seeded_conn, item.id, 1, datetime(2024, 6, 1, tzinfo=UTC))
    context = create_test_context(
        seeded_conn, inventory=inventory, master=master, settings=settings
    )
    context.reports = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    return context


def _select_kind(selector: HistoryPeriodSelector, kind: HistoryPeriodKind) -> None:
    selector.period_kind_combo.setCurrentIndex(selector.period_kind_combo.findData(kind))


def test_initial_state_is_all_period_with_current_year_and_month(qtbot, selector_context) -> None:
    selector = HistoryPeriodSelector(selector_context)
    qtbot.addWidget(selector)

    assert selector.period() == HistoryPeriod()
    assert selector.period_kind_combo.currentData() == HistoryPeriodKind.ALL
    assert [selector.year_combo.itemData(i) for i in range(selector.year_combo.count())] == [
        2024,
        2025,
    ]
    assert selector.year_combo.currentData() == 2025
    assert selector.year_combo.currentText() == "2025年度"
    assert selector.month_combo.currentData() == 1
    assert selector.month_combo.currentText() == "2026年1月"
    assert not selector.year_combo.isEnabled()
    assert not selector.month_combo.isEnabled()


@pytest.mark.parametrize(
    ("kind", "year_enabled", "month_enabled"),
    [
        (HistoryPeriodKind.ALL, False, False),
        (HistoryPeriodKind.ANNUAL, True, False),
        (HistoryPeriodKind.MONTHLY, True, True),
    ],
)
def test_combo_enabled_state_follows_kind(
    qtbot, selector_context, kind: HistoryPeriodKind, year_enabled: bool, month_enabled: bool
) -> None:
    selector = HistoryPeriodSelector(selector_context)
    qtbot.addWidget(selector)
    _select_kind(selector, HistoryPeriodKind.MONTHLY)

    _select_kind(selector, kind)

    assert selector.year_combo.isEnabled() is year_enabled
    assert selector.month_combo.isEnabled() is month_enabled


def test_period_reflects_selection(qtbot, selector_context) -> None:
    selector = HistoryPeriodSelector(selector_context)
    qtbot.addWidget(selector)
    selector.year_combo.setCurrentIndex(selector.year_combo.findData(2024))
    selector.month_combo.setCurrentIndex(selector.month_combo.findData(7))

    assert selector.period() == HistoryPeriod()
    _select_kind(selector, HistoryPeriodKind.ANNUAL)
    assert selector.period() == HistoryPeriod(HistoryPeriodKind.ANNUAL, 2024)
    _select_kind(selector, HistoryPeriodKind.MONTHLY)
    assert selector.period() == HistoryPeriod(HistoryPeriodKind.MONTHLY, 2024, 7)


def test_year_change_updates_month_labels_and_keeps_month_number(qtbot, selector_context) -> None:
    selector = HistoryPeriodSelector(selector_context)
    qtbot.addWidget(selector)
    _select_kind(selector, HistoryPeriodKind.MONTHLY)
    selector.month_combo.setCurrentIndex(selector.month_combo.findData(2))
    assert selector.month_combo.currentText() == "2026年2月"

    selector.year_combo.setCurrentIndex(selector.year_combo.findData(2024))

    assert selector.month_combo.currentData() == 2
    assert selector.month_combo.currentText() == "2025年2月"
    assert selector.month_combo.itemText(selector.month_combo.findData(12)) == "2024年12月"


def test_period_changed_is_emitted_once_per_operation(qtbot, selector_context) -> None:
    selector = HistoryPeriodSelector(selector_context)
    qtbot.addWidget(selector)
    emitted: list[HistoryPeriod] = []
    selector.periodChanged.connect(lambda: emitted.append(selector.period()))

    _select_kind(selector, HistoryPeriodKind.MONTHLY)
    assert len(emitted) == 1

    selector.year_combo.setCurrentIndex(selector.year_combo.findData(2024))
    assert emitted[1:] == [HistoryPeriod(HistoryPeriodKind.MONTHLY, 2024, 1)]

    selector.month_combo.setCurrentIndex(selector.month_combo.findData(5))
    assert emitted[2:] == [HistoryPeriod(HistoryPeriodKind.MONTHLY, 2024, 5)]
    assert len(emitted) == 3


@pytest.mark.parametrize(
    ("start_month", "expected_months", "expected_first_label", "expected_last_label"),
    [
        (4, [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3], "2025年4月", "2026年3月"),
        (1, list(range(1, 13)), "2026年1月", "2026年12月"),
        (10, [10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8, 9], "2025年10月", "2026年9月"),
    ],
)
def test_month_order_follows_fiscal_year_start_month(
    qtbot,
    selector_context: AppContext,
    start_month: int,
    expected_months: list[int],
    expected_first_label: str,
    expected_last_label: str,
) -> None:
    selector_context.settings.set_fiscal_year_start_month(start_month)
    selector = HistoryPeriodSelector(selector_context)
    qtbot.addWidget(selector)
    assert selector.year_combo.currentData() == (2026 if start_month == 1 else 2025)

    combo = selector.month_combo
    assert [combo.itemData(i) for i in range(combo.count())] == expected_months
    assert combo.itemText(0) == expected_first_label
    assert combo.itemText(11) == expected_last_label
