from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from inventory_manager_mini.core.errors import FileOutputError, ValidationError
from inventory_manager_mini.core.models import (
    DashboardRow,
    GroupAxis,
    HistoryPeriod,
    HistoryPeriodKind,
    ItemFilter,
    NewItem,
    PeriodKind,
)
from inventory_manager_mini.core.reports import ReportService, escape_csv_cell
from inventory_manager_mini.core.services import InventoryService
from tests.conftest import FixedClock


def _item(inventory: InventoryService) -> int:
    return inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="=備品",
            category_id=2,
            initial_quantity=2,
            initial_staff_id=1,
            supplier="+仕入先",
            manufacturer_part_number="-型番",
            application="@用途",
            reference_price=100,
            note="\r備考",
        )
    ).id


def test_escape_csv_cell_protects_all_formula_prefixes() -> None:
    for value in ("=式", "+式", "-式", "@式", "\t式", "\r式"):
        assert escape_csv_cell(value) == "'" + value
    assert escape_csv_cell("通常") == "通常"


def test_item_csv_uses_bom_crlf_quoted_columns_and_local_purchase_date(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _item(inventory)
    fixed_clock.current = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 1, unit_price=125)
    output = tmp_path / "items.csv"

    report.export_items_csv(output, ItemFilter())
    content = output.read_bytes()
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(StringIO(text, newline="")))

    assert content.startswith(b"\xef\xbb\xbf")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert text.startswith('"管理番号","品名","メーカー型番"')
    assert all(line.startswith('"') and line.endswith('"') for line in text.split("\r\n") if line)
    assert rows[0] == [
        "管理番号",
        "品名",
        "メーカー型番",
        "クライアント",
        "発注主体",
        "カテゴリ",
        "保管場所",
        "数量",
        "単位",
        "閾値",
        "推奨発注数",
        "低在庫",
        "参考価格",
        "仕入先",
        "販売ページURL",
        "最終購入日",
        "購入ロット数",
        "状態",
        "用途",
        "備考",
    ]
    assert rows[1][1:3] == ["'=備品", "'-型番"]
    assert rows[1][7] == "3"
    assert rows[1][13] == "'+仕入先"
    assert rows[1][15:17] == ["2026-01-16", "1"]
    assert rows[1][18:20] == ["'@用途", "備考"]


