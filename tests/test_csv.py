from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from inventory_manager_mini.core.errors import FileOutputError
from inventory_manager_mini.core.models import GroupBy, ItemFilter, NewItem, PeriodKind
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

    report.export_dashboard_csv(output, 2026, PeriodKind.ANNUAL, group_by=GroupBy.CLIENT)
    rows = list(csv.reader(output.open(encoding="utf-8-sig", newline="")))

    assert rows[0] == [
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
    assert rows[1][1:3] == ["総務", "(すべて)"]
    assert rows[1][3] == "1"


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
