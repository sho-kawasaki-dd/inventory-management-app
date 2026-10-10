from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from importlib.resources import files

import pytest

from inventory_manager_mini.core.models import (
    MAX_AGGREGATE_VALUE,
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
)
from inventory_manager_mini.db.connection import connect_memory
from inventory_manager_mini.db.integrity import AGGREGATE_COLUMNS


@pytest.fixture
def schema_conn() -> Iterator[sqlite3.Connection]:
    conn = connect_memory()
    schema = files("inventory_manager_mini.db").joinpath("schema.sql")
    conn.executescript(schema.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO clients (id, name) VALUES (1, '利用者')")
    conn.execute("INSERT INTO purchasers (id, name) VALUES (1, '発注主体')")
    conn.execute("INSERT INTO staff (id, name) VALUES (1, '担当者')")
    conn.execute("INSERT INTO categories (id, name, code_prefix) VALUES (1, 'カテゴリ', 'CAT')")
    conn.execute("INSERT INTO locations (id, name) VALUES (1, '保管場所')")
    conn.execute(
        """INSERT INTO items
        (id, client_id, purchaser_id, code, name, category_id, location_id)
        VALUES (1, 1, 1, 'CAT-0001', '品目', 1, 1)"""
    )
    conn.execute(
        """INSERT INTO stock_movements
        (id, item_id, client_id, purchaser_id, staff_id, reason, delta)
        VALUES (1, 1, 1, 1, 1, 'in', 1)"""
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_item(
    conn: sqlite3.Connection,
    *,
    client_id: int | None = 1,
    purchaser_id: int | None = 1,
    code: str = "CAT-0002",
    category_id: int = 1,
    unit: str = "個",
    quantity: int = 0,
    reorder_threshold: int = 0,
    reorder_quantity: int | None = 1,
    reference_price: int | None = 0,
    is_active: int = 1,
) -> None:
    conn.execute(
        """INSERT INTO items
        (client_id, purchaser_id, code, name, category_id, unit, quantity,
         reorder_threshold, reorder_quantity, reference_price, is_active)
        VALUES (?, ?, ?, '追加品目', ?, ?, ?, ?, ?, ?, ?)""",
        (
            client_id,
            purchaser_id,
            code,
            category_id,
            unit,
            quantity,
            reorder_threshold,
            reorder_quantity,
            reference_price,
            is_active,
        ),
    )


def _insert_movement(
    conn: sqlite3.Connection,
    *,
    reason: str = "in",
    delta: int = 1,
    reversal_of: int | None = None,
    item_id: int = 1,
    client_id: int | None = 1,
    purchaser_id: int | None = 1,
    unit_price: int | None = 0,
    cost_amount: object = None,
) -> None:
    if cost_amount is None and reason in {"out", "dispose"}:
        cost_amount = 0
    conn.execute(
        """INSERT INTO stock_movements
        (item_id, client_id, purchaser_id, staff_id, reason, delta,
         unit_price, cost_amount, reversal_of)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (item_id, client_id, purchaser_id, reason, delta, unit_price, cost_amount, reversal_of),
    )


@pytest.mark.parametrize("table", ["clients", "purchasers", "staff", "items"])
def test_is_active_check_rejects_values_outside_zero_and_one(
    schema_conn: sqlite3.Connection, table: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(f"UPDATE {table} SET is_active = 2 WHERE id = 1")


@pytest.mark.parametrize("prefix", ["A", "TOOLONG", "ab", "A-B", "A_"])
def test_category_prefix_check(schema_conn: sqlite3.Connection, prefix: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO categories (name, code_prefix) VALUES ('不正', ?)", (prefix,)
        )


@pytest.mark.parametrize(
    "values",
    [
        {"unit": "箱"},
        {"quantity": -1},
        {"quantity": 1_000_001},
        {"quantity": 1.5},
        {"reorder_threshold": -1},
        {"reorder_threshold": 1_000_001},
        {"reorder_quantity": 0},
        {"reorder_quantity": 1_000_001},
        {"reference_price": -1},
        {"reference_price": 10_000_001},
        {"reference_price": 1.5},
    ],
)
def test_item_checks_reject_invalid_values(
    schema_conn: sqlite3.Connection, values: dict[str, object]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(schema_conn, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("unit_price", [-1])
def test_movement_unit_price_check(schema_conn: sqlite3.Connection, unit_price: int) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_movement(schema_conn, unit_price=unit_price)


@pytest.mark.parametrize(
    ("reason", "delta", "unit_price"),
    [
        ("in", 1_000_001, 0),
        ("in", 1.5, 0),
        ("in", 1, 10_000_001),
        ("in", 1, 1.5),
        ("out", -10_001, MAX_UNIT_PRICE + 1),
    ],
)
def test_movement_checks_reject_out_of_range_or_non_integer_values(
    schema_conn: sqlite3.Connection,
    reason: str,
    delta: int,
    unit_price: int,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_movement(schema_conn, reason=reason, delta=delta, unit_price=unit_price)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("inbound_quantity", 1_000_000_000_001),
        ("outbound_quantity", 1.5),
        ("disposed_quantity", -1),
        ("expenditure", 1_000_000_000_001),
        ("disposal_amount", 1.5),
    ],
)
def test_total_aggregate_checks_reject_invalid_values(
    schema_conn: sqlite3.Connection,
    column: str,
    value: int,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(f"UPDATE total_aggregates SET {column} = ? WHERE id = 1", (value,))


@pytest.mark.parametrize(
    "values",
    [
        {"quantity": 0},
        {"quantity": MAX_STOCK_QUANTITY},
        {"reorder_threshold": MAX_STOCK_QUANTITY},
        {"reorder_quantity": None},
        {"reorder_quantity": 1},
        {"reorder_quantity": MAX_STOCK_QUANTITY},
        {"reference_price": None},
        {"reference_price": 0},
        {"reference_price": MAX_UNIT_PRICE},
    ],
)
def test_item_checks_accept_values_up_to_the_business_limits(
    schema_conn: sqlite3.Connection, values: dict[str, object]
) -> None:
    _insert_item(schema_conn, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["abc", -1, MAX_STOCK_QUANTITY + 1])
def test_item_integer_columns_reject_text_and_out_of_range(
    schema_conn: sqlite3.Connection, value: object
) -> None:
    for column in ("quantity", "reorder_threshold", "reorder_quantity"):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(schema_conn, **{column: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("reason", "delta", "unit_price", "cost_amount"),
    [
        ("in", MAX_STOCK_QUANTITY, None, None),
        ("in", MAX_STOCK_QUANTITY, 0, None),
        ("in", MAX_STOCK_QUANTITY, MAX_UNIT_PRICE, None),
        ("return", -MAX_STOCK_QUANTITY, None, None),
        ("adjust", MAX_STOCK_QUANTITY, MAX_UNIT_PRICE, None),
        ("adjust", -MAX_STOCK_QUANTITY, MAX_UNIT_PRICE, None),
        ("out", -MAX_STOCK_QUANTITY, None, 0),
        ("out", -MAX_STOCK_QUANTITY, None, MAX_MOVEMENT_AMOUNT),
        ("dispose", -MAX_STOCK_QUANTITY, None, 0),
        ("out", -10_000, None, MAX_MOVEMENT_AMOUNT),
        ("dispose", -10_000, None, MAX_MOVEMENT_AMOUNT),
        ("out", -(MAX_MOVEMENT_AMOUNT // MAX_UNIT_PRICE), None, MAX_MOVEMENT_AMOUNT),
    ],
)
def test_movement_checks_accept_values_up_to_the_business_limits(
    schema_conn: sqlite3.Connection,
    reason: str,
    delta: int,
    unit_price: int | None,
    cost_amount: int | None,
) -> None:
    _insert_movement(
        schema_conn,
        reason=reason,
        delta=delta,
        unit_price=unit_price,
        cost_amount=cost_amount,
    )


@pytest.mark.parametrize(
    ("reason", "delta", "unit_price", "cost_amount"),
    [
        ("out", -(MAX_STOCK_QUANTITY + 1), None, 0),
        ("adjust", -(MAX_STOCK_QUANTITY + 1), None, None),
        ("in", "abc", None, None),
        ("in", 1, "abc", None),
        ("dispose", -1, None, MAX_MOVEMENT_AMOUNT + 1),
        ("out", -1, MAX_UNIT_PRICE + 1, 1),
        ("out", -1, None, 1.5),
    ],
)
def test_movement_checks_reject_values_beyond_the_business_limits(
    schema_conn: sqlite3.Connection,
    reason: str,
    delta: object,
    unit_price: object,
    cost_amount: object,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_movement(
            schema_conn,
            reason=reason,
            delta=delta,  # type: ignore[arg-type]
            unit_price=unit_price,  # type: ignore[arg-type]
            cost_amount=cost_amount,
        )


@pytest.mark.parametrize(
    ("cost_amount", "accepted"),
    [(-MAX_MOVEMENT_AMOUNT, True), (-MAX_MOVEMENT_AMOUNT - 1, False), (1, False)],
)
def test_reversal_cost_amount_is_negative_and_within_the_limit(
    schema_conn: sqlite3.Connection, cost_amount: int, accepted: bool
) -> None:
    for reason in ("out", "dispose"):
        schema_conn.execute("SAVEPOINT reversal_case")
        try:
            if accepted:
                _insert_movement(
                    schema_conn,
                    reason=reason,
                    delta=1,
                    unit_price=None,
                    cost_amount=cost_amount,
                    reversal_of=1,
                )
            else:
                with pytest.raises(sqlite3.IntegrityError):
                    _insert_movement(
                        schema_conn,
                        reason=reason,
                        delta=1,
                        unit_price=None,
                        cost_amount=cost_amount,
                        reversal_of=1,
                    )
        finally:
            schema_conn.execute("ROLLBACK TO reversal_case")
            schema_conn.execute("RELEASE reversal_case")


def test_cost_amount_is_required_for_outflow_and_forbidden_for_other_reasons(
    schema_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, reason, "
            "delta, unit_price, cost_amount) VALUES (1, 1, 1, 1, 'out', -1, NULL, NULL)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, reason, "
            "delta, cost_amount) VALUES (1, 1, 1, 1, 'in', 1, 0)"
        )


@pytest.mark.parametrize(
    ("quantity", "accepted"),
    [
        (1, True),
        (-1, True),
        (MAX_STOCK_QUANTITY, True),
        (-MAX_STOCK_QUANTITY, True),
        (0, False),
        (MAX_STOCK_QUANTITY + 1, False),
        (1.5, False),
        ("abc", False),
    ],
)
def test_allocation_quantity_check(
    schema_conn: sqlite3.Connection, quantity: object, accepted: bool
) -> None:
    if accepted:
        schema_conn.execute(
            "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (1, 1, ?)",
            (quantity,),
        )
    else:
        with pytest.raises(sqlite3.IntegrityError):
            schema_conn.execute(
                "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (1, 1, ?)",
                (quantity,),
            )


def test_allocation_requires_valid_references_and_unique_movement_lot_pair(
    schema_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (99, 1, 1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (1, 99, 1)"
        )
    schema_conn.execute(
        "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (1, 1, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO stock_allocations (movement_id, lot_id, quantity) VALUES (1, 1, -1)"
        )


@pytest.mark.parametrize("column", AGGREGATE_COLUMNS)
def test_total_aggregates_accept_zero_and_the_upper_limit(
    schema_conn: sqlite3.Connection, column: str
) -> None:
    for value in (0, MAX_AGGREGATE_VALUE):
        schema_conn.execute(f"UPDATE total_aggregates SET {column} = ? WHERE id = 1", (value,))
    assert schema_conn.execute(f"SELECT {column} FROM total_aggregates").fetchone() == (
        MAX_AGGREGATE_VALUE,
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            f"UPDATE total_aggregates SET {column} = ? WHERE id = 1", (MAX_AGGREGATE_VALUE + 1,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(f"UPDATE total_aggregates SET {column} = 'abc' WHERE id = 1")


def test_total_aggregates_has_single_zero_initialized_row(
    schema_conn: sqlite3.Connection,
) -> None:
    assert schema_conn.execute("SELECT * FROM total_aggregates").fetchall() == [(1, 0, 0, 0, 0, 0)]
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute("INSERT INTO total_aggregates (id) VALUES (2)")
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute("INSERT INTO total_aggregates (id) VALUES (1)")


@pytest.mark.parametrize(
    ("reason", "delta"),
    [
        ("unknown", 1),
        ("in", 0),
        ("in", -1),
        ("out", 0),
        ("out", 1),
        ("return", 1),
        ("dispose", 1),
    ],
)
def test_movement_reason_and_delta_checks(
    schema_conn: sqlite3.Connection, reason: str, delta: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_movement(schema_conn, reason=reason, delta=delta)


def test_reversal_movement_is_exempt_from_reason_delta_sign_check(
    schema_conn: sqlite3.Connection,
) -> None:
    _insert_movement(schema_conn, reason="in", delta=-1, reversal_of=1)


@pytest.mark.parametrize(
    ("table", "columns", "values"),
    [
        ("clients", "name", "'利用者'"),
        ("purchasers", "name", "'発注主体'"),
        ("staff", "name", "'担当者'"),
        ("locations", "name", "'保管場所'"),
    ],
)
def test_master_names_are_unique(
    schema_conn: sqlite3.Connection, table: str, columns: str, values: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})")


def test_category_prefix_and_item_code_are_unique(schema_conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO categories (name, code_prefix) VALUES ('別カテゴリ', 'CAT')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(schema_conn, code="CAT-0001")


@pytest.mark.parametrize("parent_id", [None, 1])
def test_category_name_is_unique_under_same_parent(
    schema_conn: sqlite3.Connection, parent_id: int | None
) -> None:
    schema_conn.execute(
        "INSERT INTO categories (parent_id, name, code_prefix) VALUES (?, '重複', 'DUP1')",
        (parent_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        schema_conn.execute(
            "INSERT INTO categories (parent_id, name, code_prefix) VALUES (?, '重複', 'DUP2')",
            (parent_id,),
        )


def test_reversal_of_is_unique(schema_conn: sqlite3.Connection) -> None:
    _insert_movement(schema_conn, reason="in", delta=-1, reversal_of=1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_movement(schema_conn, reason="in", delta=-1, reversal_of=1)


@pytest.mark.parametrize(
    "values",
    [
        {"client_id": 999},
        {"purchaser_id": 999},
        {"category_id": 999},
    ],
)
def test_item_foreign_keys(schema_conn: sqlite3.Connection, values: dict[str, int]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_item(schema_conn, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"item_id": 999},
        {"client_id": 999},
        {"purchaser_id": 999},
        {"reversal_of": 999},
    ],
)
def test_movement_foreign_keys(schema_conn: sqlite3.Connection, values: dict[str, int]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert_movement(schema_conn, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("items", "client_id"),
        ("items", "purchaser_id"),
        ("stock_movements", "client_id"),
        ("stock_movements", "purchaser_id"),
    ],
)
def test_required_client_and_purchaser_references_reject_null(
    schema_conn: sqlite3.Connection, table: str, column: str
) -> None:
    if table == "items":
        with pytest.raises(sqlite3.IntegrityError):
            _insert_item(schema_conn, **{column: None})  # type: ignore[arg-type]
    else:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_movement(schema_conn, **{column: None})  # type: ignore[arg-type]


def test_items_updated_at_trigger_and_explicit_override(
    schema_conn: sqlite3.Connection,
) -> None:
    schema_conn.execute("UPDATE items SET updated_at = '2000-01-01 00:00:00' WHERE id = 1")
    schema_conn.execute("UPDATE items SET note = '自動更新' WHERE id = 1")
    automatic_timestamp = schema_conn.execute(
        "SELECT updated_at FROM items WHERE id = 1"
    ).fetchone()[0]
    assert automatic_timestamp != "2000-01-01 00:00:00"

    schema_conn.execute(
        "UPDATE items SET updated_at = '2099-01-01 00:00:00', note = '明示値' WHERE id = 1"
    )
    assert schema_conn.execute("SELECT updated_at FROM items WHERE id = 1").fetchone() == (
        "2099-01-01 00:00:00",
    )


def test_schema_resource_contains_settings_default(schema_conn: sqlite3.Connection) -> None:
    assert schema_conn.execute("SELECT key, value FROM settings").fetchall() == [
        ("fiscal_year_start_month", "4")
    ]
