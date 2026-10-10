from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from inventory_manager_mini.core.errors import (
    AlreadyReversedError,
    InactiveItemError,
    InactiveMasterError,
    MasterInUseError,
    NegativeStockError,
    ReversalNotAllowedError,
    ValidationError,
)
from inventory_manager_mini.core.models import (
    MAX_AGGREGATE_VALUE,
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    Allocation,
    ItemFilter,
    ItemUpdate,
    NewItem,
    Reason,
    StockMovement,
)
from inventory_manager_mini.core.services import BackupService, InventoryService, MasterService
from inventory_manager_mini.db import repositories
from inventory_manager_mini.db.connection import connect, transaction
from inventory_manager_mini.db.integrity import (
    AGGREGATE_COLUMNS,
    compute_aggregates,
    find_fifo_violations,
    replay_allocations,
)
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, create_schema
from tests.conftest import unchecked_constraints


def _new_item(**kwargs: object) -> NewItem:
    values: dict[str, object] = {
        "client_id": 1,
        "purchaser_id": 1,
        "name": "備品",
        "category_id": 2,
        "location_id": 1,
        "reference_price": 100,
        "reorder_threshold": 2,
        "reorder_quantity": 5,
    }
    values.update(kwargs)
    return NewItem(**values)  # type: ignore[arg-type]


def _update_item(item: object, **kwargs: object) -> ItemUpdate:
    values: dict[str, object] = {
        "id": item.id,  # type: ignore[attr-defined]
        "client_id": item.client_id,  # type: ignore[attr-defined]
        "purchaser_id": item.purchaser_id,  # type: ignore[attr-defined]
        "name": item.name,  # type: ignore[attr-defined]
        "category_id": item.category_id,  # type: ignore[attr-defined]
        "location_id": item.location_id,  # type: ignore[attr-defined]
        "reorder_threshold": item.reorder_threshold,  # type: ignore[attr-defined]
        "reorder_quantity": item.reorder_quantity,  # type: ignore[attr-defined]
        "purchase_url": item.purchase_url,  # type: ignore[attr-defined]
        "supplier": item.supplier,  # type: ignore[attr-defined]
        "manufacturer_part_number": item.manufacturer_part_number,  # type: ignore[attr-defined]
        "application": item.application,  # type: ignore[attr-defined]
        "reference_price": item.reference_price,  # type: ignore[attr-defined]
        "note": item.note,  # type: ignore[attr-defined]
    }
    values.update(kwargs)
    return ItemUpdate(**values)  # type: ignore[arg-type]


def test_create_item_sets_unit_and_records_positive_initial_quantity(
    inventory: InventoryService,
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=4, initial_staff_id=2, reference_price=250)
    )

    movement = inventory.list_history(item.id)[0]
    assert (item.code, item.unit, item.quantity) == ("ST-0001", "個", 4)
    assert (movement.reason, movement.delta, movement.unit_price, movement.staff_id) == (
        Reason.ADJUST,
        4,
        250,
        2,
    )
    assert movement.moved_at == "2026-01-15 12:00:00"
    assert inventory.create_item(_new_item(name="履歴なし")).quantity == 0


@pytest.mark.parametrize("staff_id", [None, 3])
def test_create_item_rejects_missing_or_inactive_initial_staff_atomically(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    staff_id: int | None,
) -> None:
    with pytest.raises(ValidationError if staff_id is None else InactiveMasterError):
        inventory.create_item(_new_item(initial_quantity=2, initial_staff_id=staff_id))

    assert seeded_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT next_seq FROM categories WHERE id = 2").fetchone()[0] == 1


def test_category_code_sequence_is_not_reused_after_item_moves(
    inventory: InventoryService,
    master: MasterService,
) -> None:
    original = inventory.create_item(_new_item())
    inventory.update_item(_update_item(original, category_id=1))
    with pytest.raises(MasterInUseError):
        master.delete_category(2)

    next_item = inventory.create_item(_new_item(name="次の品目"))
    assert (original.code, next_item.code) == ("ST-0001", "ST-0002")


def test_update_item_keeps_identity_and_allows_unchanged_inactive_masters(
    inventory: InventoryService,
    master: MasterService,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=3, initial_staff_id=1))
    master.deactivate_client(1)
    master.deactivate_purchaser(1)

    updated = inventory.update_item(_update_item(item, name="更新後"))
    assert (updated.code, updated.quantity, updated.unit) == (item.code, 3, "個")
    with pytest.raises(InactiveMasterError):
        inventory.update_item(_update_item(updated, client_id=3))
    with pytest.raises(InactiveMasterError):
        inventory.update_item(_update_item(updated, purchaser_id=3))
    with pytest.raises(ValidationError):
        inventory.update_item(_update_item(updated, purchaser_id=0))

    history = inventory.list_history(item.id)
    assert (history[0].client_id, history[0].purchaser_id) == (1, 1)
    with pytest.raises(InactiveMasterError):
        inventory.create_item(_new_item(purchaser_id=3))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (" https://example.test/path ", "https://example.test/path"),
        ("http://example.test", "http://example.test"),
        ("", None),
    ],
)
def test_create_item_normalizes_allowed_purchase_urls(
    inventory: InventoryService, url: str, expected: str | None
) -> None:
    assert inventory.create_item(_new_item(purchase_url=url)).purchase_url == expected


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///tmp/a", "ftp://example.test", "https:"]
)
def test_create_item_rejects_unsafe_purchase_urls(inventory: InventoryService, url: str) -> None:
    with pytest.raises(ValidationError):
        inventory.create_item(_new_item(purchase_url=url))


def test_stock_operations_record_reasons_prices_and_quantity(inventory: InventoryService) -> None:
    item = inventory.create_item(_new_item(initial_quantity=10, initial_staff_id=1))
    inbound = inventory.receive(item.id, 2, 4, unit_price=120, update_reference_price=True)
    outbound = inventory.issue(item.id, 2, 2, "会議室", note="利用")
    returned = inventory.return_to_supplier(item.id, 2, 1)
    disposed = inventory.dispose(item.id, 2, 1)
    stocktake = inventory.stocktake(item.id, 2, 12)

    assert [(row.reason, row.delta) for row in inventory.list_history(item.id)] == [
        (Reason.ADJUST, 10),
        (Reason.IN, 4),
        (Reason.OUT, -2),
        (Reason.RETURN, -1),
        (Reason.DISPOSE, -1),
        (Reason.ADJUST, 2),
    ]
    assert (inbound.unit_price, outbound.unit_price, outbound.used_for) == (120, None, "会議室")
    assert outbound.cost_amount == 200
    assert (returned.unit_price, disposed.unit_price, stocktake.unit_price) == (None, None, 120)
    assert inventory.get_item(item.id).quantity == 12  # type: ignore[union-attr]


