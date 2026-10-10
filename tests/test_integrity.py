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
from inventory_manager_mini.db.integrity import (
    compute_aggregates,
    find_fifo_migration_violations,
    find_fifo_violations,
    find_limit_violations,
    replay_allocations,
)
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


def _create_item_directly(conn: sqlite3.Connection, code: str = "ST-9999") -> int:
    conn.execute(
        "INSERT INTO items (client_id, purchaser_id, code, name, category_id) "
        "VALUES (1, 1, ?, '直接投入', 2)",
        (code,),
    )
    return int(conn.execute("SELECT id FROM items WHERE code = ?", (code,)).fetchone()[0])


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


D1 = "2026-01-01 00:00:00"
D2 = "2026-01-02 00:00:00"
D3 = "2026-01-03 00:00:00"
D4 = "2026-01-04 00:00:00"


def _add(
    conn: sqlite3.Connection,
    item_id: int,
    movement_id: int,
    reason: str,
    delta: object,
    *,
    unit_price: object = None,
    cost_amount: object = None,
    reversal_of: int | None = None,
    moved_at: str = D1,
) -> int:
    conn.execute(
        "INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason, "
        "delta, unit_price, cost_amount, reversal_of, moved_at) "
        "VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?, ?, ?)",
        (movement_id, item_id, reason, delta, unit_price, cost_amount, reversal_of, moved_at),
    )
    return movement_id


def _allocate(conn: sqlite3.Connection, movement_id: int, lot_id: int, quantity: object) -> None:
    conn.execute(
        "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (?, ?, ?)",
        (movement_id, lot_id, quantity),
    )


def _sync_quantity(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE items SET quantity = (SELECT COALESCE(SUM(delta), 0) FROM stock_movements "
        "WHERE item_id = ?) WHERE id = ?",
        (item_id, item_id),
    )


def _seed_consumed_lots(conn: sqlite3.Connection, item_id: int, quantity: int) -> None:
    """入庫ロットから出庫、棚卸増加ロットから廃棄して、各指標を同じ値にする。"""
    _add(conn, item_id, 10, "in", quantity, unit_price=1, moved_at=D1)
    _add(conn, item_id, 11, "adjust", quantity, unit_price=1, moved_at=D1)
    _add(conn, item_id, 12, "out", -quantity, cost_amount=quantity, moved_at=D2)
    _add(conn, item_id, 13, "dispose", -quantity, cost_amount=quantity, moved_at=D2)
    _allocate(conn, 12, 10, quantity)
    _allocate(conn, 13, 11, quantity)
    _sync_quantity(conn, item_id)


def _assert_stored_totals_match_history(
    inventory: InventoryService, conn: sqlite3.Connection
) -> dict[str, int]:
    stored = inventory.aggregates.get()
    assert stored == compute_aggregates(conn)
    return stored


def test_compute_aggregates_is_zero_without_history(seeded_conn: sqlite3.Connection) -> None:
    assert compute_aggregates(seeded_conn) == ZERO_TOTALS


def test_fifo_replay_accepts_reversed_issue_and_fully_restored_lot(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    seeded_conn.executemany(
        "INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason, "
        "delta, unit_price, cost_amount, reversal_of, moved_at) "
        "VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?, ?, ?)",
        [
            (10, item_id, "in", 5, 100, None, None, "2026-01-01 00:00:00"),
            (11, item_id, "out", -3, None, 300, None, "2026-01-02 00:00:00"),
            (12, item_id, "out", 3, None, -300, 11, "2026-01-03 00:00:00"),
            (13, item_id, "in", -5, 100, None, 10, "2026-01-04 00:00:00"),
        ],
    )
    seeded_conn.executemany(
        "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (?, 10, ?)",
        [(11, 3), (12, -3)],
    )
    seeded_conn.execute("UPDATE items SET quantity = 0 WHERE id = ?", (item_id,))

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.remaining_by_lot == {10: 0}
    assert replay.allocations == {11: ((10, 3),), 12: ((10, -3),)}
    assert find_fifo_violations(seeded_conn) == []
    assert compute_aggregates(seeded_conn, version=4) == ZERO_TOTALS


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


def test_find_limit_violations_accepts_exact_limits(seeded_conn: sqlite3.Connection) -> None:
    full_item = _create_item_directly(seeded_conn)
    _add(seeded_conn, full_item, 1, "in", MAX_STOCK_QUANTITY, unit_price=MAX_UNIT_PRICE)
    seeded_conn.execute(
        "UPDATE items SET quantity = ?, reference_price = ? WHERE id = ?",
        (MAX_STOCK_QUANTITY, MAX_UNIT_PRICE, full_item),
    )
    amount_item = _create_item_directly(seeded_conn, "ST-9998")
    _add(seeded_conn, amount_item, 2, "in", 10_000, unit_price=10_000, moved_at=D1)
    _add(seeded_conn, amount_item, 3, "out", -10_000, cost_amount=MAX_MOVEMENT_AMOUNT, moved_at=D2)
    _allocate(seeded_conn, 3, 2, 10_000)
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


@pytest.mark.parametrize("reason", ["out", "dispose"])
def test_find_limit_violations_version_three_reports_issue_and_dispose_amount(
    seeded_conn: sqlite3.Connection, reason: str
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, reason, -10_001, 10_000)

    assert any("の amount=100010000 " in t for t in find_limit_violations(seeded_conn, version=3))
    # v4 では 1 操作金額を旧方式で検査せず cost_amount の範囲で検査する
    assert not any("の amount=" in t for t in find_limit_violations(seeded_conn))


@pytest.mark.parametrize("reason", ["in", "return", "adjust"])
def test_find_limit_violations_does_not_apply_amount_limit_to_other_reasons(
    seeded_conn: sqlite3.Connection, reason: str
) -> None:
    item_id = _create_item_directly(seeded_conn)
    delta = 10_001 if reason == "in" else -10_001
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, reason, delta, 10_000)
        seeded_conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (max(delta, 0), item_id))

    assert not any("の amount=" in t for t in find_limit_violations(seeded_conn, version=3))


