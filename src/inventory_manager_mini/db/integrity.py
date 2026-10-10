from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from inventory_manager_mini.core.models import (
    MAX_AGGREGATE_VALUE,
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    Reason,
)

AGGREGATE_COLUMNS = (
    "inbound_quantity",
    "outbound_quantity",
    "disposed_quantity",
    "expenditure",
    "disposal_amount",
)


@dataclass(frozen=True, slots=True)
class FifoReplay:
    allocations: dict[int, tuple[tuple[int, int], ...]]
    cost_amounts: dict[int, int | None]
    unit_prices: dict[int, int | None]
    remaining_by_lot: dict[int, int]
    violations: tuple[str, ...]


@dataclass(slots=True)
class _ReplayLot:
    lot_id: int
    item_id: int
    moved_at: str
    original_quantity: int
    remaining_quantity: int
    unit_price: int | None
    active: bool = True


@dataclass(frozen=True, slots=True)
class _ReplayMovement:
    item_id: int
    reason: Reason
    delta: int
    unit_price: int | None
    reversal_of: int | None
    allocations: tuple[tuple[int, int], ...]
    cost_amount: int | None


def _schema_version(conn: sqlite3.Connection, version: int | None) -> int:
    if version is not None:
        return version
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _compute_legacy_aggregates(conn: sqlite3.Connection) -> dict[str, int]:
    totals: dict[str, int] = dict.fromkeys(AGGREGATE_COLUMNS, 0)
    rows = conn.execute("SELECT reason, delta, unit_price FROM stock_movements").fetchall()
    for reason_value, delta_value, price_value in rows:
        if type(delta_value) is not int or (
            price_value is not None and type(price_value) is not int
        ):
            raise ValueError("履歴の数量または単価が整数ではありません")
        reason = Reason(str(reason_value))
        if reason is Reason.IN:
            totals["inbound_quantity"] += delta_value
        elif reason is Reason.OUT:
            quantity = -delta_value
            totals["outbound_quantity"] += quantity
            if price_value is not None:
                totals["expenditure"] += quantity * price_value
        elif reason is Reason.DISPOSE:
            quantity = -delta_value
            totals["disposed_quantity"] += quantity
            if price_value is not None:
                totals["disposal_amount"] += quantity * price_value
    return totals


def _compute_replay_aggregates(conn: sqlite3.Connection, replay: FifoReplay) -> dict[str, int]:
    totals: dict[str, int] = dict.fromkeys(AGGREGATE_COLUMNS, 0)
    movements: dict[int, tuple[Reason, int]] = {}
    for movement_id, reason_value, delta in conn.execute(
        "SELECT id, reason, delta FROM stock_movements"
    ):
        movements[int(movement_id)] = (Reason(str(reason_value)), int(delta))

    lot_prices = replay.unit_prices
    for movement_id, (reason, delta) in movements.items():
        if reason is Reason.IN:
            totals["inbound_quantity"] += delta
        if reason not in {Reason.OUT, Reason.DISPOSE}:
            continue
        quantity_total = 0
        cost_total = 0
        for lot_id, allocation_quantity in replay.allocations.get(movement_id, ()):
            quantity_total += allocation_quantity
            price = lot_prices.get(lot_id)
            if price is not None:
                cost_total += allocation_quantity * price
        if reason is Reason.OUT:
            totals["outbound_quantity"] += quantity_total
            totals["expenditure"] += cost_total
        else:
            totals["disposed_quantity"] += quantity_total
            totals["disposal_amount"] += cost_total
    return totals


def compute_aggregates(conn: sqlite3.Connection, *, version: int | None = None) -> dict[str, int]:
    if _schema_version(conn, version) < 4:
        return _compute_legacy_aggregates(conn)

    totals: dict[str, int] = dict.fromkeys(AGGREGATE_COLUMNS, 0)
    for reason_value, delta in conn.execute("SELECT reason, delta FROM stock_movements"):
        if type(delta) is not int:
            raise ValueError("履歴の数量が整数ではありません")
        reason = Reason(str(reason_value))
        if reason is Reason.IN:
            totals["inbound_quantity"] += delta

    rows = conn.execute(
        "SELECT m.reason, a.quantity, lot.unit_price "
        "FROM stock_allocations AS a "
        "JOIN stock_movements AS m ON m.id = a.movement_id "
        "JOIN stock_movements AS lot ON lot.id = a.lot_id "
        "WHERE m.reason IN ('out', 'dispose')"
    )
    for reason_value, quantity, unit_price in rows:
        if type(quantity) is not int or (unit_price is not None and type(unit_price) is not int):
            raise ValueError("引当数量またはロット単価が整数ではありません")
        reason = Reason(str(reason_value))
        if reason is Reason.OUT:
            totals["outbound_quantity"] += quantity
            if unit_price is not None:
                totals["expenditure"] += quantity * unit_price
        elif reason is Reason.DISPOSE:
            totals["disposed_quantity"] += quantity
            if unit_price is not None:
                totals["disposal_amount"] += quantity * unit_price
    return totals