def test_stock_operations_validate_active_item_staff_and_negative_stock(
    inventory: InventoryService,
    master: MasterService,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=2, initial_staff_id=1))
    with pytest.raises(NegativeStockError):
        inventory.issue(item.id, 1, 3, "用途")
    with pytest.raises(ValidationError):
        inventory.issue(item.id, 1, 1, " ")
    with pytest.raises(InactiveMasterError):
        inventory.receive(item.id, 3, 1)

    master.deactivate_staff(1)
    master.deactivate_client(1)
    master.deactivate_purchaser(1)
    assert inventory.receive(item.id, 2, 1).delta == 1
    master.deactivate_staff(2)
    inventory.deactivate_item(item.id)
    with pytest.raises(InactiveItemError):
        inventory.dispose(item.id, 1, 1)


def test_reversing_receipt_does_not_restore_reference_price(inventory: InventoryService) -> None:
    item = inventory.create_item(_new_item(initial_quantity=2, initial_staff_id=1))
    receipt = inventory.receive(item.id, 1, 3, unit_price=250, update_reference_price=True)
    assert inventory.get_item(item.id).reference_price == 250  # type: ignore[union-attr]

    reversal = inventory.reverse(receipt.id, 2)
    assert reversal.unit_price == receipt.unit_price == 250
    assert inventory.get_item(item.id).reference_price == 250  # type: ignore[union-attr]
    inventory.receive(item.id, 1, 1, unit_price=300, update_reference_price=True)
    inventory.reverse(inventory.list_history(item.id)[-1].id, 2)
    assert inventory.get_item(item.id).reference_price == 300  # type: ignore[union-attr]


def test_reverse_rejects_all_blocked_cases(inventory: InventoryService) -> None:
    item = inventory.create_item(_new_item(initial_quantity=3, initial_staff_id=1))
    received = inventory.receive(item.id, 1, 1)
    reversed_row = inventory.reverse(received.id, 2)
    with pytest.raises(AlreadyReversedError):
        inventory.reverse(received.id, 2)
    with pytest.raises(ReversalNotAllowedError):
        inventory.reverse(reversed_row.id, 2)

    zero_adjustment = inventory.stocktake(item.id, 1, 3)
    with pytest.raises(ReversalNotAllowedError):
        inventory.reverse(zero_adjustment.id, 2)

    consumed_item = inventory.create_item(_new_item(name="消費品"))
    consumed_receipt = inventory.receive(consumed_item.id, 1, 2)
    inventory.issue(consumed_item.id, 1, 2, "利用")
    with pytest.raises(ReversalNotAllowedError):
        inventory.reverse(consumed_receipt.id, 2)

    inactive_item = inventory.create_item(
        _new_item(name="廃止品", initial_quantity=1, initial_staff_id=1)
    )
    inactive_movement = inventory.receive(inactive_item.id, 1, 1)
    inventory.deactivate_item(inactive_item.id)
    with pytest.raises(InactiveItemError):
        inventory.reverse(inactive_movement.id, 2)


def test_reverse_initial_adjustment_uses_original_attribution_after_master_changes(
    inventory: InventoryService,
    master: MasterService,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=5, initial_staff_id=1))
    original = inventory.list_history(item.id)[0]
    master.deactivate_client(1)
    master.deactivate_purchaser(1)
    master.deactivate_staff(1)
    inventory.update_item(_update_item(item, client_id=2, purchaser_id=2))

    reversal = inventory.reverse(original.id, 2)
    assert (reversal.delta, reversal.reason, reversal.client_id, reversal.purchaser_id) == (
        -5,
        Reason.ADJUST,
        1,
        1,
    )
    assert inventory.get_item(item.id).quantity == 0  # type: ignore[union-attr]


def test_reverse_requires_active_operator(
    inventory: InventoryService, master: MasterService
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=1, initial_staff_id=1))
    movement = inventory.list_history(item.id)[0]
    master.deactivate_staff(2)
    with pytest.raises(InactiveMasterError):
        inventory.reverse(movement.id, 2)


def test_inventory_reads_can_run_inside_callers_transaction(inventory: InventoryService) -> None:
    item = inventory.create_item(_new_item())
    with transaction(inventory.conn):
        assert inventory.get_item(item.id) is not None
        assert inventory.list_history(item.id) == []
        assert inventory.conn.in_transaction


def test_history_insert_and_quantity_update_roll_back_together(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=4, initial_staff_id=1))

    def fail_quantity_update(item_id: int, delta: int) -> None:
        raise RuntimeError("quantity update failed")

    monkeypatch.setattr(inventory.items, "add_quantity", fail_quantity_update)
    with pytest.raises(RuntimeError, match="quantity update failed"):
        inventory.receive(item.id, 1, 2)

    assert inventory.get_item(item.id).quantity == 4  # type: ignore[union-attr]
    assert seeded_conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 1