def test_find_limit_violations_accepts_missing_price_and_zero_price_in_amount_check(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _insert_movement(seeded_conn, item_id, "out", -MAX_STOCK_QUANTITY, None)
        _insert_movement(seeded_conn, item_id, "dispose", -MAX_STOCK_QUANTITY, 0)

    assert not any("の amount=" in t for t in find_limit_violations(seeded_conn, version=3))


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
    _seed_consumed_lots(seeded_conn, item_id, 101)

    violations = find_limit_violations(seeded_conn)

    for column in ZERO_TOTALS:
        assert any(f"全件 の {column}=101" in text for text in violations) is flagged


def test_find_limit_violations_does_not_sum_expenditure_and_disposal_amount(
    seeded_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(integrity, "MAX_AGGREGATE_VALUE", 150)
    item_id = _create_item_directly(seeded_conn)
    _seed_consumed_lots(seeded_conn, item_id, 100)

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


def test_unchecked_constraints_restores_checks_after_an_exception(
    seeded_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(RuntimeError), unchecked_constraints(seeded_conn):
        assert seeded_conn.execute("PRAGMA ignore_check_constraints").fetchone() == (1,)
        raise RuntimeError("body failure")

    assert seeded_conn.execute("PRAGMA ignore_check_constraints").fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError):
        _create_item_directly(seeded_conn)
        seeded_conn.execute("UPDATE items SET quantity = -1")


type _Row = tuple[int, str, object, int | None, int | None, str]


def _load(
    conn: sqlite3.Connection, item_id: int, rows: list[_Row], *, sync_quantity: bool = True
) -> None:
    with unchecked_constraints(conn):
        for movement_id, reason, delta, price, reversal_of, moved_at in rows:
            _add(
                conn,
                item_id,
                movement_id,
                reason,
                delta,
                unit_price=price,
                reversal_of=reversal_of,
                moved_at=moved_at,
            )
        if sync_quantity:
            _sync_quantity(conn, item_id)


def test_replay_allocates_oldest_lot_first_across_lots_and_skips_empty_lots(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (10, "in", 2, 100, None, D1),
            (11, "in", 3, 200, None, D2),
            (12, "out", -4, None, None, D3),
            (13, "out", -1, None, None, D4),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.allocations == {12: ((10, 2), (11, 2)), 13: ((11, 1),)}
    assert replay.cost_amounts[12] == 600
    assert replay.cost_amounts[13] == 200
    assert replay.cost_amounts[10] is None
    assert replay.unit_prices[10] == 100
    assert replay.unit_prices[12] is None
    assert replay.remaining_by_lot == {10: 0, 11: 0}


def test_replay_orders_lots_by_time_before_id(seeded_conn: sqlite3.Connection) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (30, "in", 2, 300, None, D1),
            (10, "in", 2, 100, None, D2),
            (40, "out", -3, None, None, D3),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.allocations == {40: ((10, 1), (30, 2))}
    assert replay.cost_amounts[40] == 2 * 300 + 1 * 100
    assert replay.remaining_by_lot == {30: 0, 10: 1}


def test_replay_orders_lots_with_same_time_by_id(seeded_conn: sqlite3.Connection) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (21, "in", 1, 50, None, D1),
            (20, "in", 1, 60, None, D1),
            (40, "out", -1, None, None, D2),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.allocations == {40: ((20, 1),)}
    assert replay.cost_amounts[40] == 60
    assert replay.remaining_by_lot == {20: 0, 21: 1}


def test_replay_counts_unpriced_lot_quantity_without_cost_and_zero_price_as_zero(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (10, "in", 2, None, None, D1),
            (11, "in", 2, 0, None, D2),
            (12, "in", 2, 50, None, D3),
            (13, "out", -5, None, None, D4),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.allocations == {13: ((10, 2), (11, 2), (12, 1))}
    assert replay.cost_amounts[13] == 50
    assert replay.unit_prices[10] is None
    assert replay.unit_prices[11] == 0


def test_replay_treats_positive_adjust_as_lot_and_zero_adjust_as_unpriced(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (10, "adjust", 3, 70, None, D1),
            (11, "adjust", 0, 300, None, D2),
            (12, "out", -2, None, None, D3),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.allocations == {12: ((10, 2),)}
    assert replay.cost_amounts[12] == 140
    assert replay.unit_prices[10] == 70
    assert replay.unit_prices[11] is None
    assert replay.remaining_by_lot == {10: 1}


def test_replay_keeps_lots_of_each_item_separate(seeded_conn: sqlite3.Connection) -> None:
    first = _create_item_directly(seeded_conn)
    second = _create_item_directly(seeded_conn, "ST-9998")
    _load(seeded_conn, first, [(10, "in", 2, 100, None, D1)])
    _load(
        seeded_conn,
        second,
        [(11, "in", 2, 500, None, D1), (12, "out", -1, None, None, D2)],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.allocations == {12: ((11, 1),)}
    assert replay.cost_amounts[12] == 500
    assert replay.remaining_by_lot == {10: 2, 11: 1}


def test_replay_reverses_each_decrease_with_signed_allocations(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (10, "in", 5, 100, None, D1),
            (11, "out", -2, None, None, D2),
            (12, "return", -1, None, None, D2),
            (13, "dispose", -1, None, None, D3),
            (14, "out", 2, None, 11, D4),
            (15, "return", 1, None, 12, D4),
            (16, "dispose", 1, None, 13, D4),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.allocations == {
        11: ((10, 2),),
        12: ((10, 1),),
        13: ((10, 1),),
        14: ((10, -2),),
        15: ((10, -1),),
        16: ((10, -1),),
    }
    assert replay.cost_amounts[11] == 200
    assert replay.cost_amounts[12] is None
    assert replay.cost_amounts[13] == 100
    assert replay.cost_amounts[14] == -200
    assert replay.cost_amounts[15] is None
    assert replay.cost_amounts[16] == -100
    assert all(replay.unit_prices[movement_id] is None for movement_id in (11, 14, 15, 16))
    assert replay.remaining_by_lot == {10: 5}


def test_replay_lot_reversal_copies_unit_price_and_empties_lot(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (10, "in", 5, 100, None, D1),
            (11, "in", -5, 100, 10, D2),
            (12, "adjust", 3, 70, None, D3),
            (13, "adjust", -3, 70, 12, D4),
        ],
    )

    replay = replay_allocations(seeded_conn)

    assert replay.violations == ()
    assert replay.unit_prices[11] == 100
    assert replay.unit_prices[13] == 70
    assert replay.allocations == {}
    assert replay.remaining_by_lot == {10: 0, 12: 0}


@pytest.mark.parametrize(
    ("rows", "quantity", "expected", "foreign_keys"),
    [
        (
            [(10, "in", 1, 100, None, D1), (11, "out", -2, None, None, D2)],
            0,
            "FIFO 引当が不足",
            True,
        ),
        (
            [
                (10, "in", 5, 100, None, D1),
                (11, "out", -3, None, None, D2),
                (12, "in", -5, 100, 10, D3),
            ],
            None,
            "消費済み",
            True,
        ),
        (
            [(10, "in", 1, 100, None, D1), (11, "in", -1, 100, 999, D2)],
            None,
            "取り消し元 ID 999",
            False,
        ),
        (
            [
                (10, "in", 1, 100, None, D1),
                (11, "in", -1, 100, 10, D2),
                (12, "in", 1, 100, 11, D3),
            ],
            None,
            "取り消し元 ID 11",
            True,
        ),
        (
            [(10, "in", 5, 100, None, D1)],
            4,
            "ロット残数合計が在庫数と一致しません",
            True,
        ),
        (
            [(10, "bogus", 1, None, None, D1)],
            0,
            "種別 'bogus' は不正",
            True,
        ),
        (
            [(10, "in", 1.5, None, None, D1)],
            0,
            "数量が整数ではありません",
            True,
        ),
        (
            [(10, "out", 1, None, None, D1), (11, "out", -1, None, 10, D2)],
            None,
            "取り消し対象ロットがありません",
            True,
        ),
    ],
    ids=[
        "insufficient",
        "consumed-lot-reversal",
        "missing-reversal-source",
        "reversal-of-reversal",
        "lot-total-mismatch",
        "invalid-reason",
        "non-integer-delta",
        "reversal-of-non-lot-increase",
    ],
)
def test_replay_reports_violations(
    seeded_conn: sqlite3.Connection,
    rows: list[_Row],
    quantity: int | None,
    expected: str,
    foreign_keys: bool,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    if not foreign_keys:
        seeded_conn.execute("PRAGMA foreign_keys = OFF")
    _load(seeded_conn, item_id, rows, sync_quantity=quantity is None)
    if quantity is not None:
        seeded_conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (quantity, item_id))

    replay = replay_allocations(seeded_conn)

    assert any(expected in violation for violation in replay.violations)


def test_replay_rejects_reversal_of_another_items_movement(
    seeded_conn: sqlite3.Connection,
) -> None:
    first = _create_item_directly(seeded_conn)
    second = _create_item_directly(seeded_conn, "ST-9998")
    _load(seeded_conn, first, [(10, "in", 1, 100, None, D1)])
    _load(seeded_conn, second, [(11, "in", -1, 100, 10, D2)], sync_quantity=False)

    replay = replay_allocations(seeded_conn)

    assert any("取り消し元 ID 10" in violation for violation in replay.violations)


def _seed_consistent_fifo_history(conn: sqlite3.Connection) -> None:
    item_id = _create_item_directly(conn)
    _add(conn, item_id, 10, "in", 5, unit_price=100, moved_at=D1)
    _add(conn, item_id, 11, "out", -3, cost_amount=300, moved_at=D2)
    _add(conn, item_id, 12, "adjust", 0, moved_at=D3)
    _allocate(conn, 11, 10, 3)
    _sync_quantity(conn, item_id)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            "DELETE FROM stock_allocations WHERE movement_id = 11",
            "履歴 ID 11 の引当明細",
        ),
        (
            "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (12, 10, 1)",
            "履歴 ID 12 の引当明細",
        ),
        (
            "UPDATE stock_allocations SET quantity = 2",
            "履歴 ID 11 の引当明細",
        ),
        (
            "UPDATE stock_allocations SET lot_id = 12",
            "履歴 ID 11 の引当明細",
        ),
        (
            "UPDATE stock_movements SET unit_price = 5 WHERE id = 11",
            "履歴 ID 11 の unit_price",
        ),
        (
            # ロット単価は保存値を正として再生するため、単価の消去は原価の不一致として現れる
            "UPDATE stock_movements SET unit_price = NULL WHERE id = 10",
            "履歴 ID 11 の cost_amount",
        ),
        (
            "UPDATE stock_movements SET unit_price = 300 WHERE id = 12",
            "履歴 ID 12 の unit_price",
        ),
        (
            "UPDATE stock_movements SET cost_amount = 301 WHERE id = 11",
            "履歴 ID 11 の cost_amount が引当明細と一致しません",
        ),
    ],
    ids=[
        "missing-allocation",
        "extra-allocation",
        "changed-quantity",
        "changed-lot",
        "issue-with-unit-price",
        "erased-lot-price-changes-cost",
        "zero-adjust-with-unit-price",
        "changed-cost",
    ],
)
def test_find_fifo_violations_reports_each_stored_inconsistency(
    seeded_conn: sqlite3.Connection, mutation: str, expected: str
) -> None:
    _seed_consistent_fifo_history(seeded_conn)
    assert find_fifo_violations(seeded_conn) == []

    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(mutation)

    assert any(expected in violation for violation in find_fifo_violations(seeded_conn))


@pytest.mark.parametrize(
    ("reason", "delta", "cost"),
    [
        ("out", -1, None),
        ("dispose", -1, None),
        ("in", 1, 5),
        ("out", -1, MAX_MOVEMENT_AMOUNT + 1),
        ("out", -1, -MAX_MOVEMENT_AMOUNT - 1),
        ("out", -1, 1.5),
    ],
    ids=["issue-without-cost", "dispose-without-cost", "in-with-cost", "over", "under", "real"],
)
def test_find_limit_violations_reports_invalid_cost_amount(
    seeded_conn: sqlite3.Connection, reason: str, delta: int, cost: object
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _add(seeded_conn, item_id, 1, reason, delta, cost_amount=cost)

    violations = find_limit_violations(seeded_conn)

    assert any("stock_movements ID 1 の cost_amount=" in text for text in violations)


@pytest.mark.parametrize("quantity", [0, MAX_STOCK_QUANTITY + 1, -MAX_STOCK_QUANTITY - 1, 1.5])
def test_find_limit_violations_reports_invalid_allocation_quantity(
    seeded_conn: sqlite3.Connection, quantity: object
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _add(seeded_conn, item_id, 1, "in", 1, unit_price=1, moved_at=D1)
        _add(seeded_conn, item_id, 2, "out", -1, cost_amount=1, moved_at=D2)
        _allocate(seeded_conn, 2, 1, quantity)

    violations = find_limit_violations(seeded_conn)

    assert any("stock_allocations ID" in text and "quantity=" in text for text in violations)


@pytest.mark.parametrize(
    ("quantity", "rejected"),
    [(100_000, False), (100_001, True)],
    ids=["exact-limit", "over-limit"],
)
def test_find_fifo_migration_violations_checks_cost_range_including_reversal(
    seeded_conn: sqlite3.Connection, quantity: int, rejected: bool
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _load(
        seeded_conn,
        item_id,
        [
            (10, "in", quantity, 1_000, None, D1),
            (11, "out", -quantity, None, None, D2),
            (12, "out", quantity, None, 11, D3),
        ],
    )

    violations = find_fifo_migration_violations(seeded_conn)

    if rejected:
        assert any("ID 11 の cost_amount=100001000" in text for text in violations)
        assert any("ID 12 の cost_amount=-100001000" in text for text in violations)
    else:
        assert violations == []


def test_find_fifo_migration_violations_stops_at_legacy_limit_violations(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    seeded_conn.execute("UPDATE items SET quantity = 3 WHERE id = ?", (item_id,))

    violations = find_fifo_migration_violations(seeded_conn)

    assert any("履歴合計" in text for text in violations)
    assert not any("ロット残数" in text for text in violations)


def test_compute_aggregates_uses_allocations_and_lot_prices(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _add(seeded_conn, item_id, 10, "in", 5, unit_price=100, moved_at=D1)
    _add(seeded_conn, item_id, 11, "in", 5, unit_price=200, moved_at=D1)
    _add(seeded_conn, item_id, 12, "out", -7, cost_amount=900, moved_at=D2)
    _add(seeded_conn, item_id, 13, "dispose", -2, cost_amount=400, moved_at=D3)
    _add(seeded_conn, item_id, 14, "return", -1, moved_at=D3)
    _add(seeded_conn, item_id, 15, "dispose", 2, cost_amount=-400, reversal_of=13, moved_at=D4)
    for movement_id, lot_id, quantity in [
        (12, 10, 5),
        (12, 11, 2),
        (13, 11, 2),
        (14, 11, 1),
        (15, 11, -2),
    ]:
        _allocate(seeded_conn, movement_id, lot_id, quantity)
    _sync_quantity(seeded_conn, item_id)

    assert compute_aggregates(seeded_conn) == {
        "inbound_quantity": 10,
        "outbound_quantity": 7,
        "disposed_quantity": 0,
        "expenditure": 900,
        "disposal_amount": 0,
    }
    assert find_fifo_violations(seeded_conn) == []


def test_compute_aggregates_switches_method_by_schema_version(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    _add(seeded_conn, item_id, 10, "in", 5, unit_price=100, moved_at=D1)
    _add(seeded_conn, item_id, 11, "out", -2, cost_amount=200, moved_at=D2)
    _allocate(seeded_conn, 11, 10, 2)
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute("UPDATE stock_movements SET unit_price = 50 WHERE id = 11")

    assert compute_aggregates(seeded_conn, version=3)["expenditure"] == 100
    assert compute_aggregates(seeded_conn, version=4)["expenditure"] == 200
    assert compute_aggregates(seeded_conn)["expenditure"] == 200


def test_compute_aggregates_rejects_non_integer_allocation_quantity(
    seeded_conn: sqlite3.Connection,
) -> None:
    item_id = _create_item_directly(seeded_conn)
    with unchecked_constraints(seeded_conn):
        _add(seeded_conn, item_id, 10, "in", 1, unit_price=1, moved_at=D1)
        _add(seeded_conn, item_id, 11, "out", -1, cost_amount=1, moved_at=D2)
        _allocate(seeded_conn, 11, 10, 1.5)

    with pytest.raises(ValueError, match="整数"):
        compute_aggregates(seeded_conn)
