from __future__ import annotations

import sqlite3

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


def compute_aggregates(conn: sqlite3.Connection) -> dict[str, int]:
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


def _format_violation(table: str, row_id: object, column: str, value: object, limit: str) -> str:
    return f"{table} ID {row_id} の {column}={value!r} は上限 {limit} の範囲外です"


def find_limit_violations(conn: sqlite3.Connection) -> list[str]:
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

    for row_id, reason_value, delta, price in conn.execute(
        "SELECT id, reason, delta, unit_price FROM stock_movements"
    ):
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
        if (
            type(delta) is int
            and type(price) is int
            and reason_value in {Reason.OUT.value, Reason.DISPOSE.value}
            and abs(delta) * price > MAX_MOVEMENT_AMOUNT
        ):
            numeric_valid = False
            violations.append(
                _format_violation(
                    "stock_movements",
                    row_id,
                    "amount",
                    abs(delta) * price,
                    "0〜100,000,000",
                )
            )

    if numeric_valid:
        totals = compute_aggregates(conn)
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