def test_commit_failure_rolls_back_movement_and_quantity() -> None:
    class CommitFailConnection(sqlite3.Connection):
        fail_next_commit = False

        def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
            if self.fail_next_commit and sql == "COMMIT":
                self.fail_next_commit = False
                raise sqlite3.OperationalError("commit failed")
            return super().execute(sql, parameters)  # type: ignore[arg-type]

    conn = sqlite3.connect(":memory:", autocommit=True, factory=CommitFailConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    create_schema(conn)
    conn.executemany("INSERT INTO clients (id, name) VALUES (?, ?)", [(1, "総務")])
    conn.executemany("INSERT INTO purchasers (id, name) VALUES (?, ?)", [(1, "本部")])
    conn.executemany("INSERT INTO staff (id, name) VALUES (?, ?)", [(1, "担当")])
    conn.executemany(
        "INSERT INTO categories (id, parent_id, name, code_prefix) VALUES (?, ?, ?, ?)",
        [(1, None, "備品", "TO"), (2, 1, "文具", "ST")],
    )
    service = InventoryService(
        conn,
        clock=lambda: datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    )
    try:
        item = service.create_item(
            _new_item(initial_quantity=3, initial_staff_id=1, location_id=None)
        )
        conn.fail_next_commit = True
        with pytest.raises(sqlite3.OperationalError, match="commit failed"):
            service.receive(item.id, 1, 2)

        assert service.get_item(item.id).quantity == 3  # type: ignore[union-attr]
        assert len(service.list_history(item.id)) == 1
        assert service.aggregates.get()["inbound_quantity"] == 0
        assert not conn.in_transaction
    finally:
        conn.close()


def test_item_filter_is_forwarded_to_repository(inventory: InventoryService) -> None:
    inventory.create_item(_new_item(name="保管品"))
    assert [row.name for row in inventory.list_items(ItemFilter(text="保管"))] == ["保管品"]


def test_service_accepts_numeric_limits_and_rejects_values_above_them(
    inventory: InventoryService,
) -> None:
    item = inventory.create_item(
        _new_item(
            initial_quantity=MAX_STOCK_QUANTITY,
            initial_staff_id=1,
            reorder_threshold=MAX_STOCK_QUANTITY,
            reorder_quantity=MAX_STOCK_QUANTITY,
            reference_price=MAX_UNIT_PRICE,
        )
    )
    assert item.quantity == MAX_STOCK_QUANTITY
    with pytest.raises(ValidationError, match="初期数量"):
        inventory.create_item(_new_item(name="超過品", initial_quantity=MAX_STOCK_QUANTITY + 1))
    with pytest.raises(ValidationError, match="参考価格"):
        inventory.update_item(_update_item(item, reference_price=MAX_UNIT_PRICE + 1))
    with pytest.raises(ValidationError, match="数量"):
        inventory.receive(item.id, 1, MAX_STOCK_QUANTITY + 1)
    with pytest.raises(ValidationError, match="在庫数が上限"):
        inventory.receive(item.id, 1, 1)


def test_issue_amount_limit_is_exact_and_failure_is_atomic(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    exact_item = inventory.create_item(
        _new_item(
            name="上限品",
            initial_quantity=MAX_STOCK_QUANTITY,
            initial_staff_id=1,
            reference_price=10_000,
        )
    )
    before = inventory.aggregates.get()
    movement = inventory.issue(exact_item.id, 1, 10_000, "用途")
    assert movement.unit_price is None
    assert movement.cost_amount == MAX_MOVEMENT_AMOUNT
    assert inventory.aggregates.get()["expenditure"] == before["expenditure"] + MAX_MOVEMENT_AMOUNT

    disposed_item = inventory.create_item(
        _new_item(
            name="超過廃棄金額品",
            initial_quantity=10_000,
            initial_staff_id=1,
            reference_price=10_001,
        )
    )
    with pytest.raises(ValidationError, match="1 操作の金額"):
        inventory.dispose(disposed_item.id, 1, 10_000)

    rejected_item = inventory.create_item(
        _new_item(
            name="超過金額品",
            initial_quantity=MAX_STOCK_QUANTITY,
            initial_staff_id=1,
            reference_price=10_001,
        )
    )
    quantity_before = inventory.get_item(rejected_item.id).quantity  # type: ignore[union-attr]
    movement_count = seeded_conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
    aggregates_before = inventory.aggregates.get()
    with pytest.raises(ValidationError, match="1 操作の金額"):
        inventory.issue(rejected_item.id, 1, 10_000, "用途")
    assert inventory.get_item(rejected_item.id).quantity == quantity_before  # type: ignore[union-attr]
    assert (
        seeded_conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == movement_count
    )
    assert inventory.aggregates.get() == aggregates_before


def test_issue_amount_limit_uses_fifo_cost_not_reference_price(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(_new_item(name="ロット価格", reference_price=0))
    inventory.receive(item.id, 1, 10_000, unit_price=10_001)
    before = _snapshot(seeded_conn)

    with pytest.raises(ValidationError, match="1 操作の金額"):
        inventory.issue(item.id, 1, 10_000, "用途")

    assert _snapshot(seeded_conn) == before


def test_amount_limit_does_not_apply_to_initial_receipt_return_or_stocktake(
    inventory: InventoryService,
) -> None:
    initial_item = inventory.create_item(
        _new_item(
            name="初期数量",
            initial_quantity=10_000,
            initial_staff_id=1,
            reference_price=10_001,
        )
    )
    receipt_item = inventory.create_item(_new_item(name="入庫", reference_price=10_001))
    return_item = inventory.create_item(
        _new_item(
            name="返品",
            initial_quantity=10_000,
            initial_staff_id=1,
            reference_price=10_001,
        )
    )
    stocktake_item = inventory.create_item(_new_item(name="棚卸", reference_price=10_001))

    assert inventory.get_item(initial_item.id).quantity == 10_000  # type: ignore[union-attr]
    assert inventory.receive(receipt_item.id, 1, 10_000).unit_price == 10_001
    assert inventory.return_to_supplier(return_item.id, 1, 10_000).delta == -10_000
    assert inventory.stocktake(stocktake_item.id, 1, 100_000).delta == 100_000


def test_aggregate_limit_failure_rolls_back_stock_history_and_totals(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(_new_item())
    seeded_conn.execute(
        "UPDATE total_aggregates SET inbound_quantity = ? WHERE id = 1",
        (MAX_AGGREGATE_VALUE - 1,),
    )
    inventory.receive(item.id, 1, 1, unit_price=250, update_reference_price=True)
    before_history = len(inventory.list_history(item.id))
    with pytest.raises(ValidationError, match="集計値"):
        inventory.receive(item.id, 1, 1, unit_price=300, update_reference_price=True)
    assert inventory.get_item(item.id).quantity == 1  # type: ignore[union-attr]
    assert inventory.get_item(item.id).reference_price == 250  # type: ignore[union-attr]
    assert len(inventory.list_history(item.id)) == before_history
    assert inventory.aggregates.get()["inbound_quantity"] == MAX_AGGREGATE_VALUE


def test_reversal_block_reason_and_reverse_enforce_quantity_limit(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=1, initial_staff_id=1))
    movement = inventory.issue(item.id, 1, 1, "用途")
    seeded_conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (MAX_STOCK_QUANTITY, item.id))
    expected = "取り消し後の在庫数が上限(1,000,000)を超えるため実行できません"
    assert inventory.reversal_block_reason(movement.id) == expected
    with pytest.raises(ValidationError, match="取り消し後の在庫数が上限"):
        inventory.reverse(movement.id, 2)


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in (
            "categories",
            "items",
            "stock_movements",
            "stock_allocations",
            "total_aggregates",
        )
    }


def _run_operation(
    inventory: InventoryService, operation: str, item_id: int, quantity: Any
) -> StockMovement:
    if operation == "receive":
        return inventory.receive(item_id, 1, quantity)
    if operation == "issue":
        return inventory.issue(item_id, 1, quantity, "用途")
    if operation == "return":
        return inventory.return_to_supplier(item_id, 1, quantity)
    if operation == "dispose":
        return inventory.dispose(item_id, 1, quantity)
    return inventory.stocktake(item_id, 1, quantity)


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("initial_quantity", 0, True),
        ("initial_quantity", 1, True),
        ("initial_quantity", MAX_STOCK_QUANTITY - 1, True),
        ("initial_quantity", MAX_STOCK_QUANTITY, True),
        ("initial_quantity", -1, False),
        ("initial_quantity", MAX_STOCK_QUANTITY + 1, False),
        ("reorder_threshold", 0, True),
        ("reorder_threshold", MAX_STOCK_QUANTITY, True),
        ("reorder_threshold", -1, False),
        ("reorder_threshold", MAX_STOCK_QUANTITY + 1, False),
        ("reorder_quantity", None, True),
        ("reorder_quantity", 1, True),
        ("reorder_quantity", MAX_STOCK_QUANTITY, True),
        ("reorder_quantity", 0, False),
        ("reorder_quantity", MAX_STOCK_QUANTITY + 1, False),
        ("reference_price", None, True),
        ("reference_price", 0, True),
        ("reference_price", MAX_UNIT_PRICE, True),
        ("reference_price", -1, False),
        ("reference_price", MAX_UNIT_PRICE + 1, False),
    ],
)
def test_create_item_numeric_boundaries(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    field: str,
    value: int | None,
    accepted: bool,
) -> None:
    kwargs: dict[str, object] = {field: value}
    if field == "initial_quantity":
        kwargs["initial_staff_id"] = 1
    before = _snapshot(seeded_conn)

    if accepted:
        item = inventory.create_item(_new_item(**kwargs))
        assert getattr(item, "quantity" if field == "initial_quantity" else field) == value
    else:
        with pytest.raises(ValidationError):
            inventory.create_item(_new_item(**kwargs))
        assert _snapshot(seeded_conn) == before


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("reorder_threshold", 0, True),
        ("reorder_threshold", MAX_STOCK_QUANTITY, True),
        ("reorder_threshold", MAX_STOCK_QUANTITY + 1, False),
        ("reorder_quantity", None, True),
        ("reorder_quantity", MAX_STOCK_QUANTITY, True),
        ("reorder_quantity", 0, False),
        ("reorder_quantity", MAX_STOCK_QUANTITY + 1, False),
        ("reference_price", None, True),
        ("reference_price", 0, True),
        ("reference_price", MAX_UNIT_PRICE, True),
        ("reference_price", -1, False),
        ("reference_price", MAX_UNIT_PRICE + 1, False),
    ],
)
def test_update_item_numeric_boundaries(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    field: str,
    value: int | None,
    accepted: bool,
) -> None:
    item = inventory.create_item(_new_item())
    before = _snapshot(seeded_conn)

    if accepted:
        assert getattr(inventory.update_item(_update_item(item, **{field: value})), field) == value
    else:
        with pytest.raises(ValidationError):
            inventory.update_item(_update_item(item, **{field: value}))
        assert _snapshot(seeded_conn) == before


