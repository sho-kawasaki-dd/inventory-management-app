from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import combinations
from zoneinfo import ZoneInfo

import pytest

from inventory_manager_mini.core import reports
from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import (
    DashboardRow,
    GroupAxis,
    HistoryPeriod,
    HistoryPeriodKind,
    ItemUpdate,
    NewItem,
    PeriodKind,
)
from inventory_manager_mini.core.reports import ReportService, fiscal_year_of
from inventory_manager_mini.core.services import InventoryService, SettingsService
from inventory_manager_mini.db.integrity import compute_aggregates
from tests.conftest import FixedClock, unchecked_constraints


class GroupBy:
    """旧 `GroupBy` の 4 値に相当する内訳軸の集合。"""

    NONE = frozenset[GroupAxis]()
    CLIENT = frozenset({GroupAxis.CLIENT})
    PURCHASER = frozenset({GroupAxis.PURCHASER})
    CLIENT_PURCHASER = frozenset({GroupAxis.CLIENT, GroupAxis.PURCHASER})


OLD_GROUP_BY = (GroupBy.NONE, GroupBy.CLIENT, GroupBy.PURCHASER, GroupBy.CLIENT_PURCHASER)
ALL_AXIS_SETS = [
    frozenset(combination)
    for size in range(len(GroupAxis) + 1)
    for combination in combinations(GroupAxis, size)
]


def _create_item(
    inventory: InventoryService,
    *,
    name: str,
    client_id: int = 1,
    purchaser_id: int = 1,
    reference_price: int | None = None,
    quantity: int = 10,
) -> int:
    return inventory.create_item(
        NewItem(
            client_id=client_id,
            purchaser_id=purchaser_id,
            name=name,
            category_id=2,
            initial_quantity=quantity,
            initial_staff_id=1 if quantity else None,
            reference_price=reference_price,
        )
    ).id


def _change_item_owners(
    inventory: InventoryService, item_id: int, client_id: int, purchaser_id: int
) -> None:
    item = inventory.get_item(item_id)
    assert item is not None
    inventory.update_item(
        ItemUpdate(
            id=item.id,
            client_id=client_id,
            purchaser_id=purchaser_id,
            name=item.name,
            category_id=item.category_id,
            location_id=item.location_id,
            reorder_threshold=item.reorder_threshold,
            reorder_quantity=item.reorder_quantity,
            purchase_url=item.purchase_url,
            supplier=item.supplier,
            manufacturer_part_number=item.manufacturer_part_number,
            application=item.application,
            reference_price=item.reference_price,
            note=item.note,
        )
    )


def test_dashboard_uses_local_month_boundaries_and_original_reversal_period(
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
) -> None:
    settings = SettingsService(seeded_conn)
    settings.set_fiscal_year_start_month(4)
    fixed_clock.current = datetime(2025, 3, 31, 14, 58, tzinfo=UTC)
    inventory = InventoryService(seeded_conn, clock=fixed_clock)
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item = inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="境界テスト",
            category_id=2,
            initial_quantity=1,
            initial_staff_id=1,
        )
    )

    fixed_clock.current = datetime(2025, 3, 31, 14, 59, tzinfo=UTC)
    inventory.receive(item.id, 1, 2, unit_price=100)
    issue = inventory.issue(item.id, 1, 1, "月またぎ")
    fixed_clock.current = datetime(2025, 3, 31, 15, 0, tzinfo=UTC)
    inventory.receive(item.id, 1, 3, unit_price=100)
    fixed_clock.current = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
    inventory.reverse(issue.id, 1)

    result = report.dashboard_result(2024, PeriodKind.MONTHLY)
    rows = result.breakdown
    march = next(row for row in rows if row.period_label == "2025年3月")
    march_total = next(row for row in result.totals if row.period_label == "2025年3月")
    april = report.dashboard(2025, PeriodKind.MONTHLY)[0]

    assert (march.inbound_quantity, march.outbound_quantity) == (2, 0)
    assert (march_total.inbound_quantity, march_total.outbound_quantity) == (2, 0)
    assert (april.period_label, april.inbound_quantity, april.outbound_quantity) == (
        "2025年4月",
        3,
        0,
    )
    assert len(rows) == 12
    assert fiscal_year_of(datetime(2026, 3, 31).date(), 4) == 2025
    assert report.dashboard(2024, PeriodKind.ANNUAL, group_by=GroupBy.NONE)[0].inbound_quantity == 2
    assert report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.NONE)[0].inbound_quantity == 3


def test_dashboard_aggregates_all_grouping_axes_and_combined_filters(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    master,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    first_id = _create_item(
        inventory, name="品目A", client_id=1, purchaser_id=1, reference_price=10
    )
    second_id = _create_item(
        inventory, name="品目B", client_id=1, purchaser_id=2, reference_price=20
    )
    third_id = _create_item(
        inventory, name="品目C", client_id=2, purchaser_id=1, reference_price=30
    )
    inventory.receive(first_id, 1, 5)
    inventory.receive(second_id, 1, 4)
    inventory.receive(third_id, 1, 3)
    inventory.issue(first_id, 1, 1, "用途A")
    inventory.issue(second_id, 1, 2, "用途B")
    inventory.issue(third_id, 1, 3, "用途C")
    _change_item_owners(inventory, first_id, 2, 2)
    master.deactivate_client(2)
    master.deactivate_purchaser(1)

    ungrouped = report.dashboard(2025, PeriodKind.ANNUAL)[0]
    by_client = report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT)
    by_purchaser = report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.PURCHASER)
    by_pair = report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT_PURCHASER)
    for rows in (by_client, by_purchaser, by_pair):
        for field in (
            "inbound_quantity",
            "outbound_quantity",
            "expenditure",
            "unpriced_issue_quantity",
            "disposed_quantity",
            "disposal_amount",
        ):
            assert sum(getattr(row, field) for row in rows) == getattr(ungrouped, field)
    assert ungrouped.outbound_quantity == 6
    assert all(row.purchaser_id is None for row in by_client)
    assert all(row.client_id is None for row in by_purchaser)
    assert (1, 1, 1) in {
        (row.client_id, row.purchaser_id, row.outbound_quantity) for row in by_pair
    }

    filtered = report.dashboard(
        2025,
        PeriodKind.ANNUAL,
        client_id=1,
        purchaser_id=2,
        group_by=GroupBy.CLIENT_PURCHASER,
    )
    assert [(row.client_id, row.purchaser_id, row.outbound_quantity) for row in filtered] == [
        (1, 2, 2)
    ]
    assert report.dashboard(2025, PeriodKind.ANNUAL, client_id=2)[0].outbound_quantity == 3


