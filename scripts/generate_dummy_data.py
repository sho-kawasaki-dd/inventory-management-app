from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inventory_manager_mini.core.services import BackupService
from inventory_manager_mini.core.timeutil import local_timestamp_for_filename, utc_now
from inventory_manager_mini.db.connection import transaction
from inventory_manager_mini.db.integrity import compute_aggregates
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, open_database


def _remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, type=Path, help="生成先データベースのパス")
    parser.add_argument("--items", type=int, default=5000, help="品目数 (既定: 5000)")
    parser.add_argument("--movements", type=int, default=100000, help="履歴件数 (既定: 100000)")
    parser.add_argument("--years", type=int, default=3, help="履歴を分布させる年数 (既定: 3)")
    parser.add_argument("--seed", type=int, default=0, help="乱数シード (既定: 0)")
    parser.add_argument(
        "--force", action="store_true", help="既存 DB と SQLite 付随ファイルを削除して作り直す"
    )


def _validate_arguments(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.items < 1:
        parser.error("--items は 1 以上で指定してください")
    if args.movements < 6:
        parser.error("全 reason と取り消し行を含めるため、--movements は 6 以上で指定してください")
    if args.years < 1:
        parser.error("--years は 1 以上で指定してください")


def _insert_masters(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    conn.executemany(
        "INSERT INTO clients (id, name, is_active) VALUES (?, ?, ?)",
        [(index, f"クライアント{index:02d}", int(index != 8)) for index in range(1, 9)],
    )
    conn.executemany(
        "INSERT INTO purchasers (id, name, is_active) VALUES (?, ?, ?)",
        [(index, f"発注主体{index:02d}", int(index != 8)) for index in range(1, 9)],
    )
    conn.executemany(
        "INSERT INTO staff (id, name, is_active) VALUES (?, ?, ?)",
        [(index, f"担当者{index:02d}", int(index != 8)) for index in range(1, 9)],
    )

    categories: list[tuple[int, str]] = []
    category_rows: list[tuple[int, int | None, str, str, int]] = []
    category_id = 1
    for parent_number in range(1, 6):
        parent_id = category_id
        category_rows.append((parent_id, None, f"分類{parent_number}", f"P{parent_number:03d}", 1))
        category_id += 1
        for child_number in range(1, 5):
            child_id = category_id
            prefix = f"C{parent_number}{child_number:03d}"
            category_rows.append(
                (child_id, parent_id, f"分類{parent_number}-{child_number}", prefix, 1)
            )
            categories.append((child_id, prefix))
            category_id += 1
    conn.executemany(
        "INSERT INTO categories (id, parent_id, name, code_prefix, next_seq) "
        "VALUES (?, ?, ?, ?, ?)",
        category_rows,
    )
    conn.executemany(
        "INSERT INTO locations (id, name) VALUES (?, ?)",
        [(index, f"保管場所{index:02d}") for index in range(1, 9)],
    )
    return categories


def _insert_items(
    conn: sqlite3.Connection,
    item_count: int,
    categories: list[tuple[int, str]],
    rng: random.Random,
    now: datetime,
) -> None:
    category_sequences = {category_id: 0 for category_id, _ in categories}
    category_prefixes = dict(categories)
    rows: list[tuple[object, ...]] = []
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for item_id in range(1, item_count + 1):
        category_id, _ = categories[(item_id - 1) % len(categories)]
        category_sequences[category_id] += 1
        sequence = category_sequences[category_id]
        code = f"{category_prefixes[category_id]}-{sequence:04d}"
        reference_price = None if item_id % 10 == 0 else rng.randint(100, 50000)
        purchase_url = f"https://example.invalid/items/{item_id}" if item_id % 5 == 0 else None
        rows.append(
            (
                item_id,
                (item_id - 1) % 8 + 1,
                (item_id * 3 - 1) % 8 + 1,
                code,
                f"備品{item_id:06d}",
                category_id,
                rng.randint(1, 8),
                0,
                rng.randint(0, 10),
                rng.randint(1, 30),
                purchase_url,
                f"仕入先{rng.randint(1, 20):02d}",
                f"MODEL-{item_id:06d}",
                f"用途{item_id % 12:02d}",
                reference_price,
                None,
                int(item_id % 25 != 0),
                timestamp,
                timestamp,
            )
        )
    conn.executemany(
        "INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, location_id, "
        "quantity, reorder_threshold, reorder_quantity, purchase_url, supplier, "
        "manufacturer_part_number, application, reference_price, note, is_active, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "UPDATE categories SET next_seq = ? WHERE id = ?",
        [(sequence + 1, category_id) for category_id, sequence in category_sequences.items()],
    )


def _generate_movements(
    conn: sqlite3.Connection,
    item_count: int,
    movement_count: int,
    years: int,
    rng: random.Random,
    now: datetime,
    ensure_positive_active_quantities: bool = False,
) -> None:
    if ensure_positive_active_quantities and movement_count < item_count + 6:
        raise ValueError("有効品目の数量を正にするには、品目数 + 6 件以上の履歴が必要です")

    balances = [0] * (item_count + 1)
    totals = [0] * (item_count + 1)
    movement_rows: list[tuple[object, ...]] = []
    original_rows: list[tuple[int, int, int, int, int, str, int, int | None, str | None]] = []
    lots_by_item: dict[int, list[int]] = {}
    lot_remaining: dict[int, int] = {}
    lot_prices: dict[int, int | None] = {}
    allocations_by_movement: dict[int, list[tuple[int, int]]] = {}
    movement_costs: dict[int, int | None] = {}

    def append_movement(
        item_id: int,
        reason: str,
        delta: int,
        reversal_of: int | None = None,
        original: tuple[int, int, int, int, int, str, int, int | None, str | None] | None = None,
    ) -> None:
        movement_id = len(movement_rows) + 1
        client_id = (item_id - 1) % 8 + 1
        purchaser_id = (item_id * 3 - 1) % 8 + 1
        staff_id = (movement_id % 7) + 1
        unit_price = None if item_id % 10 == 0 else rng.randint(100, 50000)
        used_for = f"用途{movement_id % 12:02d}" if reason == "out" else None
        if original is not None:
            _, _, client_id, purchaser_id, _, reason, _, unit_price, used_for = original
        elif delta < 0:
            unit_price = None

        movement_allocations: list[tuple[int, int]] = []
        cost_amount: int | None = None
        if reversal_of is not None and original is not None:
            if original[6] > 0:
                lot_remaining[reversal_of] = 0
            else:
                for lot_id, quantity in allocations_by_movement.get(reversal_of, []):
                    lot_remaining[lot_id] += quantity
                    movement_allocations.append((lot_id, -quantity))
                original_cost = movement_costs.get(reversal_of)
                cost_amount = None if original_cost is None else -original_cost
        elif delta > 0 and reason in {"in", "adjust"}:
            lot_remaining[movement_id] = delta
            lot_prices[movement_id] = unit_price
            lots_by_item.setdefault(item_id, []).append(movement_id)
        elif delta < 0:
            remaining = -delta
            for lot_id in lots_by_item.get(item_id, []):
                available = lot_remaining[lot_id]
                quantity = min(remaining, available)
                if quantity:
                    lot_remaining[lot_id] -= quantity
                    remaining -= quantity
                    movement_allocations.append((lot_id, quantity))
                    lot_price = lot_prices[lot_id]
                    if reason in {"out", "dispose"} and lot_price is not None:
                        cost_amount = (cost_amount or 0) + quantity * lot_price
                if remaining == 0:
                    break
            if remaining:
                raise RuntimeError(f"品目 ID {item_id} の FIFO ロットが不足しています")
        if reason in {"out", "dispose"} and cost_amount is None:
            cost_amount = 0

        movement_rows.append(
            (
                movement_id,
                item_id,
                client_id,
                purchaser_id,
                staff_id,
                reason,
                delta,
                unit_price,
                cost_amount,
                used_for,
                reversal_of,
                "生成データの取り消し" if reversal_of is not None else None,
            )
        )
        if movement_allocations:
            allocations_by_movement[movement_id] = movement_allocations
        movement_costs[movement_id] = cost_amount
        totals[item_id] += delta
        balances[item_id] += delta
        if reversal_of is None:
            original_rows.append(
                (
                    movement_id,
                    item_id,
                    client_id,
                    purchaser_id,
                    staff_id,
                    reason,
                    delta,
                    unit_price,
                    used_for,
                )
            )

    initial_actions = (("in", 10), ("out", -1), ("return", -1), ("dispose", -1), ("adjust", 2))
    for reason, delta in initial_actions:
        append_movement(1, reason, delta)
    reversal_target = original_rows[-1]
    append_movement(
        reversal_target[1],
        reversal_target[5],
        -reversal_target[6],
        reversal_target[0],
        reversal_target,
    )

    regular_movement_count = (
        movement_count - item_count if ensure_positive_active_quantities else movement_count
    )
    reason_options = ("in", "out", "return", "dispose", "adjust")
    while len(movement_rows) < regular_movement_count:
        item_id = rng.randint(1, item_count)
        reason = reason_options[(len(movement_rows) - 6) % len(reason_options)]
        quantity = rng.randint(1, 8)
        if reason == "in":
            delta = quantity
        elif reason == "adjust":
            delta = quantity if rng.random() < 0.65 else -min(quantity, balances[item_id])
            if delta == 0:
                delta = quantity
        elif balances[item_id] >= quantity:
            delta = -quantity
        elif balances[item_id] > 0:
            delta = -balances[item_id]
        else:
            reason = "in"
            delta = quantity

        append_movement(item_id, reason, delta)

    if ensure_positive_active_quantities:
        active_item_ids = [
            row[0] for row in conn.execute("SELECT id FROM items WHERE is_active = 1 ORDER BY id")
        ]
        if not active_item_ids:
            raise ValueError("有効品目がないため、正の在庫を持つ性能 DB を生成できません")
        for item_id in active_item_ids:
            if balances[item_id] == 0:
                append_movement(item_id, "in", 1)
        padding_index = 0
        while len(movement_rows) < movement_count:
            item_id = active_item_ids[padding_index % len(active_item_ids)]
            append_movement(item_id, "in", 1)
            padding_index += 1

    duration_seconds = years * 365.2425 * 24 * 60 * 60
    start = now - timedelta(seconds=duration_seconds)
    movement_total = len(movement_rows)
    rows_with_time = [
        (
            *row,
            (start + timedelta(seconds=duration_seconds * index / max(movement_total - 1, 1)))
            .astimezone(UTC)
            .strftime("%Y-%m-%d %H:%M:%S"),
        )
        for index, row in enumerate(movement_rows)
    ]
    invalid_cost_rows = [
        (row[0], row[5], row[8])
        for row in movement_rows
        if (row[5] in {"out", "dispose"}) != (row[8] is not None)
    ]
    if invalid_cost_rows:
        raise RuntimeError(f"出庫・廃棄原価の生成が不正です: {invalid_cost_rows[:5]}")
    conn.executemany(
        "INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason, "
        "delta, unit_price, cost_amount, used_for, reversal_of, note, moved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows_with_time,
    )
    conn.executemany(
        "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (?, ?, ?)",
        [
            (movement_id, lot_id, quantity)
            for movement_id, movement_allocations in allocations_by_movement.items()
            for lot_id, quantity in movement_allocations
        ],
    )
    conn.executemany(
        "UPDATE items SET quantity = ? WHERE id = ?",
        [(totals[item_id], item_id) for item_id in range(1, item_count + 1)],
    )
    aggregates = compute_aggregates(conn)
    conn.execute(
        "UPDATE total_aggregates SET inbound_quantity = ?, outbound_quantity = ?, "
        "disposed_quantity = ?, expenditure = ?, disposal_amount = ? WHERE id = 1",
        tuple(
            aggregates[column]
            for column in (
                "inbound_quantity",
                "outbound_quantity",
                "disposed_quantity",
                "expenditure",
                "disposal_amount",
            )
        ),
    )


def generate_database(
    args: argparse.Namespace, *, ensure_positive_active_quantities: bool = False
) -> tuple[int, int, float]:
    path = args.db.expanduser().resolve()
    if path.exists():
        if not args.force:
            raise FileExistsError(f"出力先がすでに存在します (--force が必要です): {path}")
        if not path.is_file():
            raise IsADirectoryError(path)
        _remove_database_files(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    rng = random.Random(args.seed)
    now = utc_now()
    conn = open_database(
        path,
        path.parent,
        backup_timestamp=local_timestamp_for_filename,
    )
    try:
        with transaction(conn):
            categories = _insert_masters(conn)
            _insert_items(conn, args.items, categories, rng, now)
            _generate_movements(
                conn,
                args.items,
                args.movements,
                args.years,
                rng,
                now,
                ensure_positive_active_quantities,
            )
        reasons = BackupService().inspect_database(conn, SCHEMA_VERSION)
        if reasons:
            raise RuntimeError("生成データの整合性検査に失敗しました: " + "、".join(reasons))
        elapsed = time.perf_counter() - started
    except BaseException:
        conn.close()
        _remove_database_files(path)
        raise
    conn.close()
    return args.items, args.movements, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="性能検証用の在庫データベースを生成します")
    _add_arguments(parser)
    args = parser.parse_args(argv)
    _validate_arguments(args, parser)
    try:
        items, movements, elapsed = generate_database(args)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
        print(f"生成に失敗しました: {error}", file=sys.stderr)
        return 1
    print(f"生成完了: 品目 {items:,} 件、履歴 {movements:,} 件、所要時間 {elapsed:.2f} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