def test_history_csv_formats_local_time_and_leaves_numeric_delta_unescaped(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _item(inventory)
    issue = inventory.issue(item_id, 1, 1, "設備")
    inventory.reverse(issue.id, 1)
    output = tmp_path / "history.csv"

    report.export_history_csv(output, item_id)
    rows = list(csv.reader(StringIO(output.read_text(encoding="utf-8-sig"), newline="")))

    assert rows[0] == [
        "日時",
        "管理番号",
        "品名",
        "操作種別",
        "数量",
        "単価",
        "原価金額",
        "クライアント",
        "発注主体",
        "担当者",
        "使用先",
        "取り消し元ID",
        "取り消し済み",
        "メモ",
    ]
    issue_row = next(row for row in rows[1:] if row[3] == "出庫")
    assert issue_row[0] == "2026-01-15 21:00:00"
    assert issue_row[4] == "-1"
    assert issue_row[5] == ""
    assert issue_row[6] == "100"
    assert issue_row[10] == "設備"
    assert issue_row[12] == "○"


def test_history_csv_exports_saved_costs_and_leaves_other_reasons_blank(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _item(inventory)
    received = inventory.receive(item_id, 1, 1, unit_price=50)
    issued = inventory.issue(item_id, 1, 1, "出庫")
    issue_reversal = inventory.reverse(issued.id, 1)
    disposed = inventory.dispose(item_id, 1, 1)
    dispose_reversal = inventory.reverse(disposed.id, 1)
    returned = inventory.return_to_supplier(item_id, 1, 1)
    return_reversal = inventory.reverse(returned.id, 1)
    stocktake_down = inventory.stocktake(item_id, 1, 2)
    stocktake_down_reversal = inventory.reverse(stocktake_down.id, 1)
    stocktake_up = inventory.stocktake(item_id, 1, 4)
    stocktake_up_reversal = inventory.reverse(stocktake_up.id, 1)
    received_reversal = inventory.reverse(received.id, 1)
    output = tmp_path / "history-cost.csv"

    report.export_history_csv(output, item_id)
    rows = list(csv.reader(StringIO(output.read_text(encoding="utf-8-sig"), newline="")))
    history = inventory.list_history(item_id)
    exported_costs = [row[6] for row in rows[1:]]

    assert exported_costs == [
        "" if movement.cost_amount is None else str(movement.cost_amount) for movement in history
    ]
    costs_by_id = {movement.id: movement.cost_amount for movement in history}
    assert costs_by_id[issued.id] == 100
    assert costs_by_id[issue_reversal.id] == -100
    assert costs_by_id[disposed.id] == 100
    assert costs_by_id[dispose_reversal.id] == -100
    assert all(
        costs_by_id[movement_id] is None
        for movement_id in (
            received.id,
            received_reversal.id,
            returned.id,
            return_reversal.id,
            stocktake_down.id,
            stocktake_down_reversal.id,
            stocktake_up.id,
            stocktake_up_reversal.id,
        )
    )
    assert "'-100" not in output.read_text(encoding="utf-8-sig")


def test_history_csv_preserves_zero_cost_as_numeric_zero(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, clock=fixed_clock)
    item_id = inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="無償品", category_id=2)
    ).id
    received = inventory.receive(item_id, 1, 1, unit_price=0)
    inventory.issue(item_id, 1, 1, "無償配布")
    inventory.reverse(inventory.list_history(item_id)[-1].id, 1)
    output = tmp_path / "zero-cost.csv"

    report.export_history_csv(output, item_id)
    rows = list(csv.reader(StringIO(output.read_text(encoding="utf-8-sig"), newline="")))
    costs = [row[6] for row in rows[1:]]

    assert received.cost_amount is None
    assert costs == ["", "0", "0"]


def test_filtered_history_csv_combines_client_and_period_filters(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    client_one_item = _item(inventory)
    client_two_item = inventory.create_item(
        NewItem(
            client_id=2,
            purchaser_id=1,
            name="経理品",
            category_id=2,
            initial_quantity=1,
            initial_staff_id=1,
        )
    ).id
    inventory.issue(client_two_item, 1, 1, "経理利用")
    fixed_clock.advance(days=40)
    inventory.issue(client_one_item, 1, 1, "2月利用")
    output = tmp_path / "filtered-history.csv"

    def read_rows() -> list[list[str]]:
        return list(csv.reader(StringIO(output.read_text(encoding="utf-8-sig"), newline="")))[1:]

    report.export_history_csv(output)
    assert len(read_rows()) == len(inventory.list_all_history())

    report.export_history_csv(output, client_id=2)
    client_two_rows = read_rows()
    assert [row[2] for row in client_two_rows] == ["経理品", "経理品"]
    assert {row[7] for row in client_two_rows} == {"経理"}

    february = HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=2)
    report.export_history_csv(output, client_id=1, period=february)
    february_rows = read_rows()
    assert [row[3] for row in february_rows] == ["出庫"]
    assert february_rows[0][10] == "2月利用"

    report.export_history_csv(output, client_id=2, period=february)
    assert read_rows() == []

    report.export_history_csv(output, client_id=3)
    assert read_rows() == []

    missing = tmp_path / "missing-client.csv"
    with pytest.raises(ValidationError, match="クライアントが見つかりません"):
        report.export_history_csv(missing, client_id=999)
    assert not missing.exists()

    invalid = tmp_path / "invalid-period.csv"
    with pytest.raises(ValueError):
        report.export_history_csv(
            invalid,
            period=HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=13),
        )
    assert not invalid.exists()


def _read_history_rows(path: Path) -> list[list[str]]:
    return list(csv.reader(StringIO(path.read_text(encoding="utf-8-sig"), newline="")))


