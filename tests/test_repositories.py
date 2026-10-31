from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from importlib.resources import files
from typing import Any

import pytest

from inventory_manager_mini.core.errors import (
    AlreadyReversedError,
    MasterInUseError,
    NegativeStockError,
    ValidationError,
)
from inventory_manager_mini.core.models import (
    MAX_AGGREGATE_VALUE,
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    Allocation,
    ItemChoice,
    ItemFilter,
    ItemUpdate,
    MovementRow,
    Reason,
    StockMovement,
)
from inventory_manager_mini.db.connection import connect_memory
from inventory_manager_mini.db.integrity import AGGREGATE_COLUMNS
from inventory_manager_mini.db.repositories import (
    AllocationRepository,
    ItemRepository,
    MasterRepository,
    MovementRepository,
    SettingsRepository,
    StocktakeRepository,
    TotalAggregatesRepository,
)
from tests.conftest import unchecked_constraints


@pytest.fixture
def repository_conn() -> Iterator[sqlite3.Connection]:
    conn = connect_memory()
    conn.executescript(
        files("inventory_manager_mini.db").joinpath("schema.sql").read_text(encoding="utf-8")
    )
    conn.executemany(
        "INSERT INTO clients (id, name, is_active) VALUES (?, ?, ?)",
        [(1, "総務", 1), (2, "経理", 1), (3, "廃止クライアント", 0)],
    )
    conn.executemany(
        "INSERT INTO purchasers (id, name, is_active) VALUES (?, ?, ?)",
        [(1, "本部", 1), (2, "部門", 1), (3, "廃止発注主体", 0)],
    )
    conn.executemany(
        "INSERT INTO staff (id, name, is_active) VALUES (?, ?, ?)",
        [(1, "担当A", 1), (2, "担当B", 1), (3, "廃止担当", 0)],
    )
    conn.executemany(
        "INSERT INTO categories (id, parent_id, name, code_prefix, next_seq) "
        "VALUES (?, ?, ?, ?, ?)",
        [(1, None, "備品", "TO", 1), (2, 1, "文具", "ST", 1), (3, 1, "工具", "TL", 1)],
    )
    conn.executemany(
        "INSERT INTO locations (id, name) VALUES (?, ?)",
        [(1, "倉庫"), (2, "事務室")],
    )
    try:
        yield conn
    finally:
        conn.close()


def _insert_item(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    code: str | None = None,
    name: str = "品目",
    client_id: int = 1,
    purchaser_id: int = 1,
    category_id: int = 2,
    location_id: int | None = 1,
    quantity: int = 0,
    threshold: int = 0,
    active: int = 1,
    part_number: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO items
        (id, client_id, purchaser_id, code, name, category_id, location_id, quantity,
         reorder_threshold, manufacturer_part_number, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            client_id,
            purchaser_id,
            code or f"ST-{item_id:04d}",
            name,
            category_id,
            location_id,
            quantity,
            threshold,
            part_number,
            active,
        ),
    )


