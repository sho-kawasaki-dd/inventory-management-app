from __future__ import annotations

import sqlite3

import pytest

from inventory_manager_mini.core.models import (
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    NewItem,
)
from inventory_manager_mini.core.services import InventoryService
from inventory_manager_mini.db import integrity
from inventory_manager_mini.db.integrity import compute_aggregates, find_limit_violations
from tests.conftest import unchecked_constraints

ZERO_TOTALS = {
    "inbound_quantity": 0,
    "outbound_quantity": 0,
    "disposed_quantity": 0,
    "expenditure": 0,
    "disposal_amount": 0,
}


def _create_item(
    inventory: InventoryService,
    name: str,
    *,
    quantity: int = 0,
    reference_price: int | None = None,
) -> int:
    return inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name=name,
            category_id=2,
            initial_quantity=quantity,
            initial_staff_id=1 if quantity else None,
            reference_price=reference_price,
        )
    ).id


def _create_item_directly(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO items (client_id, purchaser_id, code, name, category_id) "
        "VALUES (1, 1, 'ST-9999', '直接投入', 2)"
    )
    return int(conn.execute("SELECT id FROM items WHERE code = 'ST-9999'").fetchone()[0])


def _insert_movement(
    conn: sqlite3.Connection,
    item_id: int,
    reason: str,
    delta: object,
    unit_price: object = None,
) -> None:
    conn.execute(
        "INSERT INTO stock_movements "
        "(item_id, client_id, purchaser_id, staff_id, reason, delta, unit_price) "
        "VALUES (?, 1, 1, 1, ?, ?, ?)",
        (item_id, reason, delta, unit_price),
    )


def _assert_stored_totals_match_history(
    inventory: InventoryService, conn: sqlite3.Connection
) -> dict[str, int]:
    stored = inventory.aggregates.get()
    assert stored == compute_aggregates(conn)
    return stored


def test_compute_aggregates_is_zero_without_history(seeded_conn: sqlite3.Connection) -> None:
    assert compute_aggregates(seeded_conn) == ZERO_TOTALS


def test_total_aggregates_follow_every_operation_and_reversal(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    priced = _create_item(inventory, "単価あり", quantity=10, reference_price=100)
    unpriced = _create_item(inventory, "単価なし", quantity=5)
    _assert_stored_totals_match_history(inventory, seeded_conn)

    first_receipt = inventory.receive(priced, 1, 5, unit_price=200)
    assert _assert_stored_totals_match_history(inventory, seeded_conn)["inbound_quantity"] == 5

    issue = inventory.issue(priced, 1, 2, "用途")
    totals = _assert_stored_totals_match_history(inventory, seeded_conn)
    assert (totals["outbound_quantity"], totals["expenditure"]) == (2, 200)

    inventory.dispose(priced, 1, 1)
    totals = _assert_stored_totals_match_history(inventory, seeded_conn)
    assert (totals["disposed_quantity"], totals["disposal_amount"]) == (1, 100)

    before = _assert_stored_totals_match_history(inventory, seeded_conn)
    inventory.return_to_supplier(priced, 1, 1)
    inventory.stocktake(priced, 1, 5)
    assert _assert_stored_totals_match_history(inventory, seeded_conn) == before

    inventory.reverse(issue.id, 1)
    totals = _assert_stored_totals_match_history(inventory, seeded_conn)
    assert (totals["outbound_quantity"], totals["expenditure"]) == (0, 0)

    inventory.receive(priced, 1, 3)
    inventory.reverse(first_receipt.id, 1)

    inventory.issue(unpriced, 1, 3, "単価なし出庫")
    inventory.dispose(unpriced, 1, 1)

    assert _assert_stored_totals_match_history(inventory, seeded_conn) == {
        "inbound_quantity": 3,
        "outbound_quantity": 3,
        "disposed_quantity": 2,
        "expenditure": 0,
        "disposal_amount": 100,
    }


def test_compute_aggregates_rejects_non_integer_values(seeded_conn: sqlite3.Connection) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, "in", 1.5)
    with pytest.raises(ValueError, match="整数"):
        compute_aggregates(seeded_conn)