@pytest.mark.parametrize("bad", [True, 1.5, "1", None])
def test_stock_operations_reject_non_integer_quantities(
    inventory: InventoryService, seeded_conn: sqlite3.Connection, bad: Any
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=5, initial_staff_id=1))
    before = _snapshot(seeded_conn)

    for operation in ("receive", "issue", "return", "dispose", "stocktake"):
        with pytest.raises(ValidationError):
            _run_operation(inventory, operation, item.id, bad)
    for unit_price in (True, 1.5, "1"):
        with pytest.raises(ValidationError):
            inventory.receive(item.id, 1, 1, unit_price=unit_price)  # type: ignore[arg-type]

    assert _snapshot(seeded_conn) == before


@pytest.mark.parametrize(
    ("operation", "quantity"),
    [
        *[
            (name, quantity)
            for name in ("receive", "issue", "return", "dispose")
            for quantity in (0, -1, MAX_STOCK_QUANTITY + 1)
        ],
        ("stocktake", -1),
        ("stocktake", MAX_STOCK_QUANTITY + 1),
    ],
)
def test_stock_operations_reject_out_of_range_quantities_without_changes(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    operation: str,
    quantity: int,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=5, initial_staff_id=1))
    before = _snapshot(seeded_conn)

    with pytest.raises(ValidationError):
        _run_operation(inventory, operation, item.id, quantity)

    assert _snapshot(seeded_conn) == before