def replay_allocations(conn: sqlite3.Connection) -> FifoReplay:
    rows = conn.execute(
        "SELECT id, item_id, reason, delta, unit_price, reversal_of, moved_at "
        "FROM stock_movements ORDER BY moved_at, id"
    ).fetchall()
    violations: list[str] = []
    lots_by_item: dict[int, list[_ReplayLot]] = {}
    lots_by_id: dict[int, _ReplayLot] = {}
    movements: dict[int, _ReplayMovement] = {}
    allocations: dict[int, tuple[tuple[int, int], ...]] = {}
    costs: dict[int, int | None] = {}
    unit_prices: dict[int, int | None] = {}

    for (
        movement_id_value,
        item_id_value,
        reason_value,
        delta_value,
        price_value,
        reversal_value,
        moved_at_value,
    ) in rows:
        movement_id = int(movement_id_value)
        item_id = int(item_id_value)
        moved_at = str(moved_at_value)
        try:
            reason = Reason(str(reason_value))
        except ValueError:
            violations.append(f"履歴 ID {movement_id} の種別 {reason_value!r} は不正です")
            continue
        if type(delta_value) is not int:
            violations.append(f"履歴 ID {movement_id} の数量が整数ではありません")
            continue
        delta = delta_value
        reversal_of = None if reversal_value is None else int(reversal_value)
        price = price_value if type(price_value) is int else None
        expected_allocations: list[tuple[int, int]] = []
        cost_amount: int | None = None
        expected_unit_price: int | None = None

        if reversal_of is not None:
            source = movements.get(reversal_of)
            if source is None or source.reversal_of is not None or source.item_id != item_id:
                violations.append(f"履歴 ID {movement_id} の取り消し元 ID {reversal_of} が不正です")
                costs[movement_id] = 0 if reason in {Reason.OUT, Reason.DISPOSE} else None
                unit_prices[movement_id] = None
                movements[movement_id] = _ReplayMovement(
                    item_id, reason, delta, None, reversal_of, (), costs[movement_id]
                )
                continue

            if source.delta > 0:
                lot = lots_by_id.get(reversal_of)
                if lot is None or not lot.active:
                    violations.append(f"履歴 ID {movement_id} の取り消し対象ロットがありません")
                elif lot.remaining_quantity != lot.original_quantity:
                    violations.append(
                        f"履歴 ID {reversal_of} のロットは消費済みのため取り消せません "
                        f"(残数 {lot.remaining_quantity} / 元数量 {lot.original_quantity})"
                    )
                else:
                    lot.active = False
                    lot.remaining_quantity = 0
                expected_unit_price = source.unit_price
            elif source.delta < 0:
                for lot_id, quantity in source.allocations:
                    lot = lots_by_id.get(lot_id)
                    restored_quantity = -quantity
                    if lot is None or not lot.active:
                        violations.append(
                            f"履歴 ID {movement_id} の引当先ロット ID {lot_id} がありません"
                        )
                        continue
                    lot.remaining_quantity += quantity
                    if not 0 <= lot.remaining_quantity <= lot.original_quantity:
                        violations.append(
                            f"ロット ID {lot_id} の取り消し後残数が不正です: "
                            f"{lot.remaining_quantity}"
                        )
                    expected_allocations.append((lot_id, restored_quantity))
                expected_unit_price = None
                if reason in {Reason.OUT, Reason.DISPOSE}:
                    cost_amount = None if source.cost_amount is None else -source.cost_amount
            else:
                expected_unit_price = None
        elif delta > 0 and reason in {Reason.IN, Reason.ADJUST}:
            lot = _ReplayLot(movement_id, item_id, moved_at, delta, delta, price)
            lots_by_item.setdefault(item_id, []).append(lot)
            lots_by_id[movement_id] = lot
            expected_unit_price = price
        elif delta < 0:
            remaining = -delta
            cost_total = 0
            for lot in sorted(
                lots_by_item.get(item_id, ()),
                key=lambda candidate: (candidate.moved_at, candidate.lot_id),
            ):
                if not lot.active or lot.remaining_quantity <= 0:
                    continue
                take = min(remaining, lot.remaining_quantity)
                lot.remaining_quantity -= take
                expected_allocations.append((lot.lot_id, take))
                if lot.unit_price is not None:
                    cost_total += take * lot.unit_price
                remaining -= take
                if remaining == 0:
                    break
            if remaining:
                violations.append(
                    f"履歴 ID {movement_id} の FIFO 引当が不足しています: 不足数量 {remaining}"
                )
            expected_unit_price = None
            if reason in {Reason.OUT, Reason.DISPOSE}:
                cost_amount = cost_total
        else:
            expected_unit_price = None

        canonical_allocations = tuple(sorted(expected_allocations))
        if canonical_allocations:
            allocations[movement_id] = canonical_allocations
        costs[movement_id] = cost_amount
        unit_prices[movement_id] = expected_unit_price
        movements[movement_id] = _ReplayMovement(
            item_id,
            reason,
            delta,
            expected_unit_price,
            reversal_of,
            canonical_allocations,
            cost_amount,
        )

    remaining_by_lot = {
        lot_id: lot.remaining_quantity if lot.active else 0 for lot_id, lot in lots_by_id.items()
    }
    lot_totals: dict[int, int] = {}
    for lot in lots_by_id.values():
        if lot.active:
            lot_totals[lot.item_id] = lot_totals.get(lot.item_id, 0) + lot.remaining_quantity
    for item_id_value, quantity_value in conn.execute("SELECT id, quantity FROM items"):
        item_id = int(item_id_value)
        if type(quantity_value) is int and lot_totals.get(item_id, 0) != quantity_value:
            violations.append(
                f"品目 {item_id} のロット残数合計が在庫数と一致しません: "
                f"ロット {lot_totals.get(item_id, 0)}、在庫 {quantity_value}"
            )

    return FifoReplay(
        allocations=allocations,
        cost_amounts=costs,
        unit_prices=unit_prices,
        remaining_by_lot=remaining_by_lot,
        violations=tuple(violations),
    )