def test_dashboard_excludes_returns_adjustments_and_reversed_unpriced_issues(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _create_item(inventory, name="無単価", reference_price=None)
    unpriced_issue = inventory.issue(item_id, 1, 1, "取消する出庫")
    inventory.issue(item_id, 1, 2, "単価未登録")
    inventory.reverse(unpriced_issue.id, 1)
    inventory.return_to_supplier(item_id, 1, 1)
    inventory.stocktake(item_id, 1, 8)
    priced_id = _create_item(inventory, name="単価あり", reference_price=50)
    inventory.issue(priced_id, 1, 2, "費用")
    inventory.dispose(priced_id, 1, 1)
    zero_price_id = _create_item(inventory, name="0円", reference_price=0)
    inventory.issue(zero_price_id, 1, 1, "0円費用")

    row = report.dashboard_result(2025, PeriodKind.ANNUAL).totals[0]
    assert row.outbound_quantity == 5
    assert row.expenditure == 100
    assert row.unpriced_issue_quantity == 2
    assert row.disposed_quantity == 1
    assert row.disposal_amount == 50


def test_dashboard_uses_dst_aware_month_ranges_and_year_setting(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(1)
    report = ReportService(seeded_conn, tz=ZoneInfo("America/New_York"), clock=fixed_clock)
    item_id = _create_item(inventory, name="DST")
    fixed_clock.current = datetime(2026, 3, 1, 4, 59, tzinfo=UTC)
    inventory.receive(item_id, 1, 1)
    fixed_clock.current = datetime(2026, 3, 1, 5, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 2)
    fixed_clock.current = datetime(2026, 4, 1, 3, 59, tzinfo=UTC)
    inventory.receive(item_id, 1, 3)
    fixed_clock.current = datetime(2026, 4, 1, 4, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 4)

    rows = report.dashboard(2026, PeriodKind.MONTHLY)
    march = next(row for row in rows if row.period_label == "2026年3月")
    february = next(row for row in rows if row.period_label == "2026年2月")
    april = next(row for row in rows if row.period_label == "2026年4月")
    assert (february.inbound_quantity, march.inbound_quantity, april.inbound_quantity) == (1, 5, 4)
    assert report.current_fiscal_year() == 2026


def test_available_fiscal_years_include_history_range_and_current_year(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(4)
    fixed_clock.current = datetime(2023, 4, 1, 0, 0, tzinfo=UTC)
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _create_item(inventory, name="年度一覧")
    fixed_clock.current = datetime(2023, 4, 1, 0, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 1)
    fixed_clock.current = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 1)
    fixed_clock.current = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)

    assert report.available_fiscal_years() == [2023, 2024, 2025, 2026]
    assert fiscal_year_of(datetime(2025, 4, 1).date(), 4) == 2025


def test_changing_fiscal_year_start_month_changes_annual_periods(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(4)
    fixed_clock.current = datetime(2025, 4, 10, tzinfo=UTC)
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _create_item(inventory, name="年度境界")
    fixed_clock.current = datetime(2026, 3, 31, 14, 59, tzinfo=UTC)
    inventory.receive(item_id, 1, 2)
    fixed_clock.current = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 3)

    assert fiscal_year_of(datetime(2026, 3, 31).date(), 4) == 2025
    assert fiscal_year_of(datetime(2026, 4, 1).date(), 4) == 2026
    assert fiscal_year_of(datetime(2027, 4, 1).date(), 4) == 2027
    assert report.dashboard(2025, PeriodKind.ANNUAL)[0].inbound_quantity == 2
    assert report.dashboard(2026, PeriodKind.ANNUAL)[0].inbound_quantity == 3
    settings.set_fiscal_year_start_month(1)
    assert report.dashboard(2026, PeriodKind.ANNUAL)[0].inbound_quantity == 5
    assert report.current_fiscal_year() == 2026


def _new_report_item(
    inventory: InventoryService, name: str, *, quantity: int = 0, price: int | None = None
) -> int:
    return inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name=name,
            category_id=2,
            initial_quantity=quantity,
            initial_staff_id=1 if quantity else None,
            reference_price=price,
        )
    ).id


def _record(inventory: InventoryService, kind: str, item_id: int, quantity: int) -> None:
    if kind == "receive":
        inventory.receive(item_id, 1, quantity)
    elif kind == "issue":
        inventory.issue(item_id, 1, quantity, "用途")
    else:
        inventory.dispose(item_id, 1, quantity)


def test_dashboard_judges_limits_by_final_totals_not_row_order(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reports, "MAX_AGGREGATE_VALUE", 100)
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _new_report_item(inventory, "順序")
    first = inventory.receive(item_id, 1, 60)
    inventory.receive(item_id, 1, 60)
    inventory.reverse(first.id, 1)

    # 同一時刻の行は ID 順に並ぶため、途中合計は 120 まで達するが最終値は 60
    assert report.dashboard(2025, PeriodKind.ANNUAL)[0].inbound_quantity == 60


@pytest.mark.parametrize(
    ("kind", "price", "at_limit", "metric"),
    [
        ("receive", None, 100, "inbound_quantity"),
        ("issue", None, 100, "outbound_quantity"),
        ("issue", 2, 50, "expenditure"),
        ("dispose", None, 100, "disposed_quantity"),
        ("dispose", 2, 50, "disposal_amount"),
    ],
)
def test_dashboard_accepts_totals_at_the_limit_and_rejects_beyond(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    price: int | None,
    at_limit: int,
    metric: str,
) -> None:
    monkeypatch.setattr(reports, "MAX_AGGREGATE_VALUE", 100)
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _new_report_item(inventory, "上限", quantity=200, price=price)

    _record(inventory, kind, item_id, at_limit)
    for period_kind in (PeriodKind.ANNUAL, PeriodKind.MONTHLY):
        result = report.dashboard_result(2025, period_kind)
        assert max(getattr(row, metric) for row in result.totals) == 100

    _record(inventory, kind, item_id, 1)
    for period_kind in (PeriodKind.ANNUAL, PeriodKind.MONTHLY):
        with pytest.raises(ValidationError, match="集計値"):
            report.dashboard_result(2025, period_kind)


def test_dashboard_sums_beyond_int64_with_python_integers(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = _new_report_item(inventory, "巨大", quantity=10)
    huge_price = 9 * 10**18
    lot_id = seeded_conn.execute(
        "SELECT id FROM stock_movements WHERE item_id = ? AND delta > 0", (item_id,)
    ).fetchone()[0]
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(
            "UPDATE stock_movements SET unit_price = ? WHERE id = ?", (huge_price, lot_id)
        )
        for _ in range(10):
            cursor = seeded_conn.execute(
                "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, reason, "
                "delta, unit_price, cost_amount, used_for, moved_at) "
                "VALUES (?, 1, 1, 1, 'out', -1, NULL, ?, '巨大', '2026-01-15 12:00:00')",
                (item_id, huge_price),
            )
            seeded_conn.execute(
                "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (?, ?, 1)",
                (cursor.lastrowid, lot_id),
            )
    with pytest.raises(sqlite3.OperationalError):
        seeded_conn.execute(
            "SELECT SUM(a.quantity * lot.unit_price) FROM stock_allocations a "
            "JOIN stock_movements lot ON lot.id = a.lot_id"
        ).fetchone()
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)

    with pytest.raises(ValidationError, match="集計値"):
        report.dashboard(2025, PeriodKind.ANNUAL)

    monkeypatch.setattr(reports, "MAX_AGGREGATE_VALUE", 10**30)
    row = report.dashboard(2025, PeriodKind.ANNUAL)[0]
    assert type(row.expenditure) is int
    assert row.expenditure == 10 * huge_price