def test_find_limit_violations_accepts_exact_limits(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    _create_item(
        inventory,
        "上限品",
        quantity=MAX_STOCK_QUANTITY,
        reference_price=MAX_UNIT_PRICE,
    )
    amount_item = _create_item(inventory, "金額上限品", quantity=10_000, reference_price=10_000)
    inventory.issue(amount_item, 1, 10_000, "上限ちょうど")
    assert MAX_MOVEMENT_AMOUNT == 10_000 * 10_000
    assert find_limit_violations(seeded_conn) == []


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("quantity", MAX_STOCK_QUANTITY + 1),
        ("quantity", -1),
        ("quantity", 1.5),
        ("quantity", "abc"),
        ("reorder_threshold", MAX_STOCK_QUANTITY + 1),
        ("reorder_threshold", -1),
        ("reorder_quantity", 0),
        ("reorder_quantity", MAX_STOCK_QUANTITY + 1),
        ("reference_price", MAX_UNIT_PRICE + 1),
        ("reference_price", -1),
        ("reference_price", 1.5),
    ],
)
def test_find_limit_violations_reports_item_columns(
    seeded_conn: sqlite3.Connection, column: str, value: object
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(f"UPDATE items SET {column} = ? WHERE id = ?", (value, item_id))

    violations = find_limit_violations(seeded_conn)

    assert any(f"items ID {item_id}" in text and column in text for text in violations)


@pytest.mark.parametrize(
    ("reason", "delta", "unit_price", "column"),
    [
        ("in", MAX_STOCK_QUANTITY + 1, None, "delta"),
        ("out", -(MAX_STOCK_QUANTITY + 1), None, "delta"),
        ("in", 1.5, None, "delta"),
        ("in", "abc", None, "delta"),
        ("in", 1, MAX_UNIT_PRICE + 1, "unit_price"),
        ("in", 1, -1, "unit_price"),
        ("in", 1, 1.5, "unit_price"),
        ("in", 1, "abc", "unit_price"),
        ("out", -10_001, 10_000, "amount"),
        ("dispose", -10_001, 10_000, "amount"),
    ],
)
def test_find_limit_violations_reports_movement_columns(
    seeded_conn: sqlite3.Connection,
    reason: str,
    delta: object,
    unit_price: object,
    column: str,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, reason, delta, unit_price)

    violations = find_limit_violations(seeded_conn)

    assert any("stock_movements" in text and column in text for text in violations)


@pytest.mark.parametrize("reason", ["in", "return", "adjust"])
def test_find_limit_violations_does_not_apply_amount_limit_to_other_reasons(
    seeded_conn: sqlite3.Connection, reason: str
) -> None:
    item_id = _create_item_directly(seeded_conn)
    delta = 10_001 if reason == "in" else -10_001
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, reason, delta, 10_000)
        seeded_conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (max(delta, 0), item_id))

    assert not any("amount" in text for text in find_limit_violations(seeded_conn))


def test_find_limit_violations_accepts_missing_price_and_zero_price_in_amount_check(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, "out", -MAX_STOCK_QUANTITY, None)
        _insert_movement(seeded_conn, item_id, "dispose", -MAX_STOCK_QUANTITY, 0)

    assert not any("amount" in text for text in find_limit_violations(seeded_conn))


def test_find_limit_violations_reports_quantity_history_mismatch(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item_id = _create_item(inventory, "不一致品", quantity=3)
    seeded_conn.execute("UPDATE items SET quantity = 2 WHERE id = ?", (item_id,))

    violations = find_limit_violations(seeded_conn)

    assert any(f"品目 {item_id}" in text and "履歴合計" in text for text in violations)


@pytest.mark.parametrize(
    ("limit", "flagged"),
    [(100, True), (101, False)],
    ids=["above-limit", "exact-limit"],
)
def test_find_limit_violations_checks_each_aggregate_against_its_own_limit(
    seeded_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    flagged: bool,
) -> None:
    monkeypatch.setattr(integrity, "MAX_AGGREGATE_VALUE", limit)
    item_id = _create_item_directly(seeded_conn)
    _insert_movement(seeded_conn, item_id, "in", 101)
    _insert_movement(seeded_conn, item_id, "out", -101, 1)
    _insert_movement(seeded_conn, item_id, "dispose", -101, 1)

    violations = find_limit_violations(seeded_conn)

    for column in ZERO_TOTALS:
        assert any(f"全件 の {column}=101" in text for text in violations) is flagged


def test_find_limit_violations_does_not_sum_expenditure_and_disposal_amount(
    seeded_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrity, "MAX_AGGREGATE_VALUE", 150)
    item_id = _create_item_directly(seeded_conn)
    _insert_movement(seeded_conn, item_id, "out", -100, 1)
    _insert_movement(seeded_conn, item_id, "dispose", -100, 1)

    violations = find_limit_violations(seeded_conn)

    assert not any("expenditure" in text or "disposal_amount" in text for text in violations)


def test_find_limit_violations_skips_aggregation_after_numeric_violation(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, "in", 1.5)

    violations = find_limit_violations(seeded_conn)

    assert any("delta" in text for text in violations)
