from __future__ import annotations

import csv
import sqlite3
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
    assert issue_row[5] == "100"
    assert issue_row[9] == "設備"
    assert issue_row[11] == "○"


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
        "単価未登録件数",
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
    with pytest.raises(OSError, match="replace failed"):
        report.export_items_csv(output, ItemFilter())

    assert output.read_bytes() == b"existing content"
    assert list(tmp_path.glob(".existing.csv.*.tmp")) == []