def test_dashboard_attributes_costs_to_allocated_lot_purchasers(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _create_item(
        inventory, name="発注主体別ロット", client_id=1, purchaser_id=1, quantity=0
    )
    inventory.receive(item_id, 1, 2, unit_price=10)
    _change_item_owners(inventory, item_id, 2, 2)
    inventory.receive(item_id, 1, 3, unit_price=30)
    inventory.issue(item_id, 1, 1, "出庫")
    inventory.dispose(item_id, 1, 3)

    all_rows = report.dashboard_result(2025, PeriodKind.ANNUAL).totals[0]
    by_purchaser = report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.PURCHASER)
    purchaser_totals = {row.purchaser_id: row for row in by_purchaser}
    assert (all_rows.outbound_quantity, all_rows.expenditure) == (1, 10)
    assert (all_rows.disposed_quantity, all_rows.disposal_amount) == (3, 70)
    assert (
        purchaser_totals[1].outbound_quantity,
        purchaser_totals[1].expenditure,
        purchaser_totals[1].disposed_quantity,
        purchaser_totals[1].disposal_amount,
    ) == (1, 10, 1, 10)
    assert (
        purchaser_totals[2].outbound_quantity,
        purchaser_totals[2].expenditure,
        purchaser_totals[2].disposed_quantity,
        purchaser_totals[2].disposal_amount,
    ) == (0, 0, 2, 60)

    by_pair = report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT_PURCHASER)
    for field in (
        "inbound_quantity",
        "outbound_quantity",
        "expenditure",
        "unpriced_issue_quantity",
        "disposed_quantity",
        "disposal_amount",
    ):
        assert sum(getattr(row, field) for row in by_pair) == getattr(all_rows, field)

    filtered = report.dashboard(
        2025,
        PeriodKind.ANNUAL,
        client_id=2,
        purchaser_id=1,
        group_by=GroupBy.CLIENT_PURCHASER,
    )
    assert len(filtered) == 1
    assert (filtered[0].outbound_quantity, filtered[0].disposed_quantity) == (1, 1)


def test_dashboard_counts_unpriced_issue_quantity_and_reversal_by_original_period(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(1)
    fixed_clock.current = datetime(2025, 12, 31, 23, 59, tzinfo=UTC)
    item_id = _create_item(inventory, name="単価不明数量", reference_price=None)
    issue = inventory.issue(item_id, 1, 3, "月またぎ取消")
    inventory.issue(item_id, 1, 2, "未取消")
    fixed_clock.current = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    inventory.reverse(issue.id, 1)

    december = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock).dashboard(
        2025, PeriodKind.MONTHLY
    )[-1]
    january = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock).dashboard(
        2025, PeriodKind.MONTHLY
    )[0]
    assert (december.outbound_quantity, december.unpriced_issue_quantity) == (2, 2)
    assert (january.outbound_quantity, january.unpriced_issue_quantity) == (0, 0)


def test_dashboard_attributes_outflow_to_issue_period_across_lot_periods(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(4)
    fixed_clock.current = datetime(2025, 12, 30, tzinfo=UTC)
    item_id = _create_item(inventory, name="期間をまたぐロット", quantity=0)
    inventory.receive(item_id, 1, 2, unit_price=10)
    fixed_clock.current = datetime(2026, 1, 2, tzinfo=UTC)
    inventory.receive(item_id, 1, 2, unit_price=20)
    fixed_clock.current = datetime(2026, 1, 3, tzinfo=UTC)
    inventory.issue(item_id, 1, 3, "期間をまたぐ引当")

    rows = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock).dashboard(
        2025, PeriodKind.MONTHLY
    )
    december = next(row for row in rows if row.period_label == "2025年12月")
    january = next(row for row in rows if row.period_label == "2026年1月")
    assert (december.inbound_quantity, december.outbound_quantity, december.expenditure) == (
        2,
        0,
        0,
    )
    assert (january.inbound_quantity, january.outbound_quantity, january.expenditure) == (
        2,
        3,
        40,
    )


def test_monthly_annual_and_total_aggregates_agree_across_months(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _new_report_item(inventory, "月次", price=10)
    july_issue = None
    for moment in (
        datetime(2025, 4, 10, tzinfo=UTC),
        datetime(2025, 7, 20, tzinfo=UTC),
        datetime(2025, 12, 31, 16, 0, tzinfo=UTC),
        datetime(2026, 3, 5, tzinfo=UTC),
    ):
        fixed_clock.current = moment
        inventory.receive(item_id, 1, 20)
        issue = inventory.issue(item_id, 1, 3, "用途")
        inventory.dispose(item_id, 1, 2)
        inventory.return_to_supplier(item_id, 1, 1)
        inventory.stocktake(item_id, 1, 13)
        if moment.month == 7:
            july_issue = issue
    assert july_issue is not None
    fixed_clock.current = datetime(2026, 3, 20, tzinfo=UTC)
    inventory.reverse(july_issue.id, 1)

    monthly = report.dashboard(2025, PeriodKind.MONTHLY)
    annual = report.dashboard(2025, PeriodKind.ANNUAL)[0]
    totals = inventory.aggregates.get()
    assert compute_aggregates(seeded_conn) == totals
    for field in (
        "inbound_quantity",
        "outbound_quantity",
        "expenditure",
        "disposed_quantity",
        "disposal_amount",
    ):
        assert sum(getattr(row, field) for row in monthly) == getattr(annual, field)
        assert getattr(annual, field) == totals[field]
    assert next(row for row in monthly if row.period_label == "2025年7月").outbound_quantity == 0


def test_dashboard_result_all_fiscal_years_and_totals_match_breakdown(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(4)
    fixed_clock.current = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    item_id = _create_item(inventory, name="年度推移", quantity=0, reference_price=10)
    fixed_clock.current = datetime(2024, 3, 31, 23, 59, tzinfo=UTC)
    inventory.receive(item_id, 1, 2)
    fixed_clock.current = datetime(2024, 4, 1, 0, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 3)
    fixed_clock.current = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 4, unit_price=10)
    inventory.issue(item_id, 1, 2, "当日出庫")
    inventory.dispose(item_id, 1, 1)
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)

    result = report.dashboard_result(None, PeriodKind.ANNUAL)

    assert report.current_fiscal_year() == 2026
    assert [row.period_label for row in result.totals] == [
        "2023年度",
        "2024年度",
        "2025年度",
        "2026年度",
    ]
    assert [row.inbound_quantity for row in result.totals] == [2, 3, 0, 4]
    for fiscal_year, row in zip(range(2023, 2027), result.totals, strict=True):
        assert report.dashboard(fiscal_year, PeriodKind.ANNUAL)[0] == row
    assert result.totals[-1].period_start.isoformat() == "2026-04-01"
    assert (
        result.totals[-1].inbound_quantity,
        result.totals[-1].outbound_quantity,
        result.totals[-1].expenditure,
        result.totals[-1].disposed_quantity,
        result.totals[-1].disposal_amount,
    ) == (4, 2, 20, 1, 10)
    october = next(
        row
        for row in report.dashboard_result(2026, PeriodKind.MONTHLY).totals
        if row.period_label == "2026年10月"
    )
    assert (
        october.inbound_quantity,
        october.outbound_quantity,
        october.expenditure,
        october.disposed_quantity,
        october.disposal_amount,
    ) == (4, 2, 20, 1, 10)
    metric_fields = (
        "inbound_quantity",
        "outbound_quantity",
        "expenditure",
        "unpriced_issue_quantity",
        "disposed_quantity",
        "disposal_amount",
    )
    for kind, fiscal_year in (
        (PeriodKind.ANNUAL, None),
        (PeriodKind.MONTHLY, 2026),
    ):
        expected = (
            result if kind is PeriodKind.ANNUAL else report.dashboard_result(fiscal_year, kind)
        )
        for group_by in OLD_GROUP_BY:
            grouped = report.dashboard_result(fiscal_year, kind, group_by=group_by)
            assert grouped.totals == expected.totals
            for field in metric_fields:
                assert [
                    sum(
                        getattr(row, field)
                        for row in grouped.breakdown
                        if row.period_label == total.period_label
                    )
                    for total in grouped.totals
                ] == [getattr(row, field) for row in grouped.totals]

    settings.set_fiscal_year_start_month(1)
    calendar_years = report.dashboard_result(None, PeriodKind.ANNUAL)
    assert report.current_fiscal_year() == 2026
    assert [row.period_label for row in calendar_years.totals] == [
        "2024年度",
        "2025年度",
        "2026年度",
    ]
    assert [row.inbound_quantity for row in calendar_years.totals] == [5, 0, 4]
    for fiscal_year, row in zip(range(2024, 2027), calendar_years.totals, strict=True):
        assert report.dashboard(fiscal_year, PeriodKind.ANNUAL)[0] == row


