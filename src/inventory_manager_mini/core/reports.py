from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import date, datetime, tzinfo
from pathlib import Path

from inventory_manager_mini.core.models import (
    DashboardRow,
    GroupBy,
    ItemFilter,
    PeriodKind,
    Reason,
)
from inventory_manager_mini.core.services import InventoryService, SettingsService
from inventory_manager_mini.core.timeutil import (
    format_local,
    local_date,
    local_range_to_utc,
    local_today,
    utc_now,
)
from inventory_manager_mini.db.connection import read_transaction

_REASON_LABELS = {
    Reason.IN: "入庫",
    Reason.OUT: "出庫",
    Reason.RETURN: "返品",
    Reason.DISPOSE: "廃棄",
    Reason.ADJUST: "棚卸",
}
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_AggregateKey = tuple[int, int | None, int | None]


def fiscal_year_of(local_day: date, start_month: int) -> int:
    if type(start_month) is not int or not 1 <= start_month <= 12:
        raise ValueError("年度開始月は 1〜12 の整数で指定してください")
    return local_day.year - (local_day.month < start_month)


def escape_csv_cell(value: str) -> str:
    return "'" + value if value.startswith(_CSV_FORMULA_PREFIXES) else value


def _as_csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str):
        return escape_csv_cell(value)
    return value


def _write_csv(path: Path, header: Sequence[object], rows: Sequence[Sequence[object]]) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            writer = csv.writer(temporary, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
            writer.writerow(header)
            writer.writerows([_as_csv_value(value) for value in row] for row in rows)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_path)


