from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from inventory_manager_mini.core import reports
from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import GroupBy, ItemUpdate, NewItem, PeriodKind
from inventory_manager_mini.core.reports import ReportService, fiscal_year_of
from inventory_manager_mini.core.services import InventoryService, SettingsService
from tests.conftest import FixedClock, unchecked_constraints


def _create_item(
    inventory: InventoryService,
    *,
    name: str,
    client_id: int = 1,
    purchaser_id: int = 1,
    reference_price: int | None = None,
) -> int:
    return inventory.create_item(
        NewItem(
            client_id=client_id,
            purchaser_id=purchaser_id,
            name=name,
            category_id=2,
            initial_quantity=10,
            initial_staff_id=1,
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

    rows = report.dashboard(2025, PeriodKind.MONTHLY)
    march = next(row for row in rows if row.period_label == "2025年3月")
    april = report.dashboard(2026, PeriodKind.MONTHLY)[0]

    assert (march.inbound_quantity, march.outbound_quantity) == (2, 0)
    assert (april.period_label, april.inbound_quantity, april.outbound_quantity) == (
        "2025年4月",
        3,
        0,
    )
    assert len(rows) == 12
    assert fiscal_year_of(datetime(2026, 3, 31).date(), 4) == 2025
    assert report.dashboard(2025, PeriodKind.ANNUAL, group_by=GroupBy.NONE)[0].inbound_quantity == 2
    assert report.dashboard(2026, PeriodKind.ANNUAL, group_by=GroupBy.NONE)[0].inbound_quantity == 3


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

    ungrouped = report.dashboard(2026, PeriodKind.ANNUAL)[0]
    by_client = report.dashboard(2026, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT)
    by_purchaser = report.dashboard(2026, PeriodKind.ANNUAL, group_by=GroupBy.PURCHASER)
    by_pair = report.dashboard(2026, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT_PURCHASER)
    for rows in (by_client, by_purchaser, by_pair):
        for field in (
            "inbound_quantity",
            "outbound_quantity",
            "expenditure",
            "unpriced_issue_count",
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
        2026,
        PeriodKind.ANNUAL,
        client_id=1,
        purchaser_id=2,
        group_by=GroupBy.CLIENT_PURCHASER,
    )
    assert [(row.client_id, row.purchaser_id, row.outbound_quantity) for row in filtered] == [
        (1, 2, 2)
    ]
    assert report.dashboard(2026, PeriodKind.ANNUAL, client_id=2)[0].outbound_quantity == 3


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

    row = report.dashboard(2026, PeriodKind.ANNUAL)[0]
    assert row.outbound_quantity == 5
    assert row.expenditure == 100
    assert row.unpriced_issue_count == 1
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
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _create_item(inventory, name="年度境界")
    fixed_clock.current = datetime(2026, 3, 31, 14, 59, tzinfo=UTC)
    inventory.receive(item_id, 1, 2)
    fixed_clock.current = datetime(2026, 3, 31, 15, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 3)

    assert report.dashboard(2026, PeriodKind.ANNUAL)[0].inbound_quantity == 2
    assert report.dashboard(2027, PeriodKind.ANNUAL)[0].inbound_quantity == 3
    settings.set_fiscal_year_start_month(1)
    assert report.dashboard(2026, PeriodKind.ANNUAL)[0].inbound_quantity == 5


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
    assert report.dashboard(2026, PeriodKind.ANNUAL)[0].inbound_quantity == 60


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
        rows = report.dashboard(2026, period_kind)
        assert max(getattr(row, metric) for row in rows) == 100

    _record(inventory, kind, item_id, 1)
    for period_kind in (PeriodKind.ANNUAL, PeriodKind.MONTHLY):
        with pytest.raises(ValidationError, match="集計値"):
            report.dashboard(2026, period_kind)


def test_dashboard_sums_beyond_int64_with_python_integers(
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = _new_report_item(inventory, "巨大")
    huge_price = 9 * 10**18
    with unchecked_constraints(seeded_conn):
        seeded_conn.executemany(
            "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, reason, "
            "delta, unit_price, used_for, moved_at) "
            "VALUES (?, 1, 1, 1, 'out', -1, ?, '巨大', '2026-01-15 12:00:00')",
            [(item_id, huge_price)] * 10,
        )
    with pytest.raises(sqlite3.OperationalError):
        seeded_conn.execute("SELECT SUM(unit_price) FROM stock_movements").fetchone()
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)

    with pytest.raises(ValidationError, match="集計値"):
        report.dashboard(2026, PeriodKind.ANNUAL)

    monkeypatch.setattr(reports, "MAX_AGGREGATE_VALUE", 10**30)
    row = report.dashboard(2026, PeriodKind.ANNUAL)[0]
    assert type(row.expenditure) is int
    assert row.expenditure == 10 * huge_price


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
        if moment.month == 7:
            july_issue = issue
    assert july_issue is not None
    fixed_clock.current = datetime(2026, 3, 20, tzinfo=UTC)
    inventory.reverse(july_issue.id, 1)

    monthly = report.dashboard(2026, PeriodKind.MONTHLY)
    annual = report.dashboard(2026, PeriodKind.ANNUAL)[0]
    totals = inventory.aggregates.get()
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
