from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path

from inventory_manager_mini.core.errors import FileOutputError, ValidationError
from inventory_manager_mini.core.models import (
    MAX_AGGREGATE_VALUE,
    REASON_LABELS,
    Category,
    DashboardResult,
    DashboardRow,
    GroupAxis,
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

_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_AggregateKey = tuple[int, int | None, int | None, int | None, int | None]


@dataclass(frozen=True, slots=True)
class _RowIdentity:
    client_id: int | None
    client_name: str | None
    purchaser_id: int | None
    purchaser_name: str | None
    category_id: int | None
    category_name: str | None
    item_id: int | None
    item_code: str | None
    item_name: str | None


@dataclass(frozen=True, slots=True)
class _Selection:
    base: _RowIdentity
    group_by: frozenset[GroupAxis]
    category_paths: dict[int, str]


def _category_paths(categories: Sequence[Category]) -> dict[int, str]:
    by_id = {category.id: category for category in categories}
    paths: dict[int, str] = {}
    for category in categories:
        names: list[str] = []
        current: Category | None = category
        while current is not None:
            names.append(current.name)
            current = None if current.parent_id is None else by_id.get(current.parent_id)
        paths[category.id] = " > ".join(reversed(names))
    return paths


def fiscal_year_of(local_day: date, start_month: int) -> int:
    if type(start_month) is not int or not 1 <= start_month <= 12:
        raise ValueError("年度開始月は 1〜12 の整数で指定してください")
    return local_day.year if local_day.month >= start_month else local_day.year - 1


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
    except OSError as error:
        raise FileOutputError(f"CSVファイルを書き込めません: {path} ({error})") from error
    finally:
        if temporary_path is not None:
            with suppress(OSError):
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
        category_id: int | None = None,
        item_id: int | None = None,
        group_by: frozenset[GroupAxis] = frozenset(),
    ) -> list[DashboardRow]:
        return list(
            self.dashboard_result(
                fiscal_year, kind, client_id, purchaser_id, category_id, item_id, group_by
            ).breakdown
        )

    def dashboard_result(
        self,
        fiscal_year: int | None,
        kind: PeriodKind,
        client_id: int | None = None,
        purchaser_id: int | None = None,
        category_id: int | None = None,
        item_id: int | None = None,
        group_by: frozenset[GroupAxis] = frozenset(),
    ) -> DashboardResult:
        if fiscal_year is not None and type(fiscal_year) is not int:
            raise ValueError("年度は整数で指定してください")
        if fiscal_year is None and kind is not PeriodKind.ANNUAL:
            raise ValueError("年度を省略できるのは年次集計だけです")
        if client_id is not None and (type(client_id) is not int or client_id < 1):
            raise ValueError("クライアント ID は正の整数で指定してください")
        if purchaser_id is not None and (type(purchaser_id) is not int or purchaser_id < 1):
            raise ValueError("発注主体 ID は正の整数で指定してください")
        if category_id is not None and (type(category_id) is not int or category_id < 1):
            raise ValueError("カテゴリ ID は正の整数で指定してください")
        if item_id is not None and (type(item_id) is not int or item_id < 1):
            raise ValueError("品目 ID は正の整数で指定してください")
        if type(group_by) is not frozenset or any(type(axis) is not GroupAxis for axis in group_by):
            raise ValueError("内訳軸は GroupAxis の frozenset で指定してください")

        with read_transaction(self.conn):
            start_month = self.settings.get_fiscal_year_start_month()
            if fiscal_year is None:
                years = self._available_fiscal_years(start_month)
                periods = [
                    period
                    for year in years
                    for period in self._periods(year, PeriodKind.ANNUAL, start_month)
                ]
            else:
                periods = self._periods(fiscal_year, kind, start_month)
            category_paths = _category_paths(self.inventory.masters.list_categories())
            if category_id is not None and category_id not in category_paths:
                raise ValidationError("カテゴリが見つかりません")
            item_code: str | None = None
            item_name: str | None = None
            if item_id is not None:
                item_row = self.conn.execute(
                    "SELECT code, name FROM items WHERE id = ?", (item_id,)
                ).fetchone()
                if item_row is None:
                    raise ValidationError("品目が見つかりません")
                item_code, item_name = str(item_row[0]), str(item_row[1])
            selection = _Selection(
                _RowIdentity(
                    client_id,
                    self._filtered_master_name("clients", client_id),
                    purchaser_id,
                    self._filtered_master_name("purchasers", purchaser_id),
                    category_id,
                    None if category_id is None else category_paths[category_id],
                    item_id,
                    item_code,
                    item_name,
                ),
                group_by,
                category_paths,
            )
            return self._aggregate(periods, selection)

    def _available_fiscal_years(self, start_month: int) -> list[int]:
        row = self.conn.execute(
            "SELECT MIN(moved_at), MAX(moved_at) FROM stock_movements"
        ).fetchone()
        current_year = fiscal_year_of(local_today(self.clock(), self.tz), start_month)
        if row is None or row[0] is None or row[1] is None:
            return [current_year]
        first_year = fiscal_year_of(local_date(str(row[0]), self.tz), start_month)
        last_year = fiscal_year_of(local_date(str(row[1]), self.tz), start_month)
        return sorted({*range(first_year, last_year + 1), current_year})

    def _filtered_master_name(self, table: str, entity_id: int | None) -> str | None:
        if entity_id is None:
            return None
        if table == "clients":
            row = self.conn.execute(
                "SELECT name FROM clients WHERE id = ?", (entity_id,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT name FROM purchasers WHERE id = ?", (entity_id,)
            ).fetchone()
        if row is None:
            label = "クライアント" if table == "clients" else "発注主体"
            raise ValidationError(f"{label}が見つかりません")
        return str(row[0])

    def _periods(
        self, fiscal_year: int, kind: PeriodKind, start_month: int
    ) -> list[tuple[str, date, date, date]]:
        fiscal_start = date(fiscal_year, start_month, 1)
        if kind is PeriodKind.ANNUAL:
            fiscal_end = date(fiscal_year + 1, start_month, 1)
            return [(f"{fiscal_year}年度", fiscal_start, fiscal_start, fiscal_end)]

        periods: list[tuple[str, date, date, date]] = []
        for offset in range(12):
            absolute_month = start_month - 1 + offset
            year = fiscal_year + absolute_month // 12
            month = absolute_month % 12 + 1
            period_start = date(year, month, 1)
            period_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            periods.append((f"{year}年{month}月", period_start, period_start, period_end))
        return periods

    def _aggregate(
        self,
        periods: list[tuple[str, date, date, date]],
        selection: _Selection,
    ) -> DashboardResult:
        ranges = [local_range_to_utc(start, end, self.tz) for _, _, start, end in periods]
        base = selection.base
        group_by = selection.group_by
        parameters: dict[str, object] = {
            "range_start": ranges[0][0],
            "range_end": ranges[-1][1],
            "client_id": base.client_id,
            "purchaser_id": base.purchaser_id,
            "category_id": base.category_id,
            "item_id": base.item_id,
        }
        rows = self.conn.execute(
            """WITH RECURSIVE descendants(id) AS (
                SELECT id FROM categories WHERE id = :category_id
                UNION ALL
                SELECT c.id FROM categories c JOIN descendants d ON c.parent_id = d.id
            )
            SELECT COALESCE(o.moved_at, m.moved_at) AS event_at,
                m.client_id, c.name, COALESCE(lot.purchaser_id, m.purchaser_id), p.name,
                i.category_id, i.id, i.code, i.name,
                m.reason, m.delta, a.quantity, lot.unit_price
            FROM stock_movements m
            JOIN items i ON i.id = m.item_id
            LEFT JOIN stock_movements o ON o.id = m.reversal_of
            LEFT JOIN stock_allocations a ON a.movement_id = m.id
            LEFT JOIN stock_movements lot ON lot.id = a.lot_id
            JOIN clients c ON c.id = m.client_id
            JOIN purchasers p ON p.id = COALESCE(lot.purchaser_id, m.purchaser_id)
            WHERE COALESCE(o.moved_at, m.moved_at) >= :range_start
                AND COALESCE(o.moved_at, m.moved_at) < :range_end
                AND (:client_id IS NULL OR m.client_id = :client_id)
                AND (
                    :purchaser_id IS NULL
                    OR COALESCE(lot.purchaser_id, m.purchaser_id) = :purchaser_id
                )
                AND (:item_id IS NULL OR m.item_id = :item_id)
                AND (:category_id IS NULL OR i.category_id IN (SELECT id FROM descendants))
            ORDER BY event_at, m.id""",
            parameters,
        ).fetchall()

        by_client = GroupAxis.CLIENT in group_by
        by_purchaser = GroupAxis.PURCHASER in group_by
        by_category = GroupAxis.CATEGORY in group_by
        by_item = GroupAxis.ITEM in group_by
        category_paths = selection.category_paths
        totals: dict[int, list[int]] = {}
        aggregates: dict[_AggregateKey, list[int]] = {}
        identities: dict[_AggregateKey, _RowIdentity] = {}
        for row in rows:
            event_at = str(row[0])
            period_index = next(
                index for index, (start, end) in enumerate(ranges) if start <= event_at < end
            )
            total_values = totals.setdefault(period_index, [0, 0, 0, 0, 0, 0])
            key: _AggregateKey = (
                period_index,
                int(row[1]) if by_client else base.client_id,
                int(row[3]) if by_purchaser else base.purchaser_id,
                int(row[5]) if by_category else base.category_id,
                int(row[6]) if by_item else base.item_id,
            )
            values = aggregates.get(key)
            if values is None:
                values = aggregates[key] = [0, 0, 0, 0, 0, 0]
                identities[key] = _RowIdentity(
                    key[1],
                    str(row[2]) if by_client else base.client_name,
                    key[2],
                    str(row[4]) if by_purchaser else base.purchaser_name,
                    key[3],
                    category_paths[int(row[5])] if by_category else base.category_name,
                    key[4],
                    str(row[7]) if by_item else base.item_code,
                    str(row[8]) if by_item else base.item_name,
                )
            reason = Reason(str(row[9]))
            delta = int(row[10])
            allocation_quantity = None if row[11] is None else int(row[11])
            unit_price = None if row[12] is None else int(row[12])
            if reason is Reason.IN:
                total_values[0] += delta
                values[0] += delta
            elif reason is Reason.OUT:
                if allocation_quantity is not None:
                    total_values[1] += allocation_quantity
                    values[1] += allocation_quantity
                    if unit_price is None:
                        total_values[3] += allocation_quantity
                        values[3] += allocation_quantity
                    else:
                        total_values[2] += allocation_quantity * unit_price
                        values[2] += allocation_quantity * unit_price
            elif reason is Reason.DISPOSE and allocation_quantity is not None:
                total_values[4] += allocation_quantity
                values[4] += allocation_quantity
                if unit_price is not None:
                    total_values[5] += allocation_quantity * unit_price
                    values[5] += allocation_quantity * unit_price

        for period_index in range(len(periods)):
            totals.setdefault(period_index, [0, 0, 0, 0, 0, 0])
            if not group_by:
                key = (
                    period_index,
                    base.client_id,
                    base.purchaser_id,
                    base.category_id,
                    base.item_id,
                )
                aggregates.setdefault(key, [0, 0, 0, 0, 0, 0])
                identities.setdefault(key, base)

        def make_row(period_index: int, identity: _RowIdentity, metrics: list[int]) -> DashboardRow:
            if any(value < 0 or value > MAX_AGGREGATE_VALUE for value in metrics[:3] + metrics[4:]):
                raise ValidationError("集計値が 0〜1,000,000,000,000 の範囲を超えます")
            label, period_start, _, _ = periods[period_index]
            return DashboardRow(
                period_label=label,
                period_start=period_start,
                client_id=identity.client_id,
                client_name=identity.client_name,
                purchaser_id=identity.purchaser_id,
                purchaser_name=identity.purchaser_name,
                category_id=identity.category_id,
                category_name=identity.category_name,
                item_id=identity.item_id,
                item_code=identity.item_code,
                item_name=identity.item_name,
                inbound_quantity=metrics[0],
                outbound_quantity=metrics[1],
                expenditure=metrics[2],
                unpriced_issue_quantity=metrics[3],
                disposed_quantity=metrics[4],
                disposal_amount=metrics[5],
            )

        total_rows = tuple(
            make_row(period_index, base, metrics)
            for period_index, metrics in sorted(totals.items())
        )

        def sort_key(key: _AggregateKey) -> tuple[int, int, int, str, int, str, int]:
            identity = identities[key]
            return (
                key[0],
                key[1] or 0,
                key[2] or 0,
                identity.category_name or "",
                key[3] or 0,
                identity.item_code or "",
                key[4] or 0,
            )

        breakdown_rows = tuple(
            make_row(key[0], identities[key], aggregates[key])
            for key in sorted(aggregates, key=sort_key)
        )
        return DashboardResult(total_rows, breakdown_rows)

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
            "原価金額",
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
                REASON_LABELS[row.reason],
                row.delta,
                row.unit_price,
                row.cost_amount if row.reason in (Reason.OUT, Reason.DISPOSE) else None,
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
        rows: Sequence[DashboardRow],
    ) -> None:
        header = (
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
        )
        csv_rows = [
            (
                row.period_label,
                row.client_name if row.client_id is not None else "(すべて)",
                row.purchaser_name if row.purchaser_id is not None else "(すべて)",
                row.category_name if row.category_id is not None else "(すべて)",
                row.item_code if row.item_id is not None else "(すべて)",
                row.item_name if row.item_id is not None else "(すべて)",
                row.inbound_quantity,
                row.outbound_quantity,
                row.expenditure,
                row.unpriced_issue_quantity,
                row.disposed_quantity,
                row.disposal_amount,
            )
            for row in rows
        ]
        _write_csv(path, header, csv_rows)