def find_fifo_migration_violations(conn: sqlite3.Connection) -> list[str]:
    violations = find_limit_violations(conn, version=3)
    if violations:
        return violations
    replay = replay_allocations(conn)
    violations.extend(replay.violations)
    movements = conn.execute("SELECT id, reason FROM stock_movements ORDER BY id").fetchall()
    for movement_id_value, reason_value in movements:
        movement_id = int(movement_id_value)
        reason = Reason(str(reason_value))
        if reason not in {Reason.OUT, Reason.DISPOSE}:
            continue
        cost = replay.cost_amounts.get(movement_id)
        if cost is None or not -MAX_MOVEMENT_AMOUNT <= cost <= MAX_MOVEMENT_AMOUNT:
            violations.append(
                _format_violation(
                    "stock_movements",
                    movement_id,
                    "cost_amount",
                    cost,
                    "-100,000,000〜100,000,000",
                )
            )
    if not replay.violations:
        totals = _compute_replay_aggregates(conn, replay)
        for column, value in totals.items():
            if not 0 <= value <= MAX_AGGREGATE_VALUE:
                violations.append(
                    _format_violation(
                        "stock_movements", "全件", column, value, "0〜1,000,000,000,000"
                    )
                )
    return violations


def _format_violation(table: str, row_id: object, column: str, value: object, limit: str) -> str:
    return f"{table} ID {row_id} の {column}={value!r} は上限 {limit} の範囲外です"