def test_dashboard_result_keeps_filtered_axis_names_and_validates_arguments(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    item_id = _create_item(inventory, name="絞り込み", client_id=1, purchaser_id=2, quantity=0)
    inventory.receive(item_id, 1, 2)

    filtered = report.dashboard_result(
        2025, PeriodKind.ANNUAL, client_id=1, purchaser_id=2, group_by=GroupBy.NONE
    )
    assert (
        filtered.breakdown[0].client_id,
        filtered.breakdown[0].client_name,
        filtered.breakdown[0].purchaser_id,
        filtered.breakdown[0].purchaser_name,
    ) == (1, "総務", 2, "部門")
    assert filtered.totals[0].inbound_quantity == 2
    grouped_filter = report.dashboard_result(
        2025,
        PeriodKind.ANNUAL,
        client_id=1,
        purchaser_id=2,
        group_by=GroupBy.CLIENT,
    )
    assert (
        grouped_filter.breakdown[0].client_id,
        grouped_filter.breakdown[0].client_name,
        grouped_filter.breakdown[0].purchaser_id,
        grouped_filter.breakdown[0].purchaser_name,
    ) == (1, "総務", 2, "部門")

    with pytest.raises(ValidationError, match="クライアントが見つかりません"):
        report.dashboard_result(2025, PeriodKind.ANNUAL, client_id=999)
    with pytest.raises(ValidationError, match="発注主体が見つかりません"):
        report.dashboard_result(2025, PeriodKind.ANNUAL, purchaser_id=999)
    with pytest.raises(ValueError, match="年度を省略できるのは年次"):
        report.dashboard_result(None, PeriodKind.MONTHLY)


_METRIC_FIELDS = (
    "inbound_quantity",
    "outbound_quantity",
    "expenditure",
    "unpriced_issue_quantity",
    "disposed_quantity",
    "disposal_amount",
)


def _metrics(row: DashboardRow) -> tuple[int, ...]:
    return tuple(getattr(row, field) for field in _METRIC_FIELDS)


@pytest.mark.parametrize(
    ("local_day", "start_month", "expected"),
    [
        (date(2025, 12, 31), 1, 2025),
        (date(2026, 1, 1), 1, 2026),
        (date(2026, 11, 30), 12, 2025),
        (date(2026, 12, 1), 12, 2026),
        (date(2026, 3, 31), 4, 2025),
        (date(2026, 4, 1), 4, 2026),
    ],
)
def test_fiscal_year_of_uses_start_year_at_boundaries(
    local_day: date, start_month: int, expected: int
) -> None:
    assert fiscal_year_of(local_day, start_month) == expected


@pytest.mark.parametrize("start_month", [0, 13, True, "4"])
def test_fiscal_year_of_rejects_invalid_start_month(start_month: object) -> None:
    with pytest.raises(ValueError, match="年度開始月"):
        fiscal_year_of(date(2026, 4, 1), start_month)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("start_month", "first_label", "last_label"),
    [
        (1, "2026年1月", "2026年12月"),
        (4, "2026年4月", "2027年3月"),
        (12, "2026年12月", "2027年11月"),
    ],
)
def test_dashboard_periods_span_from_fiscal_year_start_month(
    seeded_conn: sqlite3.Connection,
    settings: SettingsService,
    fixed_clock: FixedClock,
    start_month: int,
    first_label: str,
    last_label: str,
) -> None:
    settings.set_fiscal_year_start_month(start_month)
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)

    totals = report.dashboard_result(2026, PeriodKind.MONTHLY).totals
    annual = report.dashboard_result(2026, PeriodKind.ANNUAL).totals

    assert len(totals) == 12
    assert (totals[0].period_label, totals[-1].period_label) == (first_label, last_label)
    assert totals[0].period_start == date(2026, start_month, 1)
    for row in totals:
        assert row.period_label == f"{row.period_start.year}年{row.period_start.month}月"
    for previous, current in zip(totals, totals[1:], strict=False):
        assert (current.period_start.year * 12 + current.period_start.month) == (
            previous.period_start.year * 12 + previous.period_start.month + 1
        )
    assert [(row.period_label, row.period_start) for row in annual] == [
        ("2026年度", date(2026, start_month, 1))
    ]