def _insert_movement(
    conn: sqlite3.Connection,
    *,
    movement_id: int,
    item_id: int = 1,
    reason: str = "in",
    delta: int = 3,
    client_id: int = 1,
    purchaser_id: int = 1,
    unit_price: int | None = 100,
    used_for: str | None = None,
    reversal_of: int | None = None,
    moved_at: str = "2026-01-01 00:00:00",
) -> None:
    cost_amount = 0 if reason in {"out", "dispose"} else None
    effective_unit_price = None if reason in {"out", "return", "dispose"} else unit_price
    conn.execute(
        """INSERT INTO stock_movements
        (id, item_id, client_id, purchaser_id, staff_id, reason, delta, unit_price, cost_amount,
         used_for, reversal_of, moved_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
        (
            movement_id,
            item_id,
            client_id,
            purchaser_id,
            reason,
            delta,
            effective_unit_price,
            cost_amount,
            used_for,
            reversal_of,
            moved_at,
        ),
    )


def _insert_repository_item(repo: ItemRepository, *, code: str, name: str = "新規") -> int:
    item = repo.insert(
        client_id=1,
        purchaser_id=1,
        code=code,
        name=name,
        category_id=2,
        location_id=1,
        reorder_threshold=2,
        reorder_quantity=5,
        purchase_url=None,
        supplier=None,
        manufacturer_part_number=None,
        application=None,
        reference_price=None,
        note=None,
    )
    return item.id


def test_item_repository_list_choices_orders_by_code_and_includes_inactive(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1, code="ST-0002", name="後", active=1)
    _insert_item(repository_conn, item_id=2, code="ST-0001", name="先", active=0)
    _insert_item(repository_conn, item_id=3, code="TL-0001", name="工具品", category_id=3)

    choices = ItemRepository(repository_conn).list_choices()

    assert [(c.id, c.code, c.name, c.is_active) for c in choices] == [
        (2, "ST-0001", "先", False),
        (1, "ST-0002", "後", True),
        (3, "TL-0001", "工具品", True),
    ]
    assert set(ItemChoice.__dataclass_fields__) == {"id", "code", "name", "is_active"}


def test_item_repository_allocates_sequences_without_reusing_codes(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = ItemRepository(repository_conn)
    assert repo.allocate_code(2) == "ST-0001"
    assert repo.allocate_code(2) == "ST-0002"
    repository_conn.execute("UPDATE categories SET next_seq = 10000 WHERE id = 2")
    assert repo.allocate_code(2) == "ST-10000"
    with pytest.raises(ValidationError):
        repo.allocate_code(999)


def test_item_repository_set_code_updates_code(repository_conn: sqlite3.Connection) -> None:
    repo = ItemRepository(repository_conn)
    _insert_item(repository_conn, item_id=1, code="ST-0001")

    repo.set_code(1, "TL-0001")

    item = repo.get(1)
    assert item is not None
    assert item.code == "TL-0001"


def test_item_repository_set_code_rejects_missing_item(
    repository_conn: sqlite3.Connection,
) -> None:
    with pytest.raises(ValidationError, match="品目が見つかりません"):
        ItemRepository(repository_conn).set_code(999, "TL-0001")


def test_item_repository_set_code_converts_unique_violation(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = ItemRepository(repository_conn)
    _insert_item(repository_conn, item_id=1, code="ST-0001")
    _insert_item(repository_conn, item_id=2, code="TL-0001")

    with pytest.raises(ValidationError, match="同じ値がすでに登録されています"):
        repo.set_code(1, "TL-0001")

    assert repo.get(1).code == "ST-0001"  # type: ignore[union-attr]


def test_item_repository_insert_update_get_and_active_state(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = ItemRepository(repository_conn)
    item_id = _insert_repository_item(repo, code="ST-0001")
    item = repo.get(item_id)
    assert item is not None
    assert (item.unit, item.quantity, item.code, item.is_active) == ("個", 0, "ST-0001", True)

    update = ItemUpdate(
        id=item_id,
        client_id=2,
        purchaser_id=2,
        name="変更後",
        category_id=3,
        location_id=2,
        reorder_threshold=4,
        reorder_quantity=8,
        purchase_url="https://example.test/item",
        supplier="仕入先",
        manufacturer_part_number="M-1",
        application="用途",
        reference_price=200,
        note="備考",
    )
    updated = repo.update(update)
    assert (updated.name, updated.client_id, updated.purchaser_id) == ("変更後", 2, 2)
    assert (updated.code, updated.quantity, updated.unit) == ("ST-0001", 0, "個")
    assert repo.set_active(item_id, False).is_active is False
    assert repo.get(999) is None
    with pytest.raises(ValidationError):
        repo.update(replace(update, id=999))


def test_item_repository_quantity_update_and_integrity_errors(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = ItemRepository(repository_conn)
    _insert_item(repository_conn, item_id=1, quantity=3)
    repo.add_quantity(1, -2)
    assert repo.get(1).quantity == 1  # type: ignore[union-attr]
    with pytest.raises(NegativeStockError) as error:
        repo.add_quantity(1, -2)
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)

    repository_conn.execute("UPDATE items SET quantity = 1000000 WHERE id = 1")
    with pytest.raises(ValidationError, match="在庫数が上限"):
        repo.add_quantity(1, 1)

    with pytest.raises(ValidationError) as duplicate:
        _insert_repository_item(repo, code="ST-0001")
    assert isinstance(duplicate.value.__cause__, sqlite3.IntegrityError)


def test_item_list_filters_and_escapes_like_metacharacters(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1, name="ペン%_\\黒", quantity=2, threshold=2)
    _insert_item(
        repository_conn,
        item_id=2,
        name="工具セット",
        client_id=2,
        purchaser_id=2,
        category_id=3,
        location_id=2,
        quantity=8,
        threshold=2,
        active=0,
        part_number="型番-42",
    )
    repo = ItemRepository(repository_conn)

    exact_metacharacters = repo.list(ItemFilter(text="%_\\"))
    assert [row.id for row in exact_metacharacters] == [1]
    assert [row.id for row in repo.list(ItemFilter(text="型番-42", include_inactive=True))] == [2]
    assert [row.id for row in repo.list(ItemFilter(client_id=2, include_inactive=True))] == [2]
    assert [row.id for row in repo.list(ItemFilter(purchaser_id=2, include_inactive=True))] == [2]
    assert [row.id for row in repo.list(ItemFilter(category_id=1, include_inactive=True))] == [1, 2]
    assert [row.id for row in repo.list(ItemFilter(category_id=2, include_inactive=True))] == [1]
    assert [row.id for row in repo.list(ItemFilter(location_id=2, include_inactive=True))] == [2]
    assert [row.id for row in repo.list(ItemFilter(low_stock_only=True))] == [1]
    assert [row.id for row in repo.list(ItemFilter())] == [1]
    assert [row.id for row in repo.list(ItemFilter(include_inactive=True))] == [1, 2]
    combined = repo.list(ItemFilter(client_id=2, category_id=1, include_inactive=True))
    assert [row.id for row in combined] == [2]
    assert repo.list_low_stock()[0].id == 1


def test_item_list_builds_paths_and_latest_non_reversed_purchase_info(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1, quantity=4, threshold=5)
    repo = ItemRepository(repository_conn)
    movement_repo = MovementRepository(repository_conn)
    _insert_movement(repository_conn, movement_id=1, moved_at="2026-01-01 00:00:00")
    _insert_movement(
        repository_conn,
        movement_id=2,
        delta=5,
        moved_at="2026-02-01 00:00:00",
    )
    movement_repo.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=-5,
        unit_price=100,
        used_for=None,
        reversal_of=2,
        note=None,
        moved_at="2026-03-01 00:00:00",
    )

    row = repo.list(ItemFilter())[0]
    assert row.category_path == "備品 > 文具"
    assert (row.client_name, row.purchaser_name, row.location_name) == ("総務", "本部", "倉庫")
    assert row.is_low_stock is True
    assert row.purchase_info.last_purchased_at == "2026-01-01 00:00:00"
    assert row.purchase_info.lot_quantity == 3
    assert repo.get_purchase_info(1).lot_quantity == 3
    assert repo.get_purchase_info(999).last_purchased_at is None


def test_item_list_low_stock_boundaries_exclude_inactive_items(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1, quantity=2, threshold=2)
    _insert_item(repository_conn, item_id=2, quantity=3, threshold=2)
    _insert_item(repository_conn, item_id=3, quantity=0, threshold=0)
    _insert_item(repository_conn, item_id=4, quantity=0, threshold=0, active=0)
    repo = ItemRepository(repository_conn)

    all_rows = {row.id: row for row in repo.list(ItemFilter(include_inactive=True))}
    assert {item_id: row.is_low_stock for item_id, row in all_rows.items()} == {
        1: True,
        2: False,
        3: True,
        4: False,
    }
    assert [row.id for row in repo.list(ItemFilter(low_stock_only=True))] == [1, 3]
    assert [
        row.id for row in repo.list(ItemFilter(low_stock_only=True, include_inactive=True))
    ] == [1, 3]
    assert [row.id for row in repo.list_low_stock()] == [1, 3]


def test_movement_repository_maps_rows_orders_and_detects_reversals(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    repo = MovementRepository(repository_conn)
    original = repo.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=3,
        unit_price=100,
        used_for=None,
        reversal_of=None,
        note="入庫",
        moved_at="2026-01-02 00:00:00",
    )
    earlier = repo.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.OUT,
        delta=-1,
        unit_price=None,
        cost_amount=100,
        used_for="会議室",
        reversal_of=None,
        note=None,
        moved_at="2026-01-01 00:00:00",
    )
    reversal = repo.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=-3,
        unit_price=100,
        used_for=None,
        reversal_of=original.id,
        note="取消",
        moved_at="2026-01-03 00:00:00",
    )

    assert repo.get(original.id) == original
    assert repo.get(999) is None
    assert repo.is_reversed(original.id) is True
    assert repo.is_reversed(earlier.id) is False
    assert [row.id for row in repo.list_by_item(1)] == [earlier.id, original.id, reversal.id]
    assert [row.id for row in repo.list_all()] == [earlier.id, original.id, reversal.id]
    assert repo.list_all()[1].is_reversed is True
    assert repo.list_all()[0].staff_name == "担当A"
    assert repo.find_invalid_reversals() == []

    with pytest.raises(AlreadyReversedError) as duplicate:
        repo.insert(
            item_id=1,
            client_id=1,
            purchaser_id=1,
            staff_id=1,
            reason=Reason.IN,
            delta=-3,
            unit_price=100,
            used_for=None,
            reversal_of=original.id,
            note=None,
            moved_at="2026-01-04 00:00:00",
        )
    assert isinstance(duplicate.value.__cause__, sqlite3.IntegrityError)


def test_allocation_repository_lists_lots_and_tracks_remaining_quantity(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    movements = MovementRepository(repository_conn)
    allocations = AllocationRepository(repository_conn)
    lot = movements.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=3,
        unit_price=100,
        used_for=None,
        reversal_of=None,
        note=None,
        moved_at="2026-01-01 00:00:00",
    )
    issue = movements.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.OUT,
        delta=-2,
        unit_price=None,
        cost_amount=200,
        used_for="用途",
        reversal_of=None,
        note=None,
        moved_at="2026-01-02 00:00:00",
    )
    allocations.insert_many(issue.id, (Allocation(lot.id, 2),))

    available = allocations.list_available_lots(1)
    assert [(value.id, value.remaining_quantity, value.unit_price) for value in available] == [
        (lot.id, 1, 100)
    ]
    assert allocations.list_for_movement(issue.id) == (Allocation(lot.id, 2),)
    assert allocations.lot_remaining(lot.id) == 1
    assert movements.latest_moved_at(1) == "2026-01-02 00:00:00"
    assert movements.latest_moved_at(999) is None


MARCH_START = "2026-03-01 00:00:00"
APRIL_START = "2026-04-01 00:00:00"
MAY_START = "2026-05-01 00:00:00"


def _ids(rows: list[MovementRow]) -> list[int]:
    return [row.id for row in rows]


def test_movement_repository_list_filtered_combines_item_client_and_utc_range(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    _insert_item(repository_conn, item_id=2, code="TL-0001", category_id=3)
    _insert_movement(repository_conn, movement_id=1, moved_at="2026-03-31 23:59:59")
    _insert_movement(repository_conn, movement_id=2, moved_at=APRIL_START)
    _insert_movement(repository_conn, movement_id=3, client_id=2, moved_at="2026-04-01 12:00:00")
    _insert_movement(repository_conn, movement_id=4, item_id=2, moved_at="2026-04-02 00:00:00")
    _insert_movement(repository_conn, movement_id=5, moved_at=MAY_START)
    repo = MovementRepository(repository_conn)

    assert _ids(repo.list_filtered()) == [1, 2, 3, 4, 5]
    assert _ids(repo.list_filtered(item_id=1)) == [1, 2, 3, 5]
    assert _ids(repo.list_filtered(item_id=1, client_id=1)) == [1, 2, 5]
    assert _ids(repo.list_filtered(start_utc=APRIL_START, end_utc=MAY_START)) == [2, 3, 4]
    assert _ids(
        repo.list_filtered(item_id=1, client_id=1, start_utc=APRIL_START, end_utc=MAY_START)
    ) == [2]
    assert _ids(repo.list_filtered(start_utc=APRIL_START)) == [2, 3, 4, 5]
    assert _ids(repo.list_filtered(end_utc=APRIL_START)) == [1]
    assert _ids(repo.list_by_item(1)) == [1, 2, 3, 5]
    assert _ids(repo.list_all()) == [1, 2, 3, 4, 5]
    assert repo.count_filtered() == 5
    assert repo.count_filtered(item_id=1) == 4
    assert repo.count_filtered(client_id=2) == 1
    assert repo.count_filtered(start_utc=APRIL_START, end_utc=MAY_START) == 3


def test_movement_repository_period_judges_reversal_rows_by_original_moved_at(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    _insert_movement(repository_conn, movement_id=1, moved_at="2026-03-15 00:00:00")
    _insert_movement(
        repository_conn,
        movement_id=2,
        delta=-3,
        reversal_of=1,
        moved_at="2026-04-10 00:00:00",
    )
    _insert_movement(repository_conn, movement_id=3, moved_at="2026-04-05 00:00:00")
    _insert_movement(
        repository_conn,
        movement_id=4,
        delta=-3,
        reversal_of=3,
        moved_at="2026-06-01 00:00:00",
    )
    repo = MovementRepository(repository_conn)

    assert _ids(repo.list_filtered(item_id=1, start_utc=MARCH_START, end_utc=APRIL_START)) == [1, 2]
    assert _ids(repo.list_filtered(item_id=1, start_utc=APRIL_START, end_utc=MAY_START)) == [3, 4]
    assert repo.count_filtered(item_id=1, start_utc=APRIL_START, end_utc=MAY_START) == 2


def _insert_page_fixture(repository_conn: sqlite3.Connection) -> MovementRepository:
    _insert_item(repository_conn, item_id=1)
    for movement_id, moved_at in [
        (1, "2026-01-01 00:00:00"),
        (2, "2026-01-02 00:00:00"),
        (3, "2026-01-02 00:00:00"),
        (4, "2026-01-03 00:00:00"),
        (5, "2026-01-03 00:00:00"),
    ]:
        _insert_movement(repository_conn, movement_id=movement_id, moved_at=moved_at)
    return MovementRepository(repository_conn)


def test_movement_repository_list_page_is_descending_with_id_tiebreak(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = _insert_page_fixture(repository_conn)

    assert _ids(repo.list_page(limit=2, offset=0)) == [5, 4]
    assert _ids(repo.list_page(item_id=1, limit=2, offset=2)) == [3, 2]
    assert _ids(repo.list_page(item_id=1, limit=2, offset=4)) == [1]
    assert repo.list_page(item_id=1, limit=2, offset=5) == []
    assert _ids(
        repo.list_page(
            start_utc="2026-01-02 00:00:00", end_utc="2026-01-03 00:00:00", limit=10, offset=0
        )
    ) == [3, 2]
    assert repo.count_filtered(item_id=1) == 5


def test_movement_repository_index_in_filtered_follows_page_order(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = _insert_page_fixture(repository_conn)

    assert [repo.index_in_filtered(movement_id) for movement_id in (5, 4, 3, 2, 1)] == [
        0,
        1,
        2,
        3,
        4,
    ]
    start_utc = "2026-01-02 00:00:00"
    end_utc = "2026-01-03 00:00:00"
    assert repo.index_in_filtered(3, start_utc=start_utc, end_utc=end_utc) == 0
    assert repo.index_in_filtered(2, start_utc=start_utc, end_utc=end_utc) == 1
    assert repo.index_in_filtered(1, start_utc=start_utc, end_utc=end_utc) is None
    assert repo.index_in_filtered(4, start_utc=start_utc, end_utc=end_utc) is None
    assert repo.index_in_filtered(3, client_id=2) is None
    assert repo.index_in_filtered(999) is None


def _insert_lot_row(
    repo: MovementRepository,
    *,
    delta: int,
    moved_at: str,
    item_id: int = 1,
    reason: Reason = Reason.IN,
    unit_price: int | None = 100,
) -> StockMovement:
    return repo.insert(
        item_id=item_id,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=reason,
        delta=delta,
        unit_price=unit_price,
        used_for=None,
        reversal_of=None,
        note=None,
        moved_at=moved_at,
    )


def _insert_issue_row(
    repo: MovementRepository,
    *,
    delta: int,
    moved_at: str,
    cost_amount: int = 0,
    reversal_of: int | None = None,
) -> StockMovement:
    return repo.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.OUT,
        delta=delta,
        unit_price=None,
        cost_amount=cost_amount,
        used_for="用途",
        reversal_of=reversal_of,
        note=None,
        moved_at=moved_at,
    )


def test_list_available_lots_filters_and_orders_by_moved_at_then_id(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    _insert_item(repository_conn, item_id=2, code="TL-0001", category_id=3)
    movements = MovementRepository(repository_conn)
    allocations = AllocationRepository(repository_conn)
    later = _insert_lot_row(movements, delta=1, moved_at="2026-01-02 00:00:00")
    earlier = _insert_lot_row(movements, delta=2, moved_at="2026-01-01 00:00:00")
    same_time = _insert_lot_row(movements, delta=3, moved_at="2026-01-01 00:00:00")
    adjust = _insert_lot_row(
        movements, delta=4, moved_at="2026-01-03 00:00:00", reason=Reason.ADJUST, unit_price=None
    )
    consumed = _insert_lot_row(movements, delta=2, moved_at="2026-01-01 12:00:00")
    reversed_lot = _insert_lot_row(movements, delta=5, moved_at="2026-01-01 13:00:00")
    movements.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=-5,
        unit_price=100,
        used_for=None,
        reversal_of=reversed_lot.id,
        note=None,
        moved_at="2026-01-01 14:00:00",
    )
    _insert_lot_row(movements, delta=7, moved_at="2026-01-01 00:00:00", item_id=2)
    issue = _insert_issue_row(movements, delta=-2, moved_at="2026-01-04 00:00:00")
    allocations.insert_many(issue.id, (Allocation(consumed.id, 2),))

    lots = allocations.list_available_lots(1)

    assert [lot.id for lot in lots] == [earlier.id, same_time.id, later.id, adjust.id]
    assert [(lot.remaining_quantity, lot.unit_price) for lot in lots] == [
        (2, 100),
        (3, 100),
        (1, 100),
        (4, None),
    ]
    assert allocations.list_available_lots(999) == []


def test_allocation_reversal_restores_lot_remaining_quantity(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    movements = MovementRepository(repository_conn)
    allocations = AllocationRepository(repository_conn)
    lot = _insert_lot_row(movements, delta=3, moved_at="2026-01-01 00:00:00")
    issue = _insert_issue_row(movements, delta=-2, moved_at="2026-01-02 00:00:00", cost_amount=200)
    allocations.insert_many(issue.id, (Allocation(lot.id, 2),))
    assert allocations.lot_remaining(lot.id) == 1

    reversal = _insert_issue_row(
        movements,
        delta=2,
        moved_at="2026-01-03 00:00:00",
        cost_amount=-200,
        reversal_of=issue.id,
    )
    allocations.insert_many(reversal.id, (Allocation(lot.id, -2),))

    assert allocations.lot_remaining(lot.id) == 3
    assert [value.remaining_quantity for value in allocations.list_available_lots(1)] == [3]
    assert allocations.list_for_movement(reversal.id) == (Allocation(lot.id, -2),)


def test_lot_remaining_is_none_for_rows_that_are_not_active_lots(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    movements = MovementRepository(repository_conn)
    lot = _insert_lot_row(movements, delta=3, moved_at="2026-01-01 00:00:00")
    issue = _insert_issue_row(movements, delta=-1, moved_at="2026-01-02 00:00:00")
    lot_reversal = movements.insert(
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=-3,
        unit_price=100,
        used_for=None,
        reversal_of=lot.id,
        note=None,
        moved_at="2026-01-03 00:00:00",
    )
    allocations = AllocationRepository(repository_conn)

    assert allocations.lot_remaining(lot.id) is None
    assert allocations.lot_remaining(issue.id) is None
    assert allocations.lot_remaining(lot_reversal.id) is None
    assert allocations.lot_remaining(999) is None


def test_allocation_insert_many_converts_integrity_errors(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    movements = MovementRepository(repository_conn)
    allocations = AllocationRepository(repository_conn)
    lot = _insert_lot_row(movements, delta=3, moved_at="2026-01-01 00:00:00")
    issue = _insert_issue_row(movements, delta=-2, moved_at="2026-01-02 00:00:00")

    for invalid in ((Allocation(999, 1),), (Allocation(lot.id, 0),)):
        with pytest.raises(ValidationError) as error:
            allocations.insert_many(issue.id, invalid)
        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    allocations.insert_many(issue.id, (Allocation(lot.id, 1),))
    with pytest.raises(ValidationError):
        allocations.insert_many(issue.id, (Allocation(lot.id, 1),))
    assert allocations.list_for_movement(issue.id) == (Allocation(lot.id, 1),)


@pytest.mark.parametrize(
    ("cost_amount", "accepted"),
    [
        (0, True),
        (200, True),
        (MAX_MOVEMENT_AMOUNT, True),
        (MAX_MOVEMENT_AMOUNT + 1, False),
        (None, False),
    ],
)
def test_movement_repository_persists_and_validates_cost_amount(
    repository_conn: sqlite3.Connection, cost_amount: int | None, accepted: bool
) -> None:
    _insert_item(repository_conn, item_id=1)
    repo = MovementRepository(repository_conn)
    values: dict[str, Any] = {
        "item_id": 1,
        "client_id": 1,
        "purchaser_id": 1,
        "staff_id": 1,
        "reason": Reason.OUT,
        "delta": -1,
        "unit_price": None,
        "cost_amount": cost_amount,
        "used_for": "用途",
        "reversal_of": None,
        "note": None,
        "moved_at": "2026-01-01 00:00:00",
    }

    if accepted:
        movement = repo.insert(**values)
        assert movement.cost_amount == cost_amount
        fetched = repo.get(movement.id)
        assert fetched is not None
        assert fetched.cost_amount == cost_amount
        assert repo.list_by_item(1)[0].cost_amount == cost_amount
    else:
        with pytest.raises(ValidationError) as error:
            repo.insert(**values)
        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
        assert repository_conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0


def test_find_quantity_mismatches_reports_only_differences(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1, quantity=3)
    _insert_item(repository_conn, item_id=2, quantity=0)
    _insert_movement(repository_conn, movement_id=1, item_id=1, delta=2)
    repo = ItemRepository(repository_conn)
    assert repo.find_quantity_mismatches() == [(1, 3, 2)]

    repository_conn.execute("UPDATE items SET quantity = 2 WHERE id = 1")
    assert repo.find_quantity_mismatches() == []


def test_find_invalid_reversals_detects_all_inconsistent_cases(
    repository_conn: sqlite3.Connection,
) -> None:
    _insert_item(repository_conn, item_id=1)
    _insert_item(repository_conn, item_id=2, code="TL-0001", category_id=3)
    _insert_movement(repository_conn, movement_id=1, delta=5)
    _insert_movement(repository_conn, movement_id=2, delta=-5, reversal_of=1)
    _insert_movement(repository_conn, movement_id=3, item_id=1, delta=-1, reason="out")
    _insert_movement(repository_conn, movement_id=4, delta=0, reason="adjust")
    _insert_movement(repository_conn, movement_id=5, delta=0, reason="adjust", reversal_of=4)
    _insert_movement(repository_conn, movement_id=6, delta=5)
    _insert_movement(repository_conn, movement_id=7, delta=-5, reversal_of=6)
    _insert_movement(repository_conn, movement_id=8, delta=-5, reversal_of=7)
    repo = MovementRepository(repository_conn)
    assert repo.find_invalid_reversals() == [5, 8]

    with unchecked_constraints(repository_conn):
        repository_conn.execute("UPDATE stock_movements SET delta = -4 WHERE id = 2")
        repository_conn.execute("UPDATE stock_movements SET item_id = 2 WHERE id = 7")
        repository_conn.execute("UPDATE stock_movements SET reason = 'out' WHERE id = 2")
        repository_conn.execute("UPDATE stock_movements SET client_id = 2 WHERE id = 2")
        repository_conn.execute("UPDATE stock_movements SET purchaser_id = 2 WHERE id = 2")
        repository_conn.execute("UPDATE stock_movements SET unit_price = 200 WHERE id = 2")
        repository_conn.execute("UPDATE stock_movements SET used_for = '別用途' WHERE id = 2")
    assert repo.find_invalid_reversals() == [2, 5, 7, 8]

    repository_conn.execute("PRAGMA foreign_keys = OFF")
    repository_conn.execute("DELETE FROM stock_movements WHERE id = 1")
    repository_conn.execute("PRAGMA foreign_keys = ON")
    assert 2 in repo.find_invalid_reversals()


def test_master_repository_crud_usage_and_category_tree(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = MasterRepository(repository_conn)
    assert [master.id for master in repo.list_clients()] == [2, 1]
    assert len(repo.list_clients(include_inactive=True)) == 3
    assert repo.get_client(1).name == "総務"  # type: ignore[union-attr]
    assert repo.get_category(2).code_prefix == "ST"  # type: ignore[union-attr]
    assert repo.category_full_path(2) == "備品 > 文具"
    assert repo.category_full_path(999) is None
    assert repo.descendant_ids(1) == [1, 2, 3]

    client = repo.insert_client("開発")
    assert repo.update_client_name(client.id, "開発部").name == "開発部"
    assert repo.set_client_active(client.id, False).is_active is False
    purchaser = repo.insert_purchaser("研究費")
    assert repo.update_purchaser_name(purchaser.id, "設備費").name == "設備費"
    assert repo.set_purchaser_active(purchaser.id, False).is_active is False
    staff = repo.insert_staff("担当C")
    assert repo.update_staff_name(staff.id, "担当D").name == "担当D"
    assert repo.set_staff_active(staff.id, False).is_active is False
    category = repo.insert_category("消耗品", "CS", 1)
    assert repo.update_category_name(category.id, "消耗品類").name == "消耗品類"
    assert repo.update_category_parent(category.id, 2).parent_id == 2
    assert repo.update_category_prefix(category.id, "CS2").code_prefix == "CS2"
    location = repo.insert_location("別倉庫")
    assert repo.update_location_name(location.id, "第2倉庫").name == "第2倉庫"

    assert not repo.client_in_use(2)
    assert not repo.purchaser_in_use(2)
    assert not repo.staff_in_use(2)
    assert not repo.category_in_use(3)
    assert not repo.location_in_use(2)
    _insert_item(repository_conn, item_id=1)
    _insert_movement(repository_conn, movement_id=1)
    assert repo.client_in_use(1)
    assert repo.purchaser_in_use(1)
    assert repo.staff_in_use(1)
    assert repo.category_in_use(2)
    assert repo.location_in_use(1)
    with pytest.raises(MasterInUseError) as in_use:
        repo.delete_client(1)
    assert isinstance(in_use.value.__cause__, sqlite3.IntegrityError)

    repo.delete_client(client.id)
    repo.delete_purchaser(purchaser.id)
    repo.delete_staff(staff.id)
    repo.delete_category(category.id)
    repo.delete_location(location.id)


def test_master_repository_unique_violations_become_domain_errors(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = MasterRepository(repository_conn)
    with pytest.raises(ValidationError) as error:
        repo.insert_client("総務")
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    with pytest.raises(ValidationError):
        repo.insert_category("重複接頭辞", "ST")
    with pytest.raises(ValidationError):
        repo.insert_category("不正接頭辞", "bad")


def test_stocktake_repository_crud_and_master_usage_are_independent(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = StocktakeRepository(repository_conn)
    master_repo = MasterRepository(repository_conn)

    repo.upsert(
        client_id=1,
        session_id="a" * 32,
        staff_id=1,
        started_at="2026-01-02 00:00:00",
    )
    repo.upsert(
        client_id=2,
        session_id="b" * 32,
        staff_id=2,
        started_at="2026-01-01 00:00:00",
    )

    assert repo.is_client_locked(1)
    assert not repo.is_client_locked(3)
    assert repo.is_staff_referenced(1)
    assert not repo.is_staff_referenced(3)
    assert not master_repo.client_in_use(1)
    assert not master_repo.staff_in_use(1)

    _insert_item(repository_conn, item_id=1, active=1)
    _insert_item(repository_conn, item_id=2, active=0)
    _insert_item(repository_conn, item_id=3, client_id=2)
    repo.replace_items(1, [1])
    repo.replace_items(2, [3])
    assert repo.get(1) == repo.get_by_session("a" * 32)
    client_stocktake = repo.get(1)
    assert client_stocktake is not None
    assert client_stocktake.item_count == 1
    assert repo.get(999) is None
    assert repo.get_by_session("c" * 32) is None
    assert [entry.client_id for entry in repo.list_all()] == [2, 1]

    repo.upsert(
        client_id=1,
        session_id="c" * 32,
        staff_id=3,
        started_at="2026-01-03 00:00:00",
    )
    updated = repo.get_by_session("c" * 32)
    assert updated is not None
    assert (updated.session_id, updated.staff_id, updated.staff_name, updated.item_count) == (
        "c" * 32,
        3,
        "廃止担当",
        1,
    )
    assert repo.get_by_session("a" * 32) is None
    assert not repo.is_staff_referenced(1)
    assert repo.is_staff_referenced(3)

    assert repo.delete(1)
    assert not repo.delete(1)
    assert repo.get(1) is None
    assert not repo.is_client_locked(1)
    assert not repo.is_staff_referenced(3)


def test_stocktake_repository_category_and_item_snapshot(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = StocktakeRepository(repository_conn)
    for item_id in (1, 2, 3):
        _insert_item(repository_conn, item_id=item_id)

    repo.upsert(
        client_id=1,
        session_id="a" * 32,
        staff_id=1,
        started_at="2026-01-02 00:00:00",
        category_id=2,
    )
    repo.replace_items(1, [1, 2])
    stocktake = repo.get(1)
    assert stocktake is not None
    assert (stocktake.category_id, stocktake.category_path, stocktake.item_count) == (
        2,
        "備品 > 文具",
        2,
    )
    assert repo.list_item_ids(1) == {1, 2}
    assert repo.list_item_ids(2) == set()
    assert repo.is_category_referenced(2)
    assert not repo.is_category_referenced(1)
    assert not repo.is_category_referenced(3)

    # 再出力でカテゴリ・スナップショットが置き換わり、旧スナップショットは残らない
    repo.upsert(
        client_id=1,
        session_id="b" * 32,
        staff_id=1,
        started_at="2026-01-03 00:00:00",
    )
    repo.replace_items(1, [3])
    replaced = repo.get(1)
    assert replaced is not None
    assert (replaced.category_id, replaced.category_path, replaced.item_count) == (None, None, 1)
    assert repo.list_item_ids(1) == {3}
    assert not repo.is_category_referenced(2)

    # カテゴリ名の変更・移動はスナップショットを変えず、表示パスだけが追随する
    repository_conn.execute("UPDATE categories SET parent_id = NULL, name = '消耗品' WHERE id = 3")
    repo.upsert(
        client_id=1,
        session_id="c" * 32,
        staff_id=1,
        started_at="2026-01-04 00:00:00",
        category_id=3,
    )
    moved = repo.get(1)
    assert moved is not None
    assert moved.category_path == "消耗品"

    repo.replace_items(1, [])
    assert repo.list_item_ids(1) == set()
    assert repo.get(1) is not None


def test_stocktake_repository_delete_cascades_item_snapshot(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = StocktakeRepository(repository_conn)
    _insert_item(repository_conn, item_id=1)
    _insert_item(repository_conn, item_id=2, client_id=2)
    repo.upsert(client_id=1, session_id="a" * 32, staff_id=1, started_at="2026-01-02 00:00:00")
    repo.upsert(client_id=2, session_id="b" * 32, staff_id=1, started_at="2026-01-02 00:00:00")
    repo.replace_items(1, [1])
    repo.replace_items(2, [2])

    assert repo.delete(1)

    rows = repository_conn.execute(
        "SELECT client_id, item_id FROM active_stocktake_items"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(2, 2)]


def test_stocktake_repository_converts_integrity_errors(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = StocktakeRepository(repository_conn)
    _insert_item(repository_conn, item_id=1)
    repo.upsert(client_id=1, session_id="a" * 32, staff_id=1, started_at="2026-01-02 00:00:00")

    with pytest.raises(ValidationError, match="重複"):
        repo.replace_items(1, [1, 1])
    with pytest.raises(ValidationError, match="見つかりません"):
        repo.replace_items(1, [999])
    with pytest.raises(ValidationError, match="見つかりません"):
        repo.replace_items(2, [1])
    with pytest.raises(ValidationError, match="見つかりません"):
        repo.upsert(
            client_id=2,
            session_id="b" * 32,
            staff_id=1,
            started_at="2026-01-02 00:00:00",
            category_id=999,
        )


def test_settings_repository_get_set_all(repository_conn: sqlite3.Connection) -> None:
    repo = SettingsRepository(repository_conn)
    assert repo.get("missing") is None
    assert repo.get("fiscal_year_start_month") == "4"
    repo.set("fiscal_year_start_month", "10")
    repo.set("additional", "value")
    assert repo.all() == {"additional": "value", "fiscal_year_start_month": "10"}


def test_total_aggregates_repository_starts_at_zero_and_applies_partial_deltas(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = TotalAggregatesRepository(repository_conn)
    assert repo.get() == dict.fromkeys(AGGREGATE_COLUMNS, 0)

    updated = repo.apply({"inbound_quantity": 5, "expenditure": 300})
    assert updated == {
        **dict.fromkeys(AGGREGATE_COLUMNS, 0),
        "inbound_quantity": 5,
        "expenditure": 300,
    }
    assert repo.get() == updated

    repo.apply({"expenditure": -100})
    assert repo.get()["expenditure"] == 200
    assert repo.get()["inbound_quantity"] == 5


def test_total_aggregates_repository_empty_deltas_change_nothing(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = TotalAggregatesRepository(repository_conn)
    repo.apply({"outbound_quantity": 2})
    assert repo.apply({}) == repo.get()
    assert repo.get()["outbound_quantity"] == 2


def test_total_aggregates_repository_rejects_unknown_column_without_update(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = TotalAggregatesRepository(repository_conn)
    with pytest.raises(ValueError, match="未定義の集計項目"):
        repo.apply({"inbound_quantity": 1, "unknown": 1})
    assert repo.get() == dict.fromkeys(AGGREGATE_COLUMNS, 0)


def test_total_aggregates_repository_enforces_zero_and_upper_limit(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = TotalAggregatesRepository(repository_conn)
    with pytest.raises(ValidationError, match="disposal_amount"):
        repo.apply({"disposal_amount": -1})

    repo.apply({"expenditure": MAX_AGGREGATE_VALUE})
    assert repo.get()["expenditure"] == MAX_AGGREGATE_VALUE
    with pytest.raises(ValidationError, match="expenditure"):
        repo.apply({"expenditure": 1})
    assert repo.get()["expenditure"] == MAX_AGGREGATE_VALUE


def test_total_aggregates_repository_does_not_partially_apply_invalid_deltas(
    repository_conn: sqlite3.Connection,
) -> None:
    repo = TotalAggregatesRepository(repository_conn)
    with pytest.raises(ValidationError):
        repo.apply({"inbound_quantity": 3, "disposed_quantity": -1})
    assert repo.get() == dict.fromkeys(AGGREGATE_COLUMNS, 0)


def test_total_aggregates_repository_requires_the_single_row(
    repository_conn: sqlite3.Connection,
) -> None:
    repository_conn.execute("DELETE FROM total_aggregates")
    with pytest.raises(RuntimeError, match="集計管理レコード"):
        TotalAggregatesRepository(repository_conn).get()


@pytest.mark.parametrize(
    "values",
    [
        {"reorder_threshold": MAX_STOCK_QUANTITY + 1},
        {"reorder_quantity": 0},
        {"reorder_quantity": MAX_STOCK_QUANTITY + 1},
        {"reference_price": MAX_UNIT_PRICE + 1},
        {"reference_price": -1},
    ],
)
def test_item_repository_converts_limit_violations_to_validation_errors(
    repository_conn: sqlite3.Connection, values: dict[str, int]
) -> None:
    repo = ItemRepository(repository_conn)
    arguments: dict[str, Any] = {
        "client_id": 1,
        "purchaser_id": 1,
        "code": "ST-0001",
        "name": "上限検査",
        "category_id": 2,
        "location_id": 1,
        "reorder_threshold": 0,
        "reorder_quantity": None,
        "purchase_url": None,
        "supplier": None,
        "manufacturer_part_number": None,
        "application": None,
        "reference_price": None,
        "note": None,
    }
    arguments.update(values)

    with pytest.raises(ValidationError) as error:
        repo.insert(**arguments)
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    assert repository_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    item_id = _insert_repository_item(repo, code="ST-0002")
    update = ItemUpdate(
        id=item_id,
        **{
            key: arguments[key]
            for key in (
                "client_id",
                "purchaser_id",
                "name",
                "category_id",
                "location_id",
                "reorder_threshold",
                "reorder_quantity",
                "purchase_url",
                "supplier",
                "manufacturer_part_number",
                "application",
                "reference_price",
                "note",
            )
        },
    )
    with pytest.raises(ValidationError) as update_error:
        repo.update(update)
    assert isinstance(update_error.value.__cause__, sqlite3.IntegrityError)


@pytest.mark.parametrize(
    ("reason", "delta", "unit_price"),
    [
        (Reason.IN, MAX_STOCK_QUANTITY + 1, None),
        (Reason.OUT, -(MAX_STOCK_QUANTITY + 1), None),
        (Reason.IN, 1, MAX_UNIT_PRICE + 1),
        (Reason.OUT, -10_001, 10_000),
        (Reason.DISPOSE, -10_001, 10_000),
    ],
)
def test_movement_repository_converts_limit_violations_to_validation_errors(
    repository_conn: sqlite3.Connection,
    reason: Reason,
    delta: int,
    unit_price: int | None,
) -> None:
    _insert_item(repository_conn, item_id=1)
    repo = MovementRepository(repository_conn)

    with pytest.raises(ValidationError) as error:
        repo.insert(
            item_id=1,
            client_id=1,
            purchaser_id=1,
            staff_id=1,
            reason=reason,
            delta=delta,
            unit_price=unit_price,
            used_for=None,
            reversal_of=None,
            note=None,
            moved_at="2026-01-01 00:00:00",
        )

    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    assert repository_conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0


def test_item_repository_quantity_boundaries(repository_conn: sqlite3.Connection) -> None:
    repo = ItemRepository(repository_conn)
    _insert_item(repository_conn, item_id=1, quantity=MAX_STOCK_QUANTITY - 1)
    repo.add_quantity(1, 1)
    assert repo.get(1).quantity == MAX_STOCK_QUANTITY  # type: ignore[union-attr]
    repo.add_quantity(1, -MAX_STOCK_QUANTITY)
    assert repo.get(1).quantity == 0  # type: ignore[union-attr]
    with pytest.raises(NegativeStockError):
        repo.add_quantity(1, -1)
    with pytest.raises(ValidationError, match="在庫数が上限"):
        repo.add_quantity(1, MAX_STOCK_QUANTITY + 1)
    assert repo.get(1).quantity == 0  # type: ignore[union-attr]