def test_stock_operations_accept_quantities_up_to_the_limit(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    received = inventory.create_item(_new_item(name="入庫"))
    assert _run_operation(inventory, "receive", received.id, MAX_STOCK_QUANTITY).delta == (
        MAX_STOCK_QUANTITY
    )
    stepwise = inventory.create_item(_new_item(name="段階入庫"))
    inventory.receive(stepwise.id, 1, MAX_STOCK_QUANTITY - 1)
    inventory.receive(stepwise.id, 1, 1)
    assert inventory.get_item(stepwise.id).quantity == MAX_STOCK_QUANTITY  # type: ignore[union-attr]

    for operation in ("issue", "return", "dispose"):
        item = inventory.create_item(
            _new_item(
                name=operation,
                initial_quantity=MAX_STOCK_QUANTITY,
                initial_staff_id=1,
                reference_price=None,
            )
        )
        movement = _run_operation(inventory, operation, item.id, MAX_STOCK_QUANTITY)
        assert movement.delta == -MAX_STOCK_QUANTITY
        assert inventory.get_item(item.id).quantity == 0  # type: ignore[union-attr]

    counted = inventory.create_item(_new_item(name="棚卸"))
    assert inventory.stocktake(counted.id, 1, MAX_STOCK_QUANTITY).delta == MAX_STOCK_QUANTITY
    assert inventory.stocktake(counted.id, 1, 0).delta == -MAX_STOCK_QUANTITY
    assert inventory.aggregates.get() == compute_aggregates(seeded_conn)


def test_receive_unit_price_boundaries_and_none_versus_zero(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(_new_item(reference_price=100))
    assert inventory.receive(item.id, 1, 1, unit_price=MAX_UNIT_PRICE).unit_price == MAX_UNIT_PRICE
    assert inventory.receive(item.id, 1, 1, unit_price=0).unit_price == 0
    assert inventory.receive(item.id, 1, 1).unit_price == 100

    before = _snapshot(seeded_conn)
    for bad in (MAX_UNIT_PRICE + 1, -1):
        with pytest.raises(ValidationError):
            inventory.receive(item.id, 1, 1, unit_price=bad, update_reference_price=True)
    assert _snapshot(seeded_conn) == before

    inventory.receive(item.id, 1, 1, unit_price=0, update_reference_price=True)
    assert inventory.get_item(item.id).reference_price == 0  # type: ignore[union-attr]


def test_applied_reference_price_is_validated_when_unit_price_is_omitted(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=5, initial_staff_id=1, reference_price=100)
    )
    with unchecked_constraints(seeded_conn):
        seeded_conn.execute(
            "UPDATE items SET reference_price = ? WHERE id = ?", (MAX_UNIT_PRICE + 1, item.id)
        )
    before = _snapshot(seeded_conn)

    with pytest.raises(ValidationError, match="適用単価"):
        inventory.receive(item.id, 1, 1)
    with pytest.raises(ValidationError, match="適用単価"):
        inventory.stocktake(item.id, 1, 6)
    reduced = inventory.stocktake(item.id, 1, 3)
    assert reduced.unit_price is None
    assert len(inventory.list_history(item.id)) == len(before["stock_movements"]) + 1
    assert inventory.get_item(item.id).quantity == 3  # type: ignore[union-attr]

    assert inventory.receive(item.id, 1, 1, unit_price=10).unit_price == 10


def test_amount_limit_is_exact_for_dispose_none_zero_and_reversal(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    disposed_item = inventory.create_item(
        _new_item(
            name="廃棄上限",
            initial_quantity=10_000,
            initial_staff_id=1,
            reference_price=10_000,
        )
    )
    disposal = inventory.dispose(disposed_item.id, 1, 10_000)
    assert disposal.unit_price is None
    assert disposal.cost_amount == MAX_MOVEMENT_AMOUNT
    assert inventory.aggregates.get()["disposal_amount"] == MAX_MOVEMENT_AMOUNT
    inventory.reverse(disposal.id, 2)
    assert inventory.aggregates.get()["disposal_amount"] == 0

    unpriced = inventory.create_item(
        _new_item(
            name="単価なし",
            initial_quantity=MAX_STOCK_QUANTITY,
            initial_staff_id=1,
            reference_price=None,
        )
    )
    zero_priced = inventory.create_item(
        _new_item(
            name="0円",
            initial_quantity=MAX_STOCK_QUANTITY,
            initial_staff_id=1,
            reference_price=0,
        )
    )
    assert inventory.issue(unpriced.id, 1, MAX_STOCK_QUANTITY, "用途").unit_price is None
    zero_disposal = inventory.dispose(zero_priced.id, 1, MAX_STOCK_QUANTITY)
    assert (zero_disposal.unit_price, zero_disposal.cost_amount) == (None, 0)
    assert inventory.aggregates.get() == compute_aggregates(seeded_conn)
    assert inventory.aggregates.get()["expenditure"] == 0


def test_fifo_estimate_allocation_and_reversal_restore_the_same_lots(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(
        _new_item(name="FIFO", initial_quantity=2, initial_staff_id=1, reference_price=10)
    )
    second_lot = inventory.receive(item.id, 1, 3, unit_price=20)

    assert inventory.estimate_outflow(item.id, 4).cost_amount == 60
    movement = inventory.issue(item.id, 1, 4, "用途")
    first_lot = inventory.list_history(item.id)[0]
    allocations = seeded_conn.execute(
        "SELECT lot_id, quantity FROM stock_allocations WHERE movement_id = ? ORDER BY lot_id",
        (movement.id,),
    ).fetchall()
    assert [(row["lot_id"], row["quantity"]) for row in allocations] == [
        (first_lot.id, 2),
        (second_lot.id, 2),
    ]
    assert (movement.unit_price, movement.cost_amount) == (None, 60)

    reversal = inventory.reverse(movement.id, 2)
    restored = seeded_conn.execute(
        "SELECT lot_id, quantity FROM stock_allocations WHERE movement_id = ? ORDER BY lot_id",
        (reversal.id,),
    ).fetchall()
    assert [(row["lot_id"], row["quantity"]) for row in restored] == [
        (first_lot.id, -2),
        (second_lot.id, -2),
    ]
    assert (reversal.unit_price, reversal.cost_amount) == (None, -60)
    assert inventory.estimate_outflow(item.id, 5).cost_amount == 80
    assert inventory.aggregates.get()["expenditure"] == 0
    assert inventory.aggregates.get() == compute_aggregates(seeded_conn)
    replay = replay_allocations(seeded_conn)
    assert replay.violations == ()
    assert replay.cost_amounts[movement.id] == 60
    assert replay.cost_amounts[reversal.id] == -60
    assert replay.allocations[movement.id] == tuple(
        (int(row["lot_id"]), int(row["quantity"])) for row in allocations
    )


def test_fifo_unpriced_lots_and_nonexpense_decreases(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(
        _new_item(name="単価不明", initial_quantity=2, initial_staff_id=1, reference_price=None)
    )
    inventory.receive(item.id, 1, 2, unit_price=0)

    estimate = inventory.estimate_outflow(item.id, 3)
    assert (estimate.cost_amount, estimate.unpriced_quantity) == (0, 2)
    issued = inventory.issue(item.id, 1, 3, "用途")
    assert (issued.cost_amount, issued.unit_price) == (0, None)
    assert inventory.aggregates.get()["expenditure"] == 0
    inventory.reverse(issued.id, 2)

    returned = inventory.return_to_supplier(item.id, 1, 1)
    counted = inventory.stocktake(item.id, 1, 2)
    assert (returned.unit_price, counted.unit_price) == (None, None)
    assert (returned.cost_amount, counted.cost_amount) == (None, None)
    assert inventory.aggregates.get() == compute_aggregates(seeded_conn)


def test_all_decrease_reversals_restore_the_original_allocations(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(
        _new_item(name="取消引当", initial_quantity=10, initial_staff_id=1, reference_price=10)
    )
    inventory.receive(item.id, 1, 5, unit_price=20)
    movements = [
        inventory.issue(item.id, 1, 2, "用途"),
        inventory.dispose(item.id, 1, 2),
        inventory.return_to_supplier(item.id, 1, 2),
        inventory.stocktake(item.id, 1, 7),
    ]

    for movement in reversed(movements):
        original_allocations = inventory.allocations.list_for_movement(movement.id)
        reversal = inventory.reverse(movement.id, 2)
        reversal_allocations = inventory.allocations.list_for_movement(reversal.id)
        assert reversal_allocations == tuple(
            Allocation(allocation.lot_id, -allocation.quantity)
            for allocation in original_allocations
        )
        assert reversal.unit_price == movement.unit_price
        assert reversal.cost_amount == (
            None if movement.cost_amount is None else -movement.cost_amount
        )

    replay = replay_allocations(seeded_conn)
    assert replay.violations == ()
    assert inventory.get_item(item.id).quantity == 15  # type: ignore[union-attr]
    assert inventory.aggregates.get() == compute_aggregates(seeded_conn)


def test_consumed_lot_can_be_reversed_after_undoing_the_consumption(
    inventory: InventoryService,
) -> None:
    item = inventory.create_item(
        _new_item(name="部分消費", initial_quantity=2, initial_staff_id=1, reference_price=10)
    )
    receipt = inventory.receive(item.id, 1, 2, unit_price=20)
    issue = inventory.issue(item.id, 1, 3, "用途")
    reason = (
        "この入庫(在庫増加)はすでに出庫・廃棄・返品・棚卸減少で消費されているため取り消せません"
    )

    assert inventory.reversal_block_reason(receipt.id) == reason
    with pytest.raises(ReversalNotAllowedError, match="すでに出庫"):
        inventory.reverse(receipt.id, 2)

    issue_reversal = inventory.reverse(issue.id, 2)
    receipt_reversal = inventory.reverse(receipt.id, 2)
    assert issue_reversal.cost_amount == -40
    assert receipt_reversal.unit_price == receipt.unit_price == 20
    updated_item = inventory.get_item(item.id)
    assert updated_item is not None
    assert updated_item.quantity == 2


def test_same_second_and_later_inventory_timestamps_are_allowed(
    inventory: InventoryService,
) -> None:
    item = inventory.create_item(_new_item(name="同秒"))
    first = inventory.receive(item.id, 1, 1, unit_price=10)
    second = inventory.receive(item.id, 1, 1, unit_price=20)
    issue = inventory.issue(item.id, 1, 1, "同秒")
    inventory.reverse(issue.id, 2)
    assert first.moved_at == second.moved_at == issue.moved_at

    inventory.clock = lambda: datetime(2026, 1, 15, 12, 0, 1, tzinfo=UTC)
    later = inventory.receive(item.id, 1, 1, unit_price=30)
    assert later.moved_at == "2026-01-15 12:00:01"


def test_inventory_operations_reject_clock_rollback_atomically(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=3, initial_staff_id=1))
    issue = inventory.issue(item.id, 1, 1, "用途")
    before = _snapshot(seeded_conn)
    inventory.clock = lambda: datetime(2026, 1, 15, 11, 59, tzinfo=UTC)

    with pytest.raises(ValidationError, match="最新履歴より前"):
        inventory.receive(item.id, 1, 1, unit_price=500, update_reference_price=True)
    with pytest.raises(ValidationError, match="最新履歴より前"):
        inventory.reverse(issue.id, 2)
    assert _snapshot(seeded_conn) == before


def test_zero_difference_stocktake_is_recorded_without_changing_totals(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=3, initial_staff_id=1, reference_price=100)
    )
    before = inventory.aggregates.get()

    movement = inventory.stocktake(item.id, 1, 3)

    assert (movement.delta, movement.unit_price) == (0, None)
    assert inventory.aggregates.get() == before
    assert inventory.reversal_block_reason(movement.id) == "差分 0 の棚卸履歴は取り消せません"


@pytest.mark.parametrize(
    ("column", "reference_price", "initial", "within", "exceeding"),
    [
        ("inbound_quantity", None, 0, ("receive", 100), ("receive", 1)),
        ("outbound_quantity", None, 200, ("issue", 100), ("issue", 1)),
        ("disposed_quantity", None, 200, ("dispose", 100), ("dispose", 1)),
        ("expenditure", 2, 200, ("issue", 50), ("issue", 1)),
        ("disposal_amount", 2, 200, ("dispose", 50), ("dispose", 1)),
    ],
)
def test_each_aggregate_is_limited_independently_and_failure_is_atomic(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    reference_price: int | None,
    initial: int,
    within: tuple[str, int],
    exceeding: tuple[str, int],
) -> None:
    monkeypatch.setattr(repositories, "MAX_AGGREGATE_VALUE", 100)
    item = inventory.create_item(
        _new_item(
            initial_quantity=initial,
            initial_staff_id=1 if initial else None,
            reference_price=reference_price,
        )
    )

    _run_operation(inventory, within[0], item.id, within[1])
    assert inventory.aggregates.get()[column] == 100
    before = _snapshot(seeded_conn)
    with pytest.raises(ValidationError, match=column):
        _run_operation(inventory, exceeding[0], item.id, exceeding[1])

    assert _snapshot(seeded_conn) == before


def test_expenditure_and_disposal_amount_are_not_added_together(
    inventory: InventoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repositories, "MAX_AGGREGATE_VALUE", 100)
    item = inventory.create_item(
        _new_item(initial_quantity=200, initial_staff_id=1, reference_price=2)
    )

    inventory.issue(item.id, 1, 50, "用途")
    inventory.dispose(item.id, 1, 50)

    totals = inventory.aggregates.get()
    assert (totals["expenditure"], totals["disposal_amount"]) == (100, 100)


def test_reverse_fails_atomically_when_totals_would_become_negative(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=10, initial_staff_id=1, reference_price=100)
    )
    movement = inventory.issue(item.id, 1, 2, "用途")
    seeded_conn.execute("UPDATE total_aggregates SET outbound_quantity = 0, expenditure = 0")
    before = _snapshot(seeded_conn)

    with pytest.raises(ValidationError, match="outbound_quantity"):
        inventory.reverse(movement.id, 2)

    assert _snapshot(seeded_conn) == before


def test_price_edit_changes_neither_totals_nor_past_history(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=10, initial_staff_id=1, reference_price=100)
    )
    issue = inventory.issue(item.id, 1, 2, "用途")
    inventory.dispose(item.id, 1, 1)
    totals_before = inventory.aggregates.get()
    history_before = [(row.id, row.unit_price) for row in inventory.list_history(item.id)]

    inventory.update_item(_update_item(item, reference_price=999))

    assert inventory.aggregates.get() == totals_before
    assert [(row.id, row.unit_price) for row in inventory.list_history(item.id)] == history_before
    assert inventory.reverse(issue.id, 2).unit_price is None
    later_issue = inventory.issue(item.id, 1, 1, "改定後")
    assert (later_issue.unit_price, later_issue.cost_amount) == (None, 100)
    assert inventory.aggregates.get() == compute_aggregates(seeded_conn)


@pytest.mark.parametrize(
    "operation",
    ["create_item", "receive_with_price", "issue", "dispose", "return", "stocktake", "reverse"],
)
def test_failure_after_partial_writes_rolls_back_every_operation(
    inventory: InventoryService,
    seeded_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=10, initial_staff_id=1, reference_price=100)
    )
    issued = inventory.issue(item.id, 1, 2, "用途")
    actions: dict[str, Callable[[], object]] = {
        "create_item": lambda: inventory.create_item(
            _new_item(name="新規", initial_quantity=3, initial_staff_id=1)
        ),
        "receive_with_price": lambda: inventory.receive(
            item.id, 1, 1, unit_price=250, update_reference_price=True
        ),
        "issue": lambda: inventory.issue(item.id, 1, 1, "用途"),
        "dispose": lambda: inventory.dispose(item.id, 1, 1),
        "return": lambda: inventory.return_to_supplier(item.id, 1, 1),
        "stocktake": lambda: inventory.stocktake(item.id, 1, 4),
        "reverse": lambda: inventory.reverse(issued.id, 2),
    }
    before = _snapshot(seeded_conn)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected failure")

    # 履歴 INSERT・数量更新の後で失敗させる(品目登録は数量更新、それ以外は集計更新)
    target = inventory.items if operation == "create_item" else inventory.aggregates
    monkeypatch.setattr(target, "add_quantity" if operation == "create_item" else "apply", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        actions[operation]()

    assert not seeded_conn.in_transaction
    assert _snapshot(seeded_conn) == before


class _CommitFailConnection(sqlite3.Connection):
    fail_next_commit = False

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if self.fail_next_commit and sql == "COMMIT":
            self.fail_next_commit = False
            raise sqlite3.OperationalError("commit failed")
        return super().execute(sql, parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize("failure", ["amount-limit", "aggregate-limit", "injected", "commit"])
def test_failed_operations_leave_a_consistent_database_after_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "inventory.db"
    conn = sqlite3.connect(path, autocommit=True, factory=_CommitFailConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    create_schema(conn)
    master = MasterService(conn)
    staff = master.add_staff("担当")
    inventory = InventoryService(conn)
    item = inventory.create_item(
        NewItem(
            client_id=master.add_client("総務").id,
            purchaser_id=master.add_purchaser("本部").id,
            name="品目",
            category_id=master.add_category("備品", "TO").id,
            initial_quantity=10_000,
            initial_staff_id=staff.id,
            reference_price=10_001,
        )
    )
    inventory.receive(item.id, staff.id, 5)
    before = _snapshot(conn)
    try:
        if failure == "amount-limit":
            with pytest.raises(ValidationError, match="1 操作の金額"):
                inventory.issue(item.id, staff.id, 10_000, "用途")
        elif failure == "aggregate-limit":
            monkeypatch.setattr(repositories, "MAX_AGGREGATE_VALUE", 5)
            with pytest.raises(ValidationError, match="inbound_quantity"):
                inventory.receive(item.id, staff.id, 1)
        elif failure == "injected":

            def fail(*args: object, **kwargs: object) -> None:
                raise RuntimeError("injected failure")

            monkeypatch.setattr(inventory.aggregates, "apply", fail)
            with pytest.raises(RuntimeError, match="injected failure"):
                inventory.receive(item.id, staff.id, 1, unit_price=300, update_reference_price=True)
        else:
            assert isinstance(conn, _CommitFailConnection)
            conn.fail_next_commit = True
            with pytest.raises(sqlite3.OperationalError, match="commit failed"):
                inventory.receive(item.id, staff.id, 1, unit_price=300, update_reference_price=True)
        assert _snapshot(conn) == before
    finally:
        conn.close()

    reopened = connect(path)
    try:
        assert _snapshot(reopened) == before
        stored = {
            column: reopened.execute(f"SELECT {column} FROM total_aggregates").fetchone()[0]
            for column in AGGREGATE_COLUMNS
        }
        assert stored == compute_aggregates(reopened)
        assert BackupService().inspect_database(reopened, SCHEMA_VERSION) == []
    finally:
        reopened.close()


_CONSUMED_LOT_REASON = (
    "この入庫(在庫増加)はすでに出庫・廃棄・返品・棚卸減少で消費されているため取り消せません"
)


def _assert_fifo_consistent(inventory: InventoryService, conn: sqlite3.Connection) -> None:
    assert find_fifo_violations(conn) == []
    for item_id, quantity in conn.execute("SELECT id, quantity FROM items").fetchall():
        lots = inventory.allocations.list_available_lots(item_id)
        assert sum(lot.remaining_quantity for lot in lots) == quantity
    assert inventory.aggregates.get() == compute_aggregates(conn)


def _allocation_pairs(inventory: InventoryService, movement_id: int) -> list[tuple[int, int]]:
    return [
        (value.lot_id, value.quantity)
        for value in inventory.allocations.list_for_movement(movement_id)
    ]


def test_every_decreasing_operation_allocates_lots_in_fifo_order(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=5, initial_staff_id=1, reference_price=10)
    )
    lot_a = inventory.list_history(item.id)[0]
    lot_b = inventory.receive(item.id, 1, 5, unit_price=20)
    assert _allocation_pairs(inventory, lot_a.id) == []
    assert _allocation_pairs(inventory, lot_b.id) == []
    _assert_fifo_consistent(inventory, seeded_conn)

    issued = inventory.issue(item.id, 1, 3, "用途")
    assert _allocation_pairs(inventory, issued.id) == [(lot_a.id, 3)]
    _assert_fifo_consistent(inventory, seeded_conn)
    disposed = inventory.dispose(item.id, 1, 3)
    assert _allocation_pairs(inventory, disposed.id) == [(lot_a.id, 2), (lot_b.id, 1)]
    _assert_fifo_consistent(inventory, seeded_conn)
    returned = inventory.return_to_supplier(item.id, 1, 2)
    assert _allocation_pairs(inventory, returned.id) == [(lot_b.id, 2)]
    _assert_fifo_consistent(inventory, seeded_conn)
    counted = inventory.stocktake(item.id, 1, 1)
    assert counted.delta == -1
    assert _allocation_pairs(inventory, counted.id) == [(lot_b.id, 1)]
    _assert_fifo_consistent(inventory, seeded_conn)

    assert [m.unit_price for m in (issued, disposed, returned, counted)] == [None] * 4
    assert [m.cost_amount for m in (issued, disposed, returned, counted)] == [30, 40, None, None]
    totals = inventory.aggregates.get()
    assert (totals["inbound_quantity"], totals["outbound_quantity"], totals["expenditure"]) == (
        5,
        3,
        30,
    )
    assert (totals["disposed_quantity"], totals["disposal_amount"]) == (3, 40)
    assert [
        (lot.id, lot.remaining_quantity)
        for lot in inventory.allocations.list_available_lots(item.id)
    ] == [(lot_b.id, 1)]


def test_stocktake_increase_lot_uses_reference_price_and_blocks_reversal_when_consumed(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=1, initial_staff_id=1, reference_price=10)
    )
    initial = inventory.list_history(item.id)[0]
    inventory.update_item(_update_item(item, reference_price=30))
    increase = inventory.stocktake(item.id, 1, 3)
    assert increase.unit_price == 30

    issued = inventory.issue(item.id, 1, 3, "用途")
    assert issued.cost_amount == 10 + 2 * 30
    for lot in (initial, increase):
        assert inventory.reversal_block_reason(lot.id) == _CONSUMED_LOT_REASON
        with pytest.raises(ReversalNotAllowedError):
            inventory.reverse(lot.id, 2)

    inventory.reverse(issued.id, 2)
    inventory.reverse(increase.id, 2)
    inventory.reverse(initial.id, 2)
    assert inventory.get_item(item.id).quantity == 0  # type: ignore[union-attr]
    _assert_fifo_consistent(inventory, seeded_conn)


def test_priced_zero_and_unpriced_lots_are_mixed_by_fifo(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=2, initial_staff_id=1, reference_price=None)
    )
    unpriced_receipt = inventory.receive(item.id, 1, 2)
    inventory.receive(item.id, 1, 2, unit_price=0)
    inventory.receive(item.id, 1, 2, unit_price=5)
    assert unpriced_receipt.unit_price is None

    estimate = inventory.estimate_outflow(item.id, 7)
    assert (estimate.cost_amount, estimate.unpriced_quantity) == (5, 4)
    issued = inventory.issue(item.id, 1, 7, "用途")
    assert issued.cost_amount == 5
    assert inventory.aggregates.get()["expenditure"] == 5
    _assert_fifo_consistent(inventory, seeded_conn)