def test_dashboard_splits_mixed_priced_and_unpriced_lots_in_one_operation(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _new_report_item(inventory, "混在ロット")
    inventory.receive(item_id, 1, 2)
    inventory.receive(item_id, 1, 3, unit_price=10)
    inventory.issue(item_id, 1, 4, "単価不明ロットを含む出庫")
    inventory.receive(item_id, 1, 2)
    inventory.dispose(item_id, 1, 3)

    result = report.dashboard_result(2025, PeriodKind.ANNUAL)

    # 出庫は FIFO で不明ロット 2 + 10 円ロット 2、廃棄は 10 円ロット 1 + 不明ロット 2
    assert _metrics(result.totals[0]) == (7, 4, 20, 2, 3, 10)
    for group_by in OLD_GROUP_BY:
        grouped = report.dashboard_result(2025, PeriodKind.ANNUAL, group_by=group_by)
        assert grouped.totals == result.totals
        assert [sum(_metrics(row)[index] for row in grouped.breakdown) for index in range(6)] == (
            list(_metrics(result.totals[0]))
        )


def test_dashboard_reversals_of_receipts_and_disposals_net_out_in_original_period(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(1)
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    fixed_clock.current = datetime(2025, 12, 31, 12, 0, tzinfo=UTC)
    receipt_item = _new_report_item(inventory, "入庫取消")
    cancelled_receipt = inventory.receive(receipt_item, 1, 5, unit_price=10)
    dispose_item = _new_report_item(inventory, "廃棄取消")
    inventory.receive(dispose_item, 1, 3, unit_price=10)
    cancelled_disposal = inventory.dispose(dispose_item, 1, 2)
    fixed_clock.current = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    inventory.reverse(cancelled_receipt.id, 1)
    inventory.reverse(cancelled_disposal.id, 1)

    december = report.dashboard_result(2025, PeriodKind.MONTHLY).totals[-1]
    january = report.dashboard_result(2026, PeriodKind.MONTHLY).totals[0]

    assert (december.period_label, _metrics(december)) == ("2025年12月", (3, 0, 0, 0, 0, 0))
    assert (january.period_label, _metrics(january)) == ("2026年1月", (0, 0, 0, 0, 0, 0))
    assert _metrics(report.dashboard_result(2026, PeriodKind.ANNUAL).totals[0]) == (0,) * 6


def test_dashboard_result_multi_row_breakdown_matches_totals_in_order(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(4)
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    items = {
        "A": _create_item(inventory, name="A", client_id=1, purchaser_id=1, quantity=0),
        "B": _create_item(inventory, name="B", client_id=1, purchaser_id=2, quantity=0),
        "C": _create_item(inventory, name="C", client_id=2, purchaser_id=1, quantity=0),
    }
    fixed_clock.current = datetime(2025, 12, 15, 12, 0, tzinfo=UTC)
    inventory.receive(items["A"], 1, 5, unit_price=10)
    inventory.issue(items["A"], 1, 2, "用途")
    inventory.receive(items["B"], 1, 4, unit_price=20)
    inventory.dispose(items["B"], 1, 1)
    inventory.receive(items["C"], 1, 3, unit_price=30)
    inventory.issue(items["C"], 1, 1, "用途")
    fixed_clock.current = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    inventory.receive(items["A"], 1, 2, unit_price=10)
    inventory.issue(items["A"], 1, 3, "用途")
    inventory.issue(items["B"], 1, 2, "用途")
    inventory.dispose(items["C"], 1, 1)

    expected_rows = {
        GroupBy.NONE: 12,
        GroupBy.CLIENT: 4,
        GroupBy.PURCHASER: 4,
        GroupBy.CLIENT_PURCHASER: 6,
    }
    for kind in (PeriodKind.MONTHLY, PeriodKind.ANNUAL):
        baseline = report.dashboard_result(2025, kind)
        for group_by, monthly_row_count in expected_rows.items():
            result = report.dashboard_result(2025, kind, group_by=group_by)
            assert result.totals == baseline.totals
            for total in result.totals:
                members = [
                    row for row in result.breakdown if row.period_label == total.period_label
                ]
                assert [sum(_metrics(row)[index] for row in members) for index in range(6)] == (
                    list(_metrics(total))
                )
            keys = [
                (row.period_start, row.client_id or 0, row.purchaser_id or 0)
                for row in result.breakdown
            ]
            assert keys == sorted(keys)
            assert report.dashboard(2025, kind, group_by=group_by) == list(result.breakdown)
            if kind is PeriodKind.MONTHLY:
                # 内訳なしは全期間を 0 で埋め、内訳軸ありは実績のある行だけを返す
                assert len(result.breakdown) == monthly_row_count

    monthly = report.dashboard_result(2025, PeriodKind.MONTHLY).totals
    december = next(row for row in monthly if row.period_label == "2025年12月")
    january = next(row for row in monthly if row.period_label == "2026年1月")
    assert _metrics(december) == (12, 3, 50, 0, 1, 20)
    assert _metrics(january) == (2, 5, 70, 0, 1, 30)


def test_dashboard_result_filtered_axes_appear_on_totals_and_breakdown(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    item_id = _create_item(inventory, name="片軸", client_id=1, purchaser_id=2, quantity=0)
    inventory.receive(item_id, 1, 2)
    annual = PeriodKind.ANNUAL

    def keys(
        rows: tuple[DashboardRow, ...],
    ) -> set[tuple[int | None, str | None, int | None, str | None]]:
        return {
            (row.client_id, row.client_name, row.purchaser_id, row.purchaser_name) for row in rows
        }

    only_client = report.dashboard_result(2025, annual, client_id=1)
    assert keys(only_client.totals) == keys(only_client.breakdown) == {(1, "総務", None, None)}
    only_purchaser = report.dashboard_result(2025, annual, purchaser_id=2)
    assert (
        keys(only_purchaser.totals) == keys(only_purchaser.breakdown) == {(None, None, 2, "部門")}
    )
    by_purchaser = report.dashboard_result(2025, annual, client_id=1, group_by=GroupBy.PURCHASER)
    assert keys(by_purchaser.totals) == {(1, "総務", None, None)}
    assert keys(by_purchaser.breakdown) == {(1, "総務", 2, "部門")}
    by_client = report.dashboard_result(2025, annual, purchaser_id=2, group_by=GroupBy.CLIENT)
    assert keys(by_client.totals) == {(None, None, 2, "部門")}
    assert keys(by_client.breakdown) == {(1, "総務", 2, "部門")}
    by_pair = report.dashboard_result(
        2025, annual, client_id=1, purchaser_id=2, group_by=GroupBy.CLIENT_PURCHASER
    )
    assert keys(by_pair.totals) == keys(by_pair.breakdown) == {(1, "総務", 2, "部門")}
    inactive = report.dashboard_result(2025, annual, client_id=3, purchaser_id=3)
    assert keys(inactive.totals) == {(3, "廃止クライアント", 3, "廃止発注主体")}
    assert _metrics(inactive.totals[0]) == (0,) * 6


def test_dashboard_result_without_history_has_zero_totals_and_no_grouped_rows(
    seeded_conn: sqlite3.Connection,
    settings: SettingsService,
    fixed_clock: FixedClock,
) -> None:
    settings.set_fiscal_year_start_month(4)
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)

    all_years = report.dashboard_result(None, PeriodKind.ANNUAL)
    assert [row.period_label for row in all_years.totals] == ["2025年度"]
    assert _metrics(all_years.totals[0]) == (0,) * 6
    assert [row.period_label for row in all_years.breakdown] == ["2025年度"]
    monthly = report.dashboard_result(2025, PeriodKind.MONTHLY, group_by=GroupBy.CLIENT_PURCHASER)
    assert len(monthly.totals) == 12
    assert all(_metrics(row) == (0,) * 6 for row in monthly.totals)
    assert monthly.breakdown == ()


@pytest.mark.parametrize(
    "arguments",
    [
        {"fiscal_year": True},
        {"fiscal_year": "2025"},
        {"fiscal_year": 2025.0},
        {"client_id": 0},
        {"client_id": -1},
        {"client_id": True},
        {"client_id": "1"},
        {"purchaser_id": 0},
        {"purchaser_id": True},
    ],
)
def test_dashboard_result_rejects_invalid_argument_types(
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
    arguments: dict[str, object],
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    call: dict[str, object] = {"fiscal_year": 2025, "kind": PeriodKind.ANNUAL, **arguments}

    with pytest.raises(ValueError, match="整数|正の整数"):
        report.dashboard_result(**call)  # type: ignore[arg-type]


def test_dashboard_result_lists_years_and_aggregates_in_one_read_transaction(
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    original = reports.read_transaction
    entered: list[int] = []

    @contextmanager
    def spy(conn: sqlite3.Connection) -> Iterator[None]:
        entered.append(1)
        with original(conn):
            yield

    monkeypatch.setattr(reports, "read_transaction", spy)

    report.dashboard_result(None, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT_PURCHASER)

    assert entered == [1]


@dataclass(frozen=True)
class _AxisEnv:
    report: ReportService
    inventory: InventoryService
    items: dict[str, int]


@pytest.fixture
def axis_env(
    seeded_conn: sqlite3.Connection, inventory: InventoryService, fixed_clock: FixedClock
) -> _AxisEnv:
    # 備品(1) > 文具(2) > ペン(3) > ボールペン(5)、備品(1) > 工具(4)
    seeded_conn.executemany(
        "INSERT INTO categories (id, parent_id, name, code_prefix, next_seq) "
        "VALUES (?, ?, ?, ?, ?)",
        [(3, 2, "ペン", "PN", 1), (4, 1, "工具", "TL", 1), (5, 3, "ボールペン", "BP", 1)],
    )
    items: dict[str, int] = {}
    for name, category_id, client_id, purchaser_id, price, received, issued in (
        ("A", 2, 1, 1, 10, 10, 2),
        ("B", 3, 1, 2, 20, 8, 3),
        ("C", 5, 2, 1, 30, 6, 1),
        ("D", 4, 1, 1, 40, 5, 4),
    ):
        item_id = inventory.create_item(
            NewItem(
                client_id=client_id,
                purchaser_id=purchaser_id,
                name=name,
                category_id=category_id,
                reference_price=price,
            )
        ).id
        inventory.receive(item_id, 1, received, unit_price=price)
        inventory.issue(item_id, 1, issued, "用途")
        items[name] = item_id
    inventory.dispose(items["B"], 1, 1)
    report = ReportService(seeded_conn, tz=ZoneInfo("UTC"), clock=fixed_clock)
    return _AxisEnv(report, inventory, items)


def _annual_total(env: _AxisEnv, **filters: int) -> tuple[int, ...]:
    return _metrics(env.report.dashboard_result(2025, PeriodKind.ANNUAL, **filters).totals[0])  # type: ignore[arg-type]


def test_dashboard_result_filters_by_item(axis_env: _AxisEnv) -> None:
    result = axis_env.report.dashboard_result(2025, PeriodKind.ANNUAL, item_id=axis_env.items["B"])

    assert _metrics(result.totals[0]) == (8, 3, 60, 0, 1, 20)
    row = result.breakdown[0]
    assert (row.item_id, row.item_code, row.item_name) == (axis_env.items["B"], "PN-0001", "B")
    assert (row.client_id, row.purchaser_id, row.category_id) == (None, None, None)
    assert result.totals[0].item_id == axis_env.items["B"]


def test_dashboard_result_category_filter_includes_descendants_only(axis_env: _AxisEnv) -> None:
    assert _annual_total(axis_env, category_id=1) == (29, 10, 270, 0, 1, 20)
    assert _annual_total(axis_env, category_id=2) == (24, 6, 110, 0, 1, 20)
    assert _annual_total(axis_env, category_id=3) == (14, 4, 90, 0, 1, 20)
    assert _annual_total(axis_env, category_id=5) == (6, 1, 30, 0, 0, 0)
    assert _annual_total(axis_env, category_id=4) == (5, 4, 160, 0, 0, 0)
    row = axis_env.report.dashboard_result(2025, PeriodKind.ANNUAL, category_id=3).breakdown[0]
    assert (row.category_id, row.category_name) == (3, "備品 > 文具 > ペン")
    assert (row.item_id, row.item_code, row.item_name) == (None, None, None)


def test_dashboard_result_item_and_category_filters_are_anded(axis_env: _AxisEnv) -> None:
    items = axis_env.items

    assert _annual_total(axis_env, item_id=items["A"], category_id=3) == (0,) * 6
    assert _annual_total(axis_env, item_id=items["B"], category_id=3) == (8, 3, 60, 0, 1, 20)
    assert _annual_total(axis_env, item_id=items["C"], category_id=3) == (6, 1, 30, 0, 0, 0)
    assert _annual_total(
        axis_env, client_id=1, purchaser_id=2, category_id=2, item_id=items["B"]
    ) == (8, 3, 60, 0, 1, 20)
    assert (
        _annual_total(axis_env, client_id=2, purchaser_id=2, category_id=2, item_id=items["B"])
        == (0,) * 6
    )
    empty = axis_env.report.dashboard_result(
        2025, PeriodKind.MONTHLY, item_id=items["A"], category_id=4
    )
    assert len(empty.totals) == len(empty.breakdown) == 12
    assert all(_metrics(row) == (0,) * 6 for row in empty.breakdown)


def test_dashboard_result_uses_the_current_category_of_the_item(
    axis_env: _AxisEnv, seeded_conn: sqlite3.Connection
) -> None:
    _change_item_category(axis_env.inventory, axis_env.items["B"], 4)

    assert _annual_total(axis_env, category_id=3) == (6, 1, 30, 0, 0, 0)
    assert _annual_total(axis_env, category_id=4) == (13, 7, 220, 0, 1, 20)
    by_category = axis_env.report.dashboard_result(
        2025, PeriodKind.ANNUAL, group_by=frozenset({GroupAxis.CATEGORY})
    ).breakdown
    assert {row.category_name: row.inbound_quantity for row in by_category} == {
        "備品 > 文具": 10,
        "備品 > 工具": 13,
        "備品 > 文具 > ペン > ボールペン": 6,
    }


def _change_item_category(inventory: InventoryService, item_id: int, category_id: int) -> None:
    item = inventory.get_item(item_id)
    assert item is not None
    inventory.update_item(
        ItemUpdate(
            id=item.id,
            client_id=item.client_id,
            purchaser_id=item.purchaser_id,
            name=item.name,
            category_id=category_id,
            location_id=item.location_id,
            reorder_threshold=item.reorder_threshold,
            reorder_quantity=item.reorder_quantity,
            purchase_url=item.purchase_url,
            supplier=item.supplier,
            manufacturer_part_number=item.manufacturer_part_number,
            application=item.application,
            reference_price=item.reference_price,
            note=item.note,
        )
    )


def test_dashboard_result_keeps_inactive_items_in_item_and_category_filters(
    axis_env: _AxisEnv,
) -> None:
    axis_env.inventory.deactivate_item(axis_env.items["B"])

    assert _annual_total(axis_env, item_id=axis_env.items["B"]) == (8, 3, 60, 0, 1, 20)
    assert _annual_total(axis_env, category_id=3) == (14, 4, 90, 0, 1, 20)


@pytest.mark.parametrize("kind", [PeriodKind.ANNUAL, PeriodKind.MONTHLY])
def test_dashboard_result_totals_match_breakdown_for_all_axis_combinations(
    axis_env: _AxisEnv, kind: PeriodKind
) -> None:
    fiscal_year = None if kind is PeriodKind.ANNUAL else 2025
    baseline = axis_env.report.dashboard_result(fiscal_year, kind)
    expected_rows = {}
    assert len(ALL_AXIS_SETS) == 16
    for group_by in ALL_AXIS_SETS:
        result = axis_env.report.dashboard_result(fiscal_year, kind, group_by=group_by)
        assert result.totals == baseline.totals
        for total in result.totals:
            members = [row for row in result.breakdown if row.period_label == total.period_label]
            assert [sum(_metrics(row)[index] for row in members) for index in range(6)] == list(
                _metrics(total)
            )
        if kind is PeriodKind.ANNUAL:
            expected_rows[group_by] = len(result.breakdown)

    if kind is PeriodKind.ANNUAL:
        axes = GroupAxis
        assert expected_rows[frozenset()] == 1  # 履歴のある年度は 2025 年度のみ
        assert expected_rows[frozenset({axes.ITEM})] == 4
        assert expected_rows[frozenset({axes.CATEGORY})] == 4
        assert expected_rows[frozenset({axes.CLIENT})] == 2
        assert expected_rows[frozenset({axes.PURCHASER})] == 2
        assert expected_rows[frozenset({axes.CLIENT, axes.PURCHASER})] == 3
        assert expected_rows[frozenset(axes)] == 4


def test_dashboard_result_group_rows_carry_axis_values_and_filtered_values(
    axis_env: _AxisEnv,
) -> None:
    report = axis_env.report
    by_item = report.dashboard_result(
        2025, PeriodKind.ANNUAL, category_id=3, group_by=frozenset({GroupAxis.ITEM})
    )
    assert [
        (row.category_id, row.category_name, row.item_code, row.client_id)
        for row in by_item.breakdown
    ] == [(3, "備品 > 文具 > ペン", "BP-0001", None), (3, "備品 > 文具 > ペン", "PN-0001", None)]
    assert {(row.category_id, row.item_id) for row in by_item.totals} == {(3, None)}

    by_category = report.dashboard_result(
        2025,
        PeriodKind.ANNUAL,
        item_id=axis_env.items["B"],
        group_by=frozenset({GroupAxis.CATEGORY}),
    )
    (row,) = by_category.breakdown
    assert (row.category_id, row.category_name, row.item_id, row.item_code) == (
        3,
        "備品 > 文具 > ペン",
        axis_env.items["B"],
        "PN-0001",
    )

    both = report.dashboard_result(
        2025, PeriodKind.ANNUAL, group_by=frozenset({GroupAxis.CATEGORY, GroupAxis.ITEM})
    )
    assert [(row.category_name, row.item_code) for row in both.breakdown] == [
        ("備品 > 工具", "TL-0001"),
        ("備品 > 文具", "ST-0001"),
        ("備品 > 文具 > ペン", "PN-0001"),
        ("備品 > 文具 > ペン > ボールペン", "BP-0001"),
    ]


def test_dashboard_result_orders_rows_by_period_client_purchaser_category_and_item(
    axis_env: _AxisEnv,
) -> None:
    result = axis_env.report.dashboard_result(
        2025, PeriodKind.ANNUAL, group_by=frozenset(GroupAxis)
    )

    assert [
        (row.client_id, row.purchaser_id, row.category_name, row.item_code)
        for row in result.breakdown
    ] == [
        (1, 1, "備品 > 工具", "TL-0001"),
        (1, 1, "備品 > 文具", "ST-0001"),
        (1, 2, "備品 > 文具 > ペン", "PN-0001"),
        (2, 1, "備品 > 文具 > ペン > ボールペン", "BP-0001"),
    ]
    monthly = axis_env.report.dashboard_result(
        2025, PeriodKind.MONTHLY, group_by=frozenset(GroupAxis)
    )
    assert [row.period_start for row in monthly.breakdown] == sorted(
        row.period_start for row in monthly.breakdown
    )


def test_dashboard_result_new_axes_keep_unpriced_returns_and_stocktake_semantics(
    axis_env: _AxisEnv,
) -> None:
    inventory = axis_env.inventory
    unpriced_id = inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="E", category_id=2)
    ).id
    inventory.receive(unpriced_id, 1, 4)
    cancelled = inventory.issue(unpriced_id, 1, 1, "取消")
    inventory.issue(unpriced_id, 1, 2, "単価不明")
    inventory.reverse(cancelled.id, 1)
    inventory.return_to_supplier(unpriced_id, 1, 1)
    inventory.stocktake(unpriced_id, 1, 3)

    by_item = axis_env.report.dashboard_result(
        2025, PeriodKind.ANNUAL, group_by=frozenset({GroupAxis.ITEM})
    )
    row = next(row for row in by_item.breakdown if row.item_id == unpriced_id)
    assert _metrics(row) == (4, 2, 0, 2, 0, 0)
    assert _metrics(by_item.totals[0]) == (33, 12, 270, 2, 1, 20)


@pytest.mark.parametrize(
    "arguments",
    [
        {"item_id": 0},
        {"item_id": True},
        {"item_id": "1"},
        {"category_id": 0},
        {"category_id": True},
        {"category_id": 1.0},
        {"group_by": {GroupAxis.CLIENT}},
        {"group_by": [GroupAxis.CLIENT]},
        {"group_by": frozenset({"client"})},
        {"group_by": None},
    ],
)
def test_dashboard_result_rejects_invalid_item_category_and_group_arguments(
    axis_env: _AxisEnv, arguments: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="ID|内訳軸"):
        axis_env.report.dashboard_result(2025, PeriodKind.ANNUAL, **arguments)  # type: ignore[arg-type]


def test_dashboard_result_rejects_unknown_item_and_category(axis_env: _AxisEnv) -> None:
    with pytest.raises(ValidationError, match="品目が見つかりません"):
        axis_env.report.dashboard_result(2025, PeriodKind.ANNUAL, item_id=999)
    with pytest.raises(ValidationError, match="カテゴリが見つかりません"):
        axis_env.report.dashboard_result(2025, PeriodKind.ANNUAL, category_id=999)


def test_dashboard_result_limits_are_checked_on_totals_with_new_axes(
    axis_env: _AxisEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reports, "MAX_AGGREGATE_VALUE", 100)

    with pytest.raises(ValidationError, match="集計値"):
        axis_env.report.dashboard_result(2025, PeriodKind.ANNUAL)
    assert _annual_total(axis_env, category_id=3)[2] == 90
    with pytest.raises(ValidationError, match="集計値"):
        axis_env.report.dashboard_result(
            2025, PeriodKind.ANNUAL, category_id=2, group_by=frozenset({GroupAxis.ITEM})
        )


@pytest.mark.parametrize(
    ("period", "start_month", "expected"),
    [
        (HistoryPeriod(), 4, (None, None)),
        (
            HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2025),
            4,
            ("2025-03-31 15:00:00", "2026-03-31 15:00:00"),
        ),
        (
            HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2025),
            1,
            ("2024-12-31 15:00:00", "2025-12-31 15:00:00"),
        ),
        (
            HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=1),
            4,
            ("2025-12-31 15:00:00", "2026-01-31 15:00:00"),
        ),
        (
            HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=4),
            4,
            ("2025-03-31 15:00:00", "2025-04-30 15:00:00"),
        ),
        (
            HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=12),
            4,
            ("2025-11-30 15:00:00", "2025-12-31 15:00:00"),
        ),
    ],
)
def test_history_utc_range_follows_fiscal_start_and_local_calendar(
    period: HistoryPeriod, start_month: int, expected: tuple[str | None, str | None]
) -> None:
    assert reports._history_utc_range(period, start_month, ZoneInfo("Asia/Tokyo")) == expected


