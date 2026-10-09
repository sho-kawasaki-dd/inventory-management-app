from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

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
    ItemFilter,
    ItemUpdate,
    NewItem,
    Reason,
)
from inventory_manager_mini.core.services import InventoryService, MasterService
from inventory_manager_mini.db.connection import transaction
from inventory_manager_mini.db.migrations import create_schema


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
    assert (inbound.unit_price, outbound.unit_price, outbound.used_for) == (120, 120, "会議室")
    assert (returned.unit_price, disposed.unit_price, stocktake.unit_price) == (120, 120, 120)
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
    with pytest.raises(NegativeStockError):
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
    assert movement.unit_price is not None
    assert abs(movement.delta) * movement.unit_price == MAX_MOVEMENT_AMOUNT
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