class ReportService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        tz: tzinfo | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.conn = conn
        self.tz = tz
        self.clock = clock
        self.inventory = InventoryService(conn)
        self.settings = SettingsService(conn)

    def current_fiscal_year(self) -> int:
        start_month = self.settings.get_fiscal_year_start_month()
        return fiscal_year_of(local_today(self.clock(), self.tz), start_month)

    def available_fiscal_years(self) -> list[int]:
        start_month = self.settings.get_fiscal_year_start_month()
        row = self.conn.execute(
            "SELECT MIN(moved_at), MAX(moved_at) FROM stock_movements"
        ).fetchone()
        current_year = fiscal_year_of(local_today(self.clock(), self.tz), start_month)
        if row is None or row[0] is None or row[1] is None:
            return [current_year]
        first_year = fiscal_year_of(local_date(str(row[0]), self.tz), start_month)
        last_year = fiscal_year_of(local_date(str(row[1]), self.tz), start_month)
        return sorted({*range(first_year, last_year + 1), current_year})

    def dashboard(
        self,
        fiscal_year: int,
        kind: PeriodKind,
        client_id: int | None = None,
        purchaser_id: int | None = None,
        group_by: GroupBy = GroupBy.NONE,
    ) -> list[DashboardRow]:
        if type(fiscal_year) is not int:
            raise ValueError("年度は整数で指定してください")
        if client_id is not None and (type(client_id) is not int or client_id < 1):
            raise ValueError("クライアント ID は正の整数で指定してください")
        if purchaser_id is not None and (type(purchaser_id) is not int or purchaser_id < 1):
            raise ValueError("発注主体 ID は正の整数で指定してください")

        with read_transaction(self.conn):
            start_month = self.settings.get_fiscal_year_start_month()
            periods = self._periods(fiscal_year, kind, start_month)
            result = self._aggregate(periods, client_id, purchaser_id, group_by)
        return result

    def _periods(
        self, fiscal_year: int, kind: PeriodKind, start_month: int
    ) -> list[tuple[str, date, date, date]]:
        fiscal_start_year = fiscal_year if start_month == 1 else fiscal_year - 1
        fiscal_start = date(fiscal_start_year, start_month, 1)
        if kind is PeriodKind.ANNUAL:
            fiscal_end = date(fiscal_start_year + 1, start_month, 1)
            return [(f"{fiscal_year}年度", fiscal_start, fiscal_start, fiscal_end)]

        periods: list[tuple[str, date, date, date]] = []
        for offset in range(12):
            absolute_month = start_month - 1 + offset
            year = fiscal_start_year + absolute_month // 12
            month = absolute_month % 12 + 1
            period_start = date(year, month, 1)
            period_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            periods.append((f"{year}年{month}月", period_start, period_start, period_end))
        return periods

    def _aggregate(
        self,
        periods: list[tuple[str, date, date, date]],
        client_id: int | None,
        purchaser_id: int | None,
        group_by: GroupBy,
    ) -> list[DashboardRow]:
        boundaries: list[str] = []
        case_parts: list[str] = []
        parameters: dict[str, object] = {"client_id": client_id, "purchaser_id": purchaser_id}
        for index, (_, _, start, end) in enumerate(periods):
            start_utc, end_utc = local_range_to_utc(start, end, self.tz)
            boundaries.extend((start_utc, end_utc))
            parameters[f"period_start_{index}"] = start_utc
            parameters[f"period_end_{index}"] = end_utc
            parameters[f"period_index_{index}"] = index
            case_parts.append(
                f"WHEN event_at >= :period_start_{index} AND event_at < :period_end_{index} "
                f"THEN :period_index_{index}"
            )
        first_start = boundaries[0]
        final_end = boundaries[-1]
        parameters["range_start"] = first_start
        parameters["range_end"] = final_end
        period_case = "CASE " + " ".join(case_parts) + " ELSE NULL END"
        rows = self.conn.execute(
            f"""WITH movements AS (
                SELECT m.*, COALESCE(o.moved_at, m.moved_at) AS event_at
                FROM stock_movements m
                LEFT JOIN stock_movements o ON o.id = m.reversal_of
            )
            SELECT {period_case} AS period_index,
                m.client_id, c.name, m.purchaser_id, p.name,
                COALESCE(SUM(CASE WHEN m.reason = 'in' THEN m.delta ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN m.reason = 'out' THEN -m.delta ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN m.reason = 'out' AND m.unit_price IS NOT NULL
                    THEN -m.delta * m.unit_price ELSE 0 END), 0),
                COUNT(CASE WHEN m.reason = 'out' AND m.unit_price IS NULL
                    AND m.reversal_of IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM stock_movements reversal WHERE reversal.reversal_of = m.id
                    ) THEN 1 END),
                COALESCE(SUM(CASE WHEN m.reason = 'dispose' THEN -m.delta ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN m.reason = 'dispose' AND m.unit_price IS NOT NULL
                    THEN -m.delta * m.unit_price ELSE 0 END), 0)
            FROM movements m
            JOIN clients c ON c.id = m.client_id
            JOIN purchasers p ON p.id = m.purchaser_id
            WHERE m.event_at >= :range_start AND m.event_at < :range_end
                AND (:client_id IS NULL OR m.client_id = :client_id)
                AND (:purchaser_id IS NULL OR m.purchaser_id = :purchaser_id)
            GROUP BY period_index, m.client_id, c.name, m.purchaser_id, p.name
            ORDER BY period_index, m.client_id, m.purchaser_id""",
            parameters,
        ).fetchall()

        aggregates: dict[_AggregateKey, list[int]] = {}
        names: dict[_AggregateKey, tuple[str | None, str | None]] = {}
        for row in rows:
            period_index = int(row[0])
            row_client_id = int(row[1])
            row_client_name = str(row[2])
            row_purchaser_id = int(row[3])
            row_purchaser_name = str(row[4])
            if group_by is GroupBy.NONE:
                key = (period_index, None, None)
            elif group_by is GroupBy.CLIENT:
                key = (period_index, row_client_id, None)
            elif group_by is GroupBy.PURCHASER:
                key = (period_index, None, row_purchaser_id)
            else:
                key = (period_index, row_client_id, row_purchaser_id)
            aggregates.setdefault(key, [0, 0, 0, 0, 0, 0])
            names.setdefault(
                key,
                (
                    row_client_name if key[1] is not None else None,
                    row_purchaser_name if key[2] is not None else None,
                ),
            )
            values = aggregates[key]
            for metric_index in range(6):
                values[metric_index] += int(row[5 + metric_index])

        if group_by is GroupBy.NONE:
            for period_index in range(len(periods)):
                key = (period_index, None, None)
                aggregates.setdefault(key, [0, 0, 0, 0, 0, 0])
                names.setdefault(key, (None, None))

        output: list[DashboardRow] = []
        for (period_index, row_client_id, row_purchaser_id), metrics in sorted(
            aggregates.items(), key=lambda entry: (entry[0][0], entry[0][1] or 0, entry[0][2] or 0)
        ):
            label, period_start, _, _ = periods[period_index]
            client_name, purchaser_name = names[(period_index, row_client_id, row_purchaser_id)]
            output.append(
                DashboardRow(
                    period_label=label,
                    period_start=period_start,
                    client_id=row_client_id,
                    client_name=client_name,
                    purchaser_id=row_purchaser_id,
                    purchaser_name=purchaser_name,
                    inbound_quantity=metrics[0],
                    outbound_quantity=metrics[1],
                    expenditure=metrics[2],
                    unpriced_issue_count=metrics[3],
                    disposed_quantity=metrics[4],
                    disposal_amount=metrics[5],
                )
            )
        return output

    def export_items_csv(self, path: Path, filter: ItemFilter) -> None:
        items = self.inventory.list_items(filter)
        header = (
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
        )
        rows = [
            (
                item.code,
                item.name,
                item.manufacturer_part_number,
                item.client_name,
                item.purchaser_name,
                item.category_path,
                item.location_name,
                item.quantity,
                item.unit,
                item.reorder_threshold,
                item.reorder_quantity,
                "○" if item.is_low_stock else "",
                item.reference_price,
                item.supplier,
                item.purchase_url,
                local_date(item.purchase_info.last_purchased_at, self.tz).isoformat()
                if item.purchase_info.last_purchased_at is not None
                else None,
                item.purchase_info.lot_quantity,
                "有効" if item.is_active else "廃止",
                item.application,
                item.note,
            )
            for item in items
        ]
        _write_csv(path, header, rows)

    def export_history_csv(self, path: Path, item_id: int | None = None) -> None:
        history = (
            self.inventory.list_all_history()
            if item_id is None
            else self.inventory.list_history(item_id)
        )
        header = (
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
        )
        rows = [
            (
                format_local(row.moved_at, self.tz),
                row.code,
                row.item_name,
                _REASON_LABELS[row.reason],
                row.delta,
                row.unit_price,
                row.client_name,
                row.purchaser_name,
                row.staff_name,
                row.used_for,
                row.reversal_of,
                "○" if row.is_reversed else "",
                row.note,
            )
            for row in history
        ]
        _write_csv(path, header, rows)

    def export_dashboard_csv(
        self,
        path: Path,
        fiscal_year: int,
        kind: PeriodKind,
        client_id: int | None = None,
        purchaser_id: int | None = None,
        group_by: GroupBy = GroupBy.NONE,
    ) -> None:
        dashboard = self.dashboard(fiscal_year, kind, client_id, purchaser_id, group_by)
        header = (
            "年度/月",
            "クライアント",
            "発注主体",
            "入庫数",
            "出庫数",
            "支出額",
            "単価未登録件数",
            "廃棄数",
            "廃棄額",
        )
        rows = [
            (
                row.period_label,
                row.client_name if row.client_id is not None else "(すべて)",
                row.purchaser_name if row.purchaser_id is not None else "(すべて)",
                row.inbound_quantity,
                row.outbound_quantity,
                row.expenditure,
                row.unpriced_issue_count,
                row.disposed_quantity,
                row.disposal_amount,
            )
            for row in dashboard
        ]
        _write_csv(path, header, rows)