@pytest.mark.parametrize("operation", ["issue", "dispose"])
def test_fifo_cost_limit_is_exact_to_one_yen_across_lots(
    inventory: InventoryService, seeded_conn: sqlite3.Connection, operation: str
) -> None:
    def build(name: str, second_price: int) -> int:
        item = inventory.create_item(
            _new_item(name=name, initial_quantity=10, initial_staff_id=1, reference_price=9_999_999)
        )
        inventory.receive(item.id, 1, 1, unit_price=second_price)
        return item.id

    exact_id = build("ちょうど", 10)
    over_id = build("1円超過", 11)
    assert inventory.estimate_outflow(over_id, 11).cost_amount == MAX_MOVEMENT_AMOUNT + 1
    before = _snapshot(seeded_conn)

    with pytest.raises(ValidationError, match="1 操作の金額"):
        _run_operation(inventory, operation, over_id, 11)
    assert _snapshot(seeded_conn) == before

    assert _run_operation(inventory, operation, exact_id, 11).cost_amount == MAX_MOVEMENT_AMOUNT
    _assert_fifo_consistent(inventory, seeded_conn)


def test_reversed_lot_is_excluded_and_reversal_costs_keep_zero_and_null(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(
        _new_item(initial_quantity=5, initial_staff_id=1, reference_price=10)
    )
    initial = inventory.list_history(item.id)[0]
    receipt = inventory.receive(item.id, 1, 3, unit_price=20)
    lot_reversal = inventory.reverse(receipt.id, 2)
    assert (lot_reversal.cost_amount, lot_reversal.unit_price) == (None, 20)

    issued = inventory.issue(item.id, 1, 5, "用途")
    assert issued.cost_amount == 50
    assert _allocation_pairs(inventory, issued.id) == [(initial.id, 5)]
    assert inventory.allocations.list_available_lots(item.id) == []
    _assert_fifo_consistent(inventory, seeded_conn)

    free = inventory.create_item(
        _new_item(name="単価なし", initial_quantity=2, initial_staff_id=1, reference_price=None)
    )
    zero_issue = inventory.issue(free.id, 1, 2, "用途")
    zero_reversal = inventory.reverse(zero_issue.id, 2)
    assert zero_issue.cost_amount == 0
    assert zero_reversal.cost_amount is not None
    assert zero_reversal.cost_amount == 0
    _assert_fifo_consistent(inventory, seeded_conn)


def test_fifo_state_stays_consistent_through_mixed_operations_and_reversals(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    a = inventory.create_item(
        _new_item(name="A", initial_quantity=4, initial_staff_id=1, reference_price=10)
    )
    b = inventory.create_item(
        _new_item(name="B", initial_quantity=3, initial_staff_id=1, reference_price=7)
    )
    inventory.receive(a.id, 1, 4, unit_price=20)
    issue_a = inventory.issue(a.id, 1, 5, "用途")
    _assert_fifo_consistent(inventory, seeded_conn)
    dispose_b = inventory.dispose(b.id, 1, 2)
    count_a = inventory.stocktake(a.id, 1, 1)
    return_a = inventory.return_to_supplier(a.id, 1, 1)
    _assert_fifo_consistent(inventory, seeded_conn)
    inventory.stocktake(b.id, 1, 6)
    issue_b = inventory.issue(b.id, 1, 4, "用途")
    _assert_fifo_consistent(inventory, seeded_conn)

    for movement in (count_a, return_a, dispose_b, issue_b, issue_a):
        inventory.reverse(movement.id, 2)
        _assert_fifo_consistent(inventory, seeded_conn)
    assert inventory.get_item(a.id).quantity == 8  # type: ignore[union-attr]
    assert inventory.get_item(b.id).quantity == 8  # type: ignore[union-attr]


@pytest.mark.parametrize("operation", ["receive", "issue", "return", "dispose", "stocktake"])
def test_every_operation_rejects_clock_rollback_atomically(
    inventory: InventoryService, seeded_conn: sqlite3.Connection, operation: str
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=5, initial_staff_id=1))
    inventory.receive(item.id, 1, 1)
    before = _snapshot(seeded_conn)
    inventory.clock = lambda: datetime(2026, 1, 15, 11, 59, 59, tzinfo=UTC)

    with pytest.raises(ValidationError, match="最新履歴より前"):
        _run_operation(inventory, operation, item.id, 3 if operation == "stocktake" else 1)

    assert _snapshot(seeded_conn) == before
    assert not seeded_conn.in_transaction


def test_clock_rollback_check_includes_reversal_rows_and_is_per_item(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=5, initial_staff_id=1))
    first = inventory.issue(item.id, 1, 1, "用途")
    second = inventory.issue(item.id, 1, 1, "用途")
    inventory.clock = lambda: datetime(2026, 1, 15, 12, 0, 10, tzinfo=UTC)
    inventory.reverse(first.id, 2)
    before = _snapshot(seeded_conn)

    inventory.clock = lambda: datetime(2026, 1, 15, 12, 0, 5, tzinfo=UTC)
    with pytest.raises(ValidationError, match="最新履歴より前"):
        inventory.issue(item.id, 1, 1, "用途")
    with pytest.raises(ValidationError, match="最新履歴より前"):
        inventory.reverse(second.id, 2)
    assert _snapshot(seeded_conn) == before

    other = inventory.create_item(_new_item(name="別品目", initial_quantity=1, initial_staff_id=1))
    assert inventory.receive(other.id, 1, 1).moved_at == "2026-01-15 12:00:05"
    _assert_fifo_consistent(inventory, seeded_conn)