def test_history_utc_range_uses_dst_offsets_of_the_local_zone() -> None:
    period = HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2026, month=3)

    assert reports._history_utc_range(period, 1, ZoneInfo("America/New_York")) == (
        "2026-03-01 05:00:00",
        "2026-04-01 04:00:00",
    )


@pytest.mark.parametrize(
    "period",
    [
        HistoryPeriod(HistoryPeriodKind.ALL, fiscal_year=2025),
        HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2025, month=1),
        HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=True),
        HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025),
        HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=0),
        HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=13),
        HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=True),
    ],
)
def test_history_utc_range_rejects_inconsistent_period(period: HistoryPeriod) -> None:
    with pytest.raises(ValueError):
        reports._history_utc_range(period, 4, ZoneInfo("Asia/Tokyo"))


def test_fiscal_year_months_and_current_local_month_follow_settings_and_zone(
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)

    months = report.fiscal_year_months(2025)
    assert len(months) == 12
    assert months[0] == (4, "2025年4月")
    assert months[9] == (1, "2026年1月")
    assert months[-1] == (3, "2026年3月")

    SettingsService(seeded_conn).set_fiscal_year_start_month(1)
    assert report.fiscal_year_months(2025)[0] == (1, "2025年1月")
    with pytest.raises(ValueError):
        report.fiscal_year_months(True)

    fixed_clock.current = datetime(2026, 1, 31, 14, 59, tzinfo=UTC)
    assert report.current_local_month() == 1
    fixed_clock.current = datetime(2026, 1, 31, 15, 0, tzinfo=UTC)
    assert report.current_local_month() == 2