def test_history_csv_for_unknown_item_contains_only_header(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    output = tmp_path / "unknown-item.csv"

    report.export_history_csv(output, item_id=9999)
    rows = _read_history_rows(output)

    assert len(rows) == 1
    assert rows[0][0] == "日時"


def test_history_csv_is_ascending_and_combines_item_client_and_period(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    first_item = inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="品目A", category_id=2)
    ).id
    second_item = inventory.create_item(
        NewItem(client_id=2, purchaser_id=1, name="品目B", category_id=2)
    ).id
    inventory.receive(first_item, 1, 1, unit_price=10)
    inventory.receive(second_item, 1, 1, unit_price=10)
    inventory.receive(first_item, 1, 2, unit_price=10)
    fixed_clock.advance(days=40)
    inventory.receive(first_item, 1, 3, unit_price=10)
    output = tmp_path / "combined.csv"
    january = HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=1)

    report.export_history_csv(output, first_item)
    all_rows = _read_history_rows(output)[1:]
    report.export_history_csv(output, first_item, client_id=1, period=january)
    january_rows = _read_history_rows(output)[1:]
    report.export_history_csv(output, first_item, client_id=2, period=january)
    other_client_rows = _read_history_rows(output)[1:]

    times = [row[0] for row in all_rows]
    assert times == sorted(times)
    assert [row[4] for row in all_rows] == ["1", "2", "3"]
    assert [row[4] for row in january_rows] == ["1", "2"]
    assert {row[2] for row in january_rows} == {"品目A"}
    assert other_client_rows == []