def test_estimate_outflow_validates_input_and_does_not_write(
    inventory: InventoryService, seeded_conn: sqlite3.Connection
) -> None:
    item = inventory.create_item(_new_item(initial_quantity=3, initial_staff_id=1))
    before = _snapshot(seeded_conn)

    with pytest.raises(NegativeStockError):
        inventory.estimate_outflow(item.id, 4)
    for bad_quantity in (0, -1, MAX_STOCK_QUANTITY + 1, True):
        with pytest.raises(ValidationError):
            inventory.estimate_outflow(item.id, bad_quantity)
    for bad_item_id in (0, 999):
        with pytest.raises(ValidationError):
            inventory.estimate_outflow(bad_item_id, 1)

    assert inventory.estimate_outflow(item.id, 3).cost_amount == 300
    assert _snapshot(seeded_conn) == before
    assert not seeded_conn.in_transaction


@pytest.mark.parametrize("operation", ["issue", "reverse_issue", "stocktake_decrease"])
def test_commit_failure_rolls_back_allocations_and_stays_consistent(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / "inventory.db"
    conn = sqlite3.connect(path, autocommit=True, factory=_CommitFailConnection)
    conn.execute("PRAGMA foreign_keys = ON")
    create_schema(conn)
    master = MasterService(conn)
    staff = master.add_staff("担当")
    inventory = InventoryService(conn)
    item = inventory.create_item(
        NewItem(
            client_id=master.add_client("総務").id,
            purchaser_id=master.add_purchaser("本部").id,
            name="品目",
            category_id=master.add_category("備品", "TO").id,
            initial_quantity=5,
            initial_staff_id=staff.id,
            reference_price=10,
        )
    )
    inventory.receive(item.id, staff.id, 5, unit_price=20)
    issued = inventory.issue(item.id, staff.id, 6, "用途")
    before = _snapshot(conn)
    actions: dict[str, Callable[[], object]] = {
        "issue": lambda: inventory.issue(item.id, staff.id, 1, "用途"),
        "reverse_issue": lambda: inventory.reverse(issued.id, staff.id),
        "stocktake_decrease": lambda: inventory.stocktake(item.id, staff.id, 2),
    }
    try:
        assert isinstance(conn, _CommitFailConnection)
        conn.fail_next_commit = True
        with pytest.raises(sqlite3.OperationalError, match="commit failed"):
            actions[operation]()
        assert _snapshot(conn) == before
        _assert_fifo_consistent(inventory, conn)
    finally:
        conn.close()

    reopened = connect(path)
    try:
        assert _snapshot(reopened) == before
        assert find_fifo_violations(reopened) == []
    finally:
        reopened.close()