def find_limit_violations(conn: sqlite3.Connection, *, version: int | None = None) -> list[str]:
    schema_version = _schema_version(conn, version)
    fifo_schema = schema_version >= 4
    violations: list[str] = []
    numeric_valid = True
    for row_id, quantity, threshold, reorder_quantity, reference_price in conn.execute(
        "SELECT id, quantity, reorder_threshold, reorder_quantity, reference_price FROM items"
    ):
        fields = (
            ("quantity", quantity, 0, MAX_STOCK_QUANTITY, "0〜1,000,000"),
            ("reorder_threshold", threshold, 0, MAX_STOCK_QUANTITY, "0〜1,000,000"),
            ("reorder_quantity", reorder_quantity, 1, MAX_STOCK_QUANTITY, "1〜1,000,000"),
            ("reference_price", reference_price, 0, MAX_UNIT_PRICE, "0〜10,000,000"),
        )
        for column, value, minimum, maximum, label in fields:
            if value is None and column in {"reorder_quantity", "reference_price"}:
                continue
            if type(value) is not int or not minimum <= value <= maximum:
                numeric_valid = False
                violations.append(_format_violation("items", row_id, column, value, label))

    if fifo_schema:
        movement_rows = conn.execute(
            "SELECT id, reason, delta, unit_price, cost_amount FROM stock_movements"
        )
    else:
        movement_rows = conn.execute(
            "SELECT id, reason, delta, unit_price, NULL FROM stock_movements"
        )
    for row_id, reason_value, delta, price, cost in movement_rows:
        try:
            reason = Reason(str(reason_value))
        except ValueError:
            numeric_valid = False
            violations.append(f"stock_movements ID {row_id} の reason={reason_value!r} は不正です")
            continue
        if type(delta) is not int or not -MAX_STOCK_QUANTITY <= delta <= MAX_STOCK_QUANTITY:
            numeric_valid = False
            violations.append(
                _format_violation(
                    "stock_movements", row_id, "delta", delta, "-1,000,000〜1,000,000"
                )
            )
        if price is not None and (type(price) is not int or not 0 <= price <= MAX_UNIT_PRICE):
            numeric_valid = False
            violations.append(
                _format_violation("stock_movements", row_id, "unit_price", price, "0〜10,000,000")
            )
        if fifo_schema:
            cost_required = reason in {Reason.OUT, Reason.DISPOSE}
            if (cost is None) == cost_required or (
                cost is not None
                and (
                    type(cost) is not int or not -MAX_MOVEMENT_AMOUNT <= cost <= MAX_MOVEMENT_AMOUNT
                )
            ):
                numeric_valid = False
                violations.append(
                    _format_violation(
                        "stock_movements",
                        row_id,
                        "cost_amount",
                        cost,
                        "出庫・廃棄は必須、-100,000,000〜100,000,000",
                    )
                )
        elif (
            type(delta) is int
            and type(price) is int
            and reason in {Reason.OUT, Reason.DISPOSE}
            and abs(delta) * price > MAX_MOVEMENT_AMOUNT
        ):
            numeric_valid = False
            violations.append(
                _format_violation(
                    "stock_movements", row_id, "amount", abs(delta) * price, "0〜100,000,000"
                )
            )

    if fifo_schema:
        for allocation_id, quantity in conn.execute("SELECT id, quantity FROM stock_allocations"):
            if (
                type(quantity) is not int
                or not -MAX_STOCK_QUANTITY <= quantity <= MAX_STOCK_QUANTITY
                or quantity == 0
            ):
                numeric_valid = False
                violations.append(
                    _format_violation(
                        "stock_allocations",
                        allocation_id,
                        "quantity",
                        quantity,
                        "-1,000,000〜1,000,000 (0以外)",
                    )
                )

    if numeric_valid:
        totals = compute_aggregates(conn, version=schema_version)
        for column, value in totals.items():
            if not 0 <= value <= MAX_AGGREGATE_VALUE:
                violations.append(
                    _format_violation(
                        "stock_movements", "全件", column, value, "0〜1,000,000,000,000"
                    )
                )

    item_balances: dict[int, int] = {}
    for item_id, delta in conn.execute("SELECT item_id, delta FROM stock_movements"):
        if type(delta) is int:
            item_balances[int(item_id)] = item_balances.get(int(item_id), 0) + delta
    for item_id, quantity in conn.execute("SELECT id, quantity FROM items"):
        if type(quantity) is int and item_balances.get(int(item_id), 0) != quantity:
            violations.append(
                f"品目 {item_id} の在庫数が履歴合計と一致しません: "
                f"在庫 {quantity}、履歴合計 {item_balances.get(int(item_id), 0)}"
            )
    return violations


def find_fifo_violations(conn: sqlite3.Connection) -> list[str]:
    replay = replay_allocations(conn)
    violations = list(replay.violations)
    actual: dict[int, tuple[tuple[int, int], ...]] = {}
    for movement_id, lot_id, quantity in conn.execute(
        "SELECT movement_id, lot_id, quantity FROM stock_allocations"
    ):
        actual.setdefault(int(movement_id), ())
        actual[int(movement_id)] = (*actual[int(movement_id)], (int(lot_id), int(quantity)))
    actual = {movement_id: tuple(sorted(rows)) for movement_id, rows in actual.items()}
    for movement_id in sorted(set(actual) | set(replay.allocations)):
        if actual.get(movement_id, ()) != replay.allocations.get(movement_id, ()):
            violations.append(
                f"履歴 ID {movement_id} の引当明細が時系列再生結果と一致しません: "
                f"保存値 {actual.get(movement_id, ())!r}、再生値 "
                f"{replay.allocations.get(movement_id, ())!r}"
            )

    for movement_id, reason_value, unit_price, cost_amount in conn.execute(
        "SELECT id, reason, unit_price, cost_amount FROM stock_movements"
    ):
        movement_id = int(movement_id)
        reason = Reason(str(reason_value))
        expected_price = replay.unit_prices.get(movement_id)
        if unit_price != expected_price:
            violations.append(
                f"履歴 ID {movement_id} の unit_price がロット規則と一致しません: "
                f"保存値 {unit_price!r}、期待値 {expected_price!r}"
            )
        expected_cost = replay.cost_amounts.get(movement_id)
        if reason in {Reason.OUT, Reason.DISPOSE} and cost_amount != expected_cost:
            violations.append(
                f"履歴 ID {movement_id} の cost_amount が引当明細と一致しません: "
                f"保存値 {cost_amount!r}、期待値 {expected_cost!r}"
            )
    return violations
