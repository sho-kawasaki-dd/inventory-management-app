from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from importlib.resources import files

import pytest

from inventory_manager_mini.core.errors import (
    AlreadyReversedError,
    MasterInUseError,
    NegativeStockError,
    ValidationError,
)
from inventory_manager_mini.core.models import MAX_AGGREGATE_VALUE, ItemFilter, ItemUpdate, Reason
from inventory_manager_mini.db.connection import connect_memory
from inventory_manager_mini.db.integrity import AGGREGATE_COLUMNS
from inventory_manager_mini.db.repositories import (
    ItemRepository,
    MasterRepository,
    MovementRepository,
    SettingsRepository,
    TotalAggregatesRepository,
)


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
    conn.execute(
        """INSERT INTO stock_movements
        (id, item_id, client_id, purchaser_id, staff_id, reason, delta, unit_price,
         used_for, reversal_of, moved_at)
        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
        (
            movement_id,
            item_id,
            client_id,
            purchaser_id,
            reason,
            delta,
            unit_price,
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