def _history_item(inventory: InventoryService, name: str) -> int:
    return _create_item(inventory, name=name, quantity=0)


def test_history_page_splits_rows_newest_first_and_clamps_pages(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _history_item(inventory, "大量履歴")
    for index in range(650):
        inventory.receive(item_id, 1, 1, unit_price=10)
        if index % 2:
            fixed_clock.advance(seconds=1)

    first = report.list_history_page(item_id)
    assert (first.total_count, first.total_pages, first.page, first.page_size) == (650, 3, 1, 300)
    assert len(first.rows) == 300
    first_ids = [row.id for row in first.rows]
    assert first_ids == sorted(first_ids, reverse=True)

    last = report.list_history_page(item_id, page=3)
    assert (last.page, len(last.rows)) == (3, 50)
    clamped = report.list_history_page(item_id, page=9)
    assert (clamped.page, len(clamped.rows)) == (3, 50)

    large = report.list_history_page(item_id, page=2, page_size=500)
    assert (large.total_pages, large.page, len(large.rows)) == (2, 2, 150)


def test_history_page_selects_page_containing_movement_and_ignores_unknown_ids(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _history_item(inventory, "選択履歴")
    other_id = _history_item(inventory, "別品目")
    other_movement = inventory.receive(other_id, 1, 1, unit_price=10)
    oldest = inventory.receive(item_id, 1, 1, unit_price=10)
    for _ in range(649):
        inventory.receive(item_id, 1, 1, unit_price=10)

    selected = report.list_history_page(item_id, select_movement_id=oldest.id)
    assert selected.page == 3
    assert oldest.id in {row.id for row in selected.rows}
    assert report.list_history_page(item_id, page=2, select_movement_id=999999).page == 2
    assert report.list_history_page(item_id, page=2, select_movement_id=other_movement.id).page == 2


def test_history_page_is_empty_for_item_without_history_or_unknown_item(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _history_item(inventory, "空履歴")

    for target in (item_id, 999):
        page = report.list_history_page(target, page=4)
        assert page.rows == ()
        assert (page.total_count, page.total_pages, page.page) == (0, 0, 1)


def test_history_page_rejects_invalid_arguments(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _history_item(inventory, "検証履歴")

    with pytest.raises(ValueError):
        report.list_history_page(True)
    with pytest.raises(ValueError):
        report.list_history_page(item_id, page=0)
    with pytest.raises(ValueError):
        report.list_history_page(item_id, page_size=301)
    with pytest.raises(ValueError):
        report.list_history_page(item_id, select_movement_id=0)


def test_history_page_period_filter_keeps_reversals_in_original_period(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _history_item(inventory, "期間履歴")
    # 2025-03-31 23:59 JST（2024 年度）
    fixed_clock.current = datetime(2025, 3, 31, 14, 59, tzinfo=UTC)
    received = inventory.receive(item_id, 1, 1, unit_price=10)
    issued = inventory.issue(item_id, 1, 1, "2024 年度の出庫")
    # 2025-04-01 09:00 JST（2025 年度）に取り消す
    fixed_clock.current = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
    reversal = inventory.reverse(issued.id, 1)
    fixed_clock.current = datetime(2025, 5, 10, 3, 0, tzinfo=UTC)
    later = inventory.receive(item_id, 1, 1, unit_price=10)

    fiscal_2024 = report.list_history_page(
        item_id, period=HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2024)
    )
    fiscal_2025 = report.list_history_page(
        item_id, period=HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2025)
    )
    april_2025 = report.list_history_page(
        item_id, period=HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=4)
    )
    all_rows = report.list_history_page(item_id)

    assert {row.id for row in fiscal_2024.rows} == {received.id, issued.id, reversal.id}
    assert {row.id for row in fiscal_2025.rows} == {later.id}
    assert april_2025.total_count == 0
    assert all_rows.total_count == 4
    assert fiscal_2024.total_count + fiscal_2025.total_count == all_rows.total_count