def test_history_csv_period_keeps_reversal_with_original_period_and_execution_time(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="年度またぎ", category_id=2)
    ).id
    fixed_clock.current = datetime(2025, 3, 31, 14, 59, tzinfo=UTC)
    inventory.receive(item_id, 1, 1, unit_price=10)
    issued = inventory.issue(item_id, 1, 1, "前年度の出庫")
    fixed_clock.current = datetime(2025, 4, 1, 0, 0, tzinfo=UTC)
    inventory.reverse(issued.id, 1)
    fixed_clock.current = datetime(2025, 5, 10, 3, 0, tzinfo=UTC)
    inventory.receive(item_id, 1, 1, unit_price=10)
    output = tmp_path / "reversal-period.csv"

    for previous_year in (
        HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2024),
        HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2024, month=3),
    ):
        report.export_history_csv(output, item_id, period=previous_year)
        rows = _read_history_rows(output)[1:]
        assert [row[3] for row in rows] == ["入庫", "出庫", "出庫"]
        assert [row[4] for row in rows] == ["1", "-1", "1"]
        assert rows[1][12] == "○"
        assert rows[2][0] == "2025-04-01 09:00:00"
        assert rows[2][11] == str(issued.id)

    for current_year in (
        HistoryPeriod(HistoryPeriodKind.ANNUAL, fiscal_year=2025),
        HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=5),
    ):
        report.export_history_csv(output, item_id, period=current_year)
        rows = _read_history_rows(output)[1:]
        assert [row[3] for row in rows] == ["入庫"]
        assert rows[0][0] == "2025-05-10 12:00:00"
        assert rows[0][11] == ""

    april = HistoryPeriod(HistoryPeriodKind.MONTHLY, fiscal_year=2025, month=4)
    report.export_history_csv(output, item_id, period=april)
    assert _read_history_rows(output)[1:] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"item_id": 0},
        {"item_id": -1},
        {"item_id": True},
        {"client_id": 0},
        {"client_id": -1},
        {"client_id": True},
    ],
)
def test_history_csv_rejects_non_positive_and_bool_ids(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
    kwargs: dict[str, Any],
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    output = tmp_path / "invalid-id.csv"

    with pytest.raises(ValueError):
        report.export_history_csv(output, **kwargs)
    assert not output.exists()


def test_dashboard_csv_marks_collapsed_axes_as_all(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _item(inventory)
    inventory.receive(item_id, 1, 1, unit_price=100)
    output = tmp_path / "dashboard.csv"

    rows = report.dashboard(2025, PeriodKind.ANNUAL, group_by=frozenset({GroupAxis.CLIENT}))
    report.export_dashboard_csv(output, rows)
    rows = list(csv.reader(output.open(encoding="utf-8-sig", newline="")))

    assert rows[0] == [
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
    assert rows[1][1:6] == ["総務", "(すべて)", "(すべて)", "(すべて)", "(すべて)"]
    assert rows[1][6] == "1"


def test_dashboard_csv_exports_filtered_inactive_master_names_safely(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    item_id = _item(inventory)
    inventory.receive(item_id, 1, 1, unit_price=100)
    seeded_conn.execute("UPDATE clients SET name = ?, is_active = 0 WHERE id = 1", ("=顧客",))
    seeded_conn.execute(
        "UPDATE purchasers SET name = ?, is_active = 0 WHERE id = 1", ("+発注主体",)
    )
    rows = report.dashboard_result(2025, PeriodKind.ANNUAL, client_id=1, purchaser_id=1).breakdown
    output = tmp_path / "filtered-dashboard.csv"

    report.export_dashboard_csv(output, rows)
    exported = list(csv.reader(output.open(encoding="utf-8-sig", newline="")))

    assert exported[1][1:6] == ["'=顧客", "'+発注主体", "(すべて)", "(すべて)", "(すべて)"]
    assert exported[1][6:] == ["1", "0", "0", "0", "0", "0"]


def test_csv_replace_failure_preserves_existing_file_and_removes_temporary_file(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ReportService(seeded_conn, clock=fixed_clock)
    _item(inventory)
    output = tmp_path / "existing.csv"
    output.write_bytes(b"existing content")

    def fail_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("inventory_manager_mini.core.reports.os.replace", fail_replace)
    with pytest.raises(FileOutputError, match="replace failed") as error_info:
        report.export_items_csv(output, ItemFilter())

    assert str(output) in error_info.value.message
    assert isinstance(error_info.value.__cause__, OSError)
    assert output.read_bytes() == b"existing content"
    assert list(tmp_path.glob(".existing.csv.*.tmp")) == []

    dashboard_rows = report.dashboard(2025, PeriodKind.ANNUAL)
    with pytest.raises(FileOutputError, match="replace failed"):
        report.export_dashboard_csv(output, dashboard_rows)

    assert output.read_bytes() == b"existing content"
    assert list(tmp_path.glob(".existing.csv.*.tmp")) == []


def _dashboard_row(
    label: str,
    client: tuple[int, str] | None,
    purchaser: tuple[int, str] | None,
    base: int,
    category: tuple[int, str] | None = None,
    item: tuple[int, str, str] | None = None,
) -> DashboardRow:
    return DashboardRow(
        period_label=label,
        period_start=date(2025, 4, 1),
        client_id=None if client is None else client[0],
        client_name=None if client is None else client[1],
        purchaser_id=None if purchaser is None else purchaser[0],
        purchaser_name=None if purchaser is None else purchaser[1],
        category_id=None if category is None else category[0],
        category_name=None if category is None else category[1],
        item_id=None if item is None else item[0],
        item_code=None if item is None else item[1],
        item_name=None if item is None else item[2],
        inbound_quantity=base + 1,
        outbound_quantity=base + 2,
        expenditure=base + 3,
        unpriced_issue_quantity=base + 4,
        disposed_quantity=base + 5,
        disposal_amount=base + 6,
    )


def test_dashboard_csv_writes_given_rows_as_is_without_reaggregating(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    rows = [
        _dashboard_row("2025年度", None, None, 10),
        _dashboard_row("2025年4月", (2, "経理"), None, 20),
        _dashboard_row("2025年5月", None, (1, "本部"), 30),
        _dashboard_row("2025年6月", (1, "総務"), (2, "部門"), 40),
        _dashboard_row("2025年7月", None, None, 50, (2, "=備品 > 文具"), (7, "ST-0001", "@品名")),
    ]
    output = tmp_path / "as-is.csv"

    report.export_dashboard_csv(output, rows)
    content = output.read_bytes()
    text = content.decode("utf-8-sig")
    exported = list(csv.reader(StringIO(text, newline="")))

    assert content.startswith(b"\xef\xbb\xbf")
    assert "\n" not in text.replace("\r\n", "")
    assert all(line.startswith('"') and line.endswith('"') for line in text.split("\r\n") if line)
    all_ = "(すべて)"
    assert exported[1:] == [
        ["2025年度", all_, all_, all_, all_, all_, "11", "12", "13", "14", "15", "16"],
        ["2025年4月", "経理", all_, all_, all_, all_, "21", "22", "23", "24", "25", "26"],
        ["2025年5月", all_, "本部", all_, all_, all_, "31", "32", "33", "34", "35", "36"],
        ["2025年6月", "総務", "部門", all_, all_, all_, "41", "42", "43", "44", "45", "46"],
        [
            "2025年7月",
            all_,
            all_,
            "'=備品 > 文具",
            "ST-0001",
            "'@品名",
            "51",
            "52",
            "53",
            "54",
            "55",
            "56",
        ],
    ]


def test_dashboard_csv_with_no_rows_contains_only_the_header(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, clock=fixed_clock)
    output = tmp_path / "empty.csv"

    report.export_dashboard_csv(output, [])

    assert len(list(csv.reader(output.open(encoding="utf-8-sig", newline="")))) == 1


@pytest.mark.parametrize(
    ("client_id", "purchaser_id", "group_by", "expected"),
    [
        (None, None, frozenset[GroupAxis](), ["(すべて)", "(すべて)"]),
        (None, None, frozenset({GroupAxis.PURCHASER}), ["(すべて)", "本部"]),
        (None, None, frozenset({GroupAxis.CLIENT}), ["総務", "(すべて)"]),
        (1, None, frozenset[GroupAxis](), ["総務", "(すべて)"]),
        (None, 1, frozenset[GroupAxis](), ["(すべて)", "本部"]),
        (1, None, frozenset({GroupAxis.PURCHASER}), ["総務", "本部"]),
        (None, 1, frozenset({GroupAxis.CLIENT}), ["総務", "本部"]),
        (1, 1, frozenset({GroupAxis.CLIENT, GroupAxis.PURCHASER}), ["総務", "本部"]),
    ],
)
def test_dashboard_csv_shows_names_for_filtered_axes_and_all_for_unfiltered_collapsed_axes(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    inventory: InventoryService,
    fixed_clock: FixedClock,
    client_id: int | None,
    purchaser_id: int | None,
    group_by: frozenset[GroupAxis],
    expected: list[str],
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    inventory.receive(
        inventory.create_item(
            NewItem(client_id=1, purchaser_id=1, name="絞り込み", category_id=2)
        ).id,
        1,
        1,
    )
    output = tmp_path / "filtered.csv"

    rows = report.dashboard_result(
        2025, PeriodKind.ANNUAL, client_id, purchaser_id, group_by=group_by
    ).breakdown
    report.export_dashboard_csv(output, rows)
    exported = list(csv.reader(output.open(encoding="utf-8-sig", newline="")))

    assert exported[1][1:3] == expected


def test_dashboard_csv_does_not_mark_inactive_master_names(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    rows = report.dashboard_result(2025, PeriodKind.ANNUAL, client_id=3, purchaser_id=3).breakdown
    output = tmp_path / "inactive.csv"

    report.export_dashboard_csv(output, rows)
    exported = list(csv.reader(output.open(encoding="utf-8-sig", newline="")))

    assert exported[1][1:3] == ["廃止クライアント", "廃止発注主体"]


@pytest.mark.parametrize("destination", ["missing-directory/out.csv", "directory"])
def test_dashboard_csv_unwritable_destination_raises_file_output_error(
    tmp_path: Path,
    seeded_conn: sqlite3.Connection,
    fixed_clock: FixedClock,
    destination: str,
) -> None:
    report = ReportService(seeded_conn, tz=ZoneInfo("Asia/Tokyo"), clock=fixed_clock)
    output = tmp_path / destination
    if destination == "directory":
        output.mkdir()
        (output / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileOutputError) as error_info:
        report.export_dashboard_csv(output, report.dashboard(2025, PeriodKind.ANNUAL))

    assert str(output) in error_info.value.message
    assert list(tmp_path.rglob("*.tmp")) == []
    if destination == "directory":
        assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"
