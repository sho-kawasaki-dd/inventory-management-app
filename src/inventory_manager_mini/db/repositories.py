from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import TypeVar, cast

from inventory_manager_mini.core.errors import (
    AlreadyReversedError,
    MasterInUseError,
    NegativeStockError,
    ValidationError,
)
from inventory_manager_mini.core.models import (
    MAX_AGGREGATE_VALUE,
    MAX_STOCK_QUANTITY,
    Category,
    Client,
    Item,
    ItemFilter,
    ItemRow,
    ItemUpdate,
    Location,
    MovementRow,
    PurchaseInfo,
    Purchaser,
    Reason,
    Staff,
    StockMovement,
)
from inventory_manager_mini.db.integrity import AGGREGATE_COLUMNS

_MODEL = TypeVar("_MODEL", bound=Client | Purchaser | Staff | Category | Location)
_ITEM_COLUMNS = (
    "id, client_id, purchaser_id, code, name, category_id, location_id, unit, quantity, "
    "reorder_threshold, reorder_quantity, purchase_url, supplier, manufacturer_part_number, "
    "application, reference_price, note, is_active, created_at, updated_at"
)
_MOVEMENT_COLUMNS = (
    "id, item_id, client_id, purchaser_id, staff_id, reason, delta, unit_price, used_for, "
    "reversal_of, note, moved_at"
)


def _bool(value: int) -> bool:
    return bool(value)


def _qualified_columns(columns: str, alias: str) -> str:
    return ", ".join(f"{alias}.{column}" for column in columns.split(", "))


def _item_from_row(row: sqlite3.Row) -> Item:
    return Item(
        id=int(row["id"]),
        client_id=int(row["client_id"]),
        purchaser_id=int(row["purchaser_id"]),
        code=str(row["code"]),
        name=str(row["name"]),
        category_id=int(row["category_id"]),
        location_id=None if row["location_id"] is None else int(row["location_id"]),
        unit=str(row["unit"]),
        quantity=int(row["quantity"]),
        reorder_threshold=int(row["reorder_threshold"]),
        reorder_quantity=(
            None if row["reorder_quantity"] is None else int(row["reorder_quantity"])
        ),
        purchase_url=None if row["purchase_url"] is None else str(row["purchase_url"]),
        supplier=None if row["supplier"] is None else str(row["supplier"]),
        manufacturer_part_number=(
            None
            if row["manufacturer_part_number"] is None
            else str(row["manufacturer_part_number"])
        ),
        application=None if row["application"] is None else str(row["application"]),
        reference_price=(None if row["reference_price"] is None else int(row["reference_price"])),
        note=None if row["note"] is None else str(row["note"]),
        is_active=_bool(int(row["is_active"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _movement_from_row(row: sqlite3.Row) -> StockMovement:
    return StockMovement(
        id=int(row["id"]),
        item_id=int(row["item_id"]),
        client_id=int(row["client_id"]),
        purchaser_id=int(row["purchaser_id"]),
        staff_id=int(row["staff_id"]),
        reason=Reason(str(row["reason"])),
        delta=int(row["delta"]),
        unit_price=None if row["unit_price"] is None else int(row["unit_price"]),
        used_for=None if row["used_for"] is None else str(row["used_for"]),
        reversal_of=None if row["reversal_of"] is None else int(row["reversal_of"]),
        note=None if row["note"] is None else str(row["note"]),
        moved_at=str(row["moved_at"]),
    )


def _master_from_row(
    row: sqlite3.Row,
    model: type[Client] | type[Purchaser] | type[Staff] | type[Category] | type[Location],
) -> Client | Purchaser | Staff | Category | Location:
    if model is Client:
        return Client(
            id=int(row["id"]), name=str(row["name"]), is_active=_bool(int(row["is_active"]))
        )
    if model is Purchaser:
        return Purchaser(
            id=int(row["id"]), name=str(row["name"]), is_active=_bool(int(row["is_active"]))
        )
    if model is Staff:
        return Staff(
            id=int(row["id"]), name=str(row["name"]), is_active=_bool(int(row["is_active"]))
        )
    if model is Category:
        return Category(
            id=int(row["id"]),
            parent_id=None if row["parent_id"] is None else int(row["parent_id"]),
            name=str(row["name"]),
            code_prefix=str(row["code_prefix"]),
            next_seq=int(row["next_seq"]),
        )
    if model is Location:
        return Location(id=int(row["id"]), name=str(row["name"]))
    raise TypeError(f"未対応のマスタモデルです: {model}")


def _convert_integrity_error(
    error: sqlite3.IntegrityError,
    mappings: Sequence[tuple[str, str, type[Exception], str]],
) -> Exception:
    error_name = getattr(error, "sqlite_errorname", "")
    message = str(error)
    for expected_name, target, error_type, user_message in mappings:
        if error_name == expected_name and target in message:
            return error_type(user_message)
    return ValidationError("データの整合性を確認してください")


def _execute(
    conn: sqlite3.Connection,
    sql: str,
    parameters: Sequence[object] = (),
    *,
    integrity_mappings: Sequence[tuple[str, str, type[Exception], str]] = (),
) -> sqlite3.Cursor:
    try:
        return conn.execute(sql, parameters)
    except sqlite3.IntegrityError as error:
        converted = _convert_integrity_error(error, integrity_mappings)
        raise converted from error


_UNIQUE_VALIDATION = (
    "SQLITE_CONSTRAINT_UNIQUE",
    "",
    ValidationError,
    "同じ値がすでに登録されています",
)


class ItemRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def allocate_code(self, category_id: int) -> str:
        row = _execute(
            self.conn,
            "UPDATE categories SET next_seq = next_seq + 1 WHERE id = ? "
            "RETURNING code_prefix, next_seq - 1 AS allocated_seq",
            (category_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("カテゴリが見つかりません")
        return f"{row['code_prefix']}-{int(row['allocated_seq']):04d}"

    def insert(
        self,
        *,
        client_id: int,
        purchaser_id: int,
        code: str,
        name: str,
        category_id: int,
        location_id: int | None,
        reorder_threshold: int,
        reorder_quantity: int | None,
        purchase_url: str | None,
        supplier: str | None,
        manufacturer_part_number: str | None,
        application: str | None,
        reference_price: int | None,
        note: str | None,
    ) -> Item:
        cursor = _execute(
            self.conn,
            """INSERT INTO items (
                client_id, purchaser_id, code, name, category_id, location_id, unit,
                reorder_threshold, reorder_quantity, purchase_url, supplier,
                manufacturer_part_number, application, reference_price, note
            ) VALUES (?, ?, ?, ?, ?, ?, '個', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                purchaser_id,
                code,
                name,
                category_id,
                location_id,
                reorder_threshold,
                reorder_quantity,
                purchase_url,
                supplier,
                manufacturer_part_number,
                application,
                reference_price,
                note,
            ),
            integrity_mappings=(_UNIQUE_VALIDATION,),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("登録した品目 ID を取得できません")
        item = self.get(cursor.lastrowid)
        if item is None:
            raise RuntimeError("登録した品目を取得できません")
        return item

    def update(self, item: ItemUpdate) -> Item:
        _execute(
            self.conn,
            """UPDATE items SET client_id = ?, purchaser_id = ?, name = ?, category_id = ?,
                location_id = ?, reorder_threshold = ?, reorder_quantity = ?, purchase_url = ?,
                supplier = ?, manufacturer_part_number = ?, application = ?, reference_price = ?,
                note = ? WHERE id = ?""",
            (
                item.client_id,
                item.purchaser_id,
                item.name,
                item.category_id,
                item.location_id,
                item.reorder_threshold,
                item.reorder_quantity,
                item.purchase_url,
                item.supplier,
                item.manufacturer_part_number,
                item.application,
                item.reference_price,
                item.note,
                item.id,
            ),
            integrity_mappings=(_UNIQUE_VALIDATION,),
        )
        updated = self.get(item.id)
        if updated is None:
            raise ValidationError("品目が見つかりません")
        return updated

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return None if row is None else _item_from_row(row)

    def set_active(self, item_id: int, is_active: bool) -> Item:
        cursor = self.conn.execute(
            "UPDATE items SET is_active = ? WHERE id = ?", (int(is_active), item_id)
        )
        if cursor.rowcount == 0:
            raise ValidationError("品目が見つかりません")
        item = self.get(item_id)
        if item is None:
            raise RuntimeError("更新した品目を取得できません")
        return item

    def add_quantity(self, item_id: int, delta: int) -> None:
        current = self.conn.execute(
            "SELECT quantity FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if current is not None and int(current[0]) + delta > MAX_STOCK_QUANTITY:
            raise ValidationError("操作後の在庫数が上限(1,000,000)を超えます")
        _execute(
            self.conn,
            "UPDATE items SET quantity = quantity + ? WHERE id = ?",
            (delta, item_id),
            integrity_mappings=(
                (
                    "SQLITE_CONSTRAINT_CHECK",
                    "quantity",
                    NegativeStockError,
                    "在庫数が負になるため更新できません",
                ),
            ),
        )

    def list(self, filter: ItemFilter) -> list[ItemRow]:
        conditions: list[str] = []
        parameters: list[object] = []
        if filter.text is not None:
            escaped = filter.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(
                "(i.name LIKE ? ESCAPE '\\' OR i.code LIKE ? ESCAPE '\\' "
                "OR i.manufacturer_part_number LIKE ? ESCAPE '\\')"
            )
            parameters.extend((pattern, pattern, pattern))
        if filter.client_id is not None:
            conditions.append("i.client_id = ?")
            parameters.append(filter.client_id)
        if filter.purchaser_id is not None:
            conditions.append("i.purchaser_id = ?")
            parameters.append(filter.purchaser_id)
        if filter.category_id is not None:
            conditions.append("i.category_id IN (SELECT id FROM descendants)")
        if filter.location_id is not None:
            conditions.append("i.location_id = ?")
            parameters.append(filter.location_id)
        if filter.low_stock_only:
            conditions.append("i.quantity <= i.reorder_threshold")
        if not filter.include_inactive:
            conditions.append("i.is_active = 1")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        if filter.category_id is None:
            descendants = ""
        else:
            descendants = """descendants(id) AS (
                SELECT id FROM categories WHERE id = ?
                UNION ALL
                SELECT c.id FROM categories c JOIN descendants d ON c.parent_id = d.id
            ),"""
            parameters.insert(0, filter.category_id)
        rows = self.conn.execute(
            f"""WITH RECURSIVE {descendants}
            category_tree(id, path) AS (
                SELECT id, name FROM categories WHERE parent_id IS NULL
                UNION ALL
                SELECT c.id, category_tree.path || ' > ' || c.name
                FROM categories c JOIN category_tree ON c.parent_id = category_tree.id
            )
            SELECT {_qualified_columns(_ITEM_COLUMNS, "i")},
                cl.name AS client_name, pu.name AS purchaser_name,
                ct.path AS category_path, lo.name AS location_name,
                (i.quantity <= i.reorder_threshold) AS is_low_stock,
                purchase.moved_at AS last_purchased_at, purchase.delta AS lot_quantity
            FROM items i
            JOIN clients cl ON cl.id = i.client_id
            JOIN purchasers pu ON pu.id = i.purchaser_id
            JOIN category_tree ct ON ct.id = i.category_id
            LEFT JOIN locations lo ON lo.id = i.location_id
            LEFT JOIN stock_movements purchase ON purchase.id = (
                SELECT m.id FROM stock_movements m INDEXED BY idx_movements_item_purchase
                WHERE m.item_id = i.id AND m.reason = 'in' AND m.reversal_of IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM stock_movements r WHERE r.reversal_of = m.id
                    )
                ORDER BY m.moved_at DESC, m.id DESC LIMIT 1
            ){where}
            ORDER BY i.code, i.id""",
            parameters,
        ).fetchall()
        return [self._item_row(row) for row in rows]

    def list_low_stock(self) -> list[ItemRow]:
        return self.list(ItemFilter(low_stock_only=True))

    def get_purchase_info(self, item_id: int) -> PurchaseInfo:
        row = self.conn.execute(
            """SELECT moved_at, delta FROM stock_movements m
            WHERE m.item_id = ? AND m.reason = 'in' AND m.reversal_of IS NULL
                AND NOT EXISTS (SELECT 1 FROM stock_movements r WHERE r.reversal_of = m.id)
            ORDER BY m.moved_at DESC, m.id DESC LIMIT 1""",
            (item_id,),
        ).fetchone()
        if row is None:
            return PurchaseInfo(last_purchased_at=None, lot_quantity=None)
        return PurchaseInfo(last_purchased_at=str(row["moved_at"]), lot_quantity=int(row["delta"]))

    def find_quantity_mismatches(self) -> list[tuple[int, int, int]]:
        totals: dict[int, int] = {}
        for item_id, delta in self.conn.execute("SELECT item_id, delta FROM stock_movements"):
            totals[int(item_id)] = totals.get(int(item_id), 0) + int(delta)
        return [
            (int(row[0]), int(row[1]), totals.get(int(row[0]), 0))
            for row in self.conn.execute("SELECT id, quantity FROM items ORDER BY id")
            if int(row[1]) != totals.get(int(row[0]), 0)
        ]

    @staticmethod
    def _item_row(row: sqlite3.Row) -> ItemRow:
        item = _item_from_row(row)
        return ItemRow(
            **{field: getattr(item, field) for field in Item.__dataclass_fields__},
            client_name=str(row["client_name"]),
            purchaser_name=str(row["purchaser_name"]),
            category_path=str(row["category_path"]),
            location_name=None if row["location_name"] is None else str(row["location_name"]),
            is_low_stock=_bool(int(row["is_low_stock"])),
            purchase_info=PurchaseInfo(
                last_purchased_at=(
                    None if row["last_purchased_at"] is None else str(row["last_purchased_at"])
                ),
                lot_quantity=None if row["lot_quantity"] is None else int(row["lot_quantity"]),
            ),
        )


class MovementRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def insert(
        self,
        *,
        item_id: int,
        client_id: int,
        purchaser_id: int,
        staff_id: int,
        reason: Reason,
        delta: int,
        unit_price: int | None,
        used_for: str | None,
        reversal_of: int | None,
        note: str | None,
        moved_at: str,
    ) -> StockMovement:
        cursor = _execute(
            self.conn,
            """INSERT INTO stock_movements
            (item_id, client_id, purchaser_id, staff_id, reason, delta, unit_price,
             used_for, reversal_of, note, moved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item_id,
                client_id,
                purchaser_id,
                staff_id,
                reason.value,
                delta,
                unit_price,
                used_for,
                reversal_of,
                note,
                moved_at,
            ),
            integrity_mappings=(
                (
                    "SQLITE_CONSTRAINT_UNIQUE",
                    "stock_movements.reversal_of",
                    AlreadyReversedError,
                    "この履歴はすでに取り消されています",
                ),
                _UNIQUE_VALIDATION,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("登録した履歴 ID を取得できません")
        movement = self.get(cursor.lastrowid)
        if movement is None:
            raise RuntimeError("登録した履歴を取得できません")
        return movement

    def get(self, movement_id: int) -> StockMovement | None:
        row = self.conn.execute(
            f"SELECT {_MOVEMENT_COLUMNS} FROM stock_movements WHERE id = ?", (movement_id,)
        ).fetchone()
        return None if row is None else _movement_from_row(row)

    def is_reversed(self, movement_id: int) -> bool:
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM stock_movements WHERE reversal_of = ?) AS result",
            (movement_id,),
        ).fetchone()
        return bool(row["result"])

    def list_by_item(self, item_id: int) -> list[MovementRow]:
        return self._list("WHERE m.item_id = ?", (item_id,))

    def list_all(self) -> list[MovementRow]:
        return self._list("", ())

    def find_invalid_reversals(self) -> list[int]:
        rows = self.conn.execute(
            """SELECT m.id FROM stock_movements m
            LEFT JOIN stock_movements o ON o.id = m.reversal_of
            WHERE m.reversal_of IS NOT NULL AND (
                o.id IS NULL OR o.reversal_of IS NOT NULL
                OR (o.reason = 'adjust' AND o.delta = 0)
                OR m.item_id != o.item_id OR m.delta != -o.delta
                OR m.reason IS NOT o.reason OR m.client_id IS NOT o.client_id
                OR m.purchaser_id IS NOT o.purchaser_id
                OR m.unit_price IS NOT o.unit_price OR m.used_for IS NOT o.used_for
            ) ORDER BY m.id"""
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def _list(self, where: str, parameters: Sequence[object]) -> list[MovementRow]:
        rows = self.conn.execute(
            f"""SELECT {_qualified_columns(_MOVEMENT_COLUMNS, "m")},
                i.code, i.name AS item_name, c.name AS client_name,
                p.name AS purchaser_name, s.name AS staff_name,
                EXISTS(SELECT 1 FROM stock_movements r WHERE r.reversal_of = m.id) AS is_reversed
            FROM stock_movements m
            JOIN items i ON i.id = m.item_id
            JOIN clients c ON c.id = m.client_id
            JOIN purchasers p ON p.id = m.purchaser_id
            JOIN staff s ON s.id = m.staff_id
            {where} ORDER BY m.moved_at, m.id""",
            parameters,
        ).fetchall()
        result: list[MovementRow] = []
        for row in rows:
            movement = _movement_from_row(row)
            result.append(
                MovementRow(
                    **{
                        field: getattr(movement, field)
                        for field in StockMovement.__dataclass_fields__
                    },
                    code=str(row["code"]),
                    item_name=str(row["item_name"]),
                    client_name=str(row["client_name"]),
                    purchaser_name=str(row["purchaser_name"]),
                    staff_name=str(row["staff_name"]),
                    is_reversed=_bool(int(row["is_reversed"])),
                )
            )
        return result


class MasterRepository:
    _TABLES = {
        Client: ("clients", ("id", "name", "is_active")),
        Purchaser: ("purchasers", ("id", "name", "is_active")),
        Staff: ("staff", ("id", "name", "is_active")),
        Category: ("categories", ("id", "parent_id", "name", "code_prefix", "next_seq")),
        Location: ("locations", ("id", "name")),
    }

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def list_clients(self, include_inactive: bool = False) -> list[Client]:
        return self._list(Client, include_inactive)

    def list_purchasers(self, include_inactive: bool = False) -> list[Purchaser]:
        return self._list(Purchaser, include_inactive)

    def list_staff(self, include_inactive: bool = False) -> list[Staff]:
        return self._list(Staff, include_inactive)

    def list_categories(self) -> list[Category]:
        return self._list(Category)

    def list_locations(self) -> list[Location]:
        return self._list(Location)

    def get_client(self, entity_id: int) -> Client | None:
        return self._get(Client, entity_id)

    def get_purchaser(self, entity_id: int) -> Purchaser | None:
        return self._get(Purchaser, entity_id)

    def get_staff(self, entity_id: int) -> Staff | None:
        return self._get(Staff, entity_id)

    def get_category(self, entity_id: int) -> Category | None:
        return self._get(Category, entity_id)

    def get_location(self, entity_id: int) -> Location | None:
        return self._get(Location, entity_id)

    def insert_client(self, name: str) -> Client:
        return self._insert(Client, {"name": name})

    def insert_purchaser(self, name: str) -> Purchaser:
        return self._insert(Purchaser, {"name": name})

    def insert_staff(self, name: str) -> Staff:
        return self._insert(Staff, {"name": name})

    def insert_category(
        self, name: str, code_prefix: str, parent_id: int | None = None
    ) -> Category:
        return self._insert(
            Category, {"name": name, "code_prefix": code_prefix, "parent_id": parent_id}
        )

    def insert_location(self, name: str) -> Location:
        return self._insert(Location, {"name": name})

    def update_client_name(self, entity_id: int, name: str) -> Client:
        return self._update_name(Client, entity_id, name)

    def update_purchaser_name(self, entity_id: int, name: str) -> Purchaser:
        return self._update_name(Purchaser, entity_id, name)

    def update_staff_name(self, entity_id: int, name: str) -> Staff:
        return self._update_name(Staff, entity_id, name)

    def update_category_name(self, entity_id: int, name: str) -> Category:
        return self._update_name(Category, entity_id, name)

    def update_location_name(self, entity_id: int, name: str) -> Location:
        return self._update_name(Location, entity_id, name)

    def set_client_active(self, entity_id: int, is_active: bool) -> Client:
        return self._set_active(Client, entity_id, is_active)

    def set_purchaser_active(self, entity_id: int, is_active: bool) -> Purchaser:
        return self._set_active(Purchaser, entity_id, is_active)

    def set_staff_active(self, entity_id: int, is_active: bool) -> Staff:
        return self._set_active(Staff, entity_id, is_active)

    def update_category_parent(self, entity_id: int, parent_id: int | None) -> Category:
        return self._update_category(entity_id, "parent_id", parent_id)

    def update_category_prefix(self, entity_id: int, code_prefix: str) -> Category:
        return self._update_category(entity_id, "code_prefix", code_prefix)

    def delete_client(self, entity_id: int) -> None:
        self._delete(Client, entity_id)

    def delete_purchaser(self, entity_id: int) -> None:
        self._delete(Purchaser, entity_id)

    def delete_staff(self, entity_id: int) -> None:
        self._delete(Staff, entity_id)

    def delete_category(self, entity_id: int) -> None:
        self._delete(Category, entity_id)

    def delete_location(self, entity_id: int) -> None:
        self._delete(Location, entity_id)

    def client_in_use(self, entity_id: int) -> bool:
        return self._exists(
            "SELECT EXISTS(SELECT 1 FROM items WHERE client_id = ?) "
            "OR EXISTS(SELECT 1 FROM stock_movements WHERE client_id = ?)",
            (entity_id, entity_id),
        )

    def purchaser_in_use(self, entity_id: int) -> bool:
        return self._exists(
            "SELECT EXISTS(SELECT 1 FROM items WHERE purchaser_id = ?) "
            "OR EXISTS(SELECT 1 FROM stock_movements WHERE purchaser_id = ?)",
            (entity_id, entity_id),
        )

    def staff_in_use(self, entity_id: int) -> bool:
        return self._exists(
            "SELECT EXISTS(SELECT 1 FROM stock_movements WHERE staff_id = ?)", (entity_id,)
        )

    def category_in_use(self, entity_id: int) -> bool:
        return self._exists(
            "SELECT EXISTS(SELECT 1 FROM items WHERE category_id = ?) "
            "OR EXISTS(SELECT 1 FROM categories WHERE parent_id = ?) "
            "OR EXISTS(SELECT 1 FROM categories WHERE id = ? AND next_seq > 1)",
            (entity_id, entity_id, entity_id),
        )

    def location_in_use(self, entity_id: int) -> bool:
        return self._exists(
            "SELECT EXISTS(SELECT 1 FROM items WHERE location_id = ?)", (entity_id,)
        )

    def descendant_ids(self, category_id: int) -> list[int]:
        rows = self.conn.execute(
            """WITH RECURSIVE tree(id) AS (
                SELECT id FROM categories WHERE id = ?
                UNION ALL
                SELECT c.id FROM categories c JOIN tree t ON c.parent_id = t.id
            ) SELECT id FROM tree ORDER BY id""",
            (category_id,),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def category_full_path(self, category_id: int) -> str | None:
        row = self.conn.execute(
            """WITH RECURSIVE paths(id, parent_id, path, depth) AS (
                SELECT id, parent_id, name, 0 FROM categories WHERE id = ?
                UNION ALL
                SELECT parent.id, parent.parent_id, parent.name || ' > ' || paths.path,
                    paths.depth + 1
                FROM categories parent JOIN paths ON paths.parent_id = parent.id
            ) SELECT path FROM paths ORDER BY depth DESC LIMIT 1""",
            (category_id,),
        ).fetchone()
        return None if row is None else str(row["path"])

    def _list(self, model: type[_MODEL], include_inactive: bool = True) -> list[_MODEL]:
        table, columns = self._TABLES[model]
        where = " WHERE is_active = 1" if not include_inactive and "is_active" in columns else ""
        rows = self.conn.execute(
            f"SELECT {', '.join(columns)} FROM {table}{where} ORDER BY name, id"
        ).fetchall()
        return [cast(_MODEL, _master_from_row(row, model)) for row in rows]

    def _get(self, model: type[_MODEL], entity_id: int) -> _MODEL | None:
        table, columns = self._TABLES[model]
        row = self.conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?", (entity_id,)
        ).fetchone()
        return None if row is None else cast(_MODEL, _master_from_row(row, model))

    def _insert(self, model: type[_MODEL], values: Mapping[str, object]) -> _MODEL:
        table, _ = self._TABLES[model]
        columns = tuple(values)
        placeholders = ", ".join("?" for _ in columns)
        cursor = _execute(
            self.conn,
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(values[column] for column in columns),
            integrity_mappings=(_UNIQUE_VALIDATION,),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("登録したマスタ ID を取得できません")
        entity = self._get(model, cursor.lastrowid)
        if entity is None:
            raise RuntimeError("登録したマスタを取得できません")
        return entity

    def _update_name(self, model: type[_MODEL], entity_id: int, name: str) -> _MODEL:
        table, _ = self._TABLES[model]
        _execute(
            self.conn,
            f"UPDATE {table} SET name = ? WHERE id = ?",
            (name, entity_id),
            integrity_mappings=(_UNIQUE_VALIDATION,),
        )
        entity = self._get(model, entity_id)
        if entity is None:
            raise ValidationError("マスタが見つかりません")
        return entity

    def _set_active(self, model: type[_MODEL], entity_id: int, is_active: bool) -> _MODEL:
        table, _ = self._TABLES[model]
        self.conn.execute(
            f"UPDATE {table} SET is_active = ? WHERE id = ?", (int(is_active), entity_id)
        )
        entity = self._get(model, entity_id)
        if entity is None:
            raise ValidationError("マスタが見つかりません")
        return entity

    def _update_category(self, entity_id: int, field: str, value: object) -> Category:
        _execute(
            self.conn,
            f"UPDATE categories SET {field} = ? WHERE id = ?",
            (value, entity_id),
            integrity_mappings=(_UNIQUE_VALIDATION,),
        )
        category = self.get_category(entity_id)
        if category is None:
            raise ValidationError("カテゴリが見つかりません")
        return category

    def _delete(self, model: type[_MODEL], entity_id: int) -> None:
        table, _ = self._TABLES[model]
        _execute(
            self.conn,
            f"DELETE FROM {table} WHERE id = ?",
            (entity_id,),
            integrity_mappings=(
                (
                    "SQLITE_CONSTRAINT_FOREIGNKEY",
                    "",
                    MasterInUseError,
                    "使用中のマスタは削除できません",
                ),
            ),
        )

    def _exists(self, sql: str, parameters: Sequence[object]) -> bool:
        row = self.conn.execute(sql, parameters).fetchone()
        return bool(row[0])


class SettingsRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def all(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}


class TotalAggregatesRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    def get(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT inbound_quantity, outbound_quantity, disposed_quantity, expenditure, "
            "disposal_amount FROM total_aggregates WHERE id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("集計管理レコードがありません")
        return {column: int(row[column]) for column in AGGREGATE_COLUMNS}

    def apply(self, deltas: dict[str, int]) -> dict[str, int]:
        current = self.get()
        updated = current.copy()
        for column, delta in deltas.items():
            if column not in AGGREGATE_COLUMNS:
                raise ValueError(f"未定義の集計項目です: {column}")
            updated[column] += delta
            if not 0 <= updated[column] <= MAX_AGGREGATE_VALUE:
                raise ValidationError(
                    f"集計値 {column} は 0〜{MAX_AGGREGATE_VALUE:,} の範囲を超えます"
                )
        assignments = ", ".join(f"{column} = ?" for column in deltas)
        if assignments:
            _execute(
                self.conn,
                f"UPDATE total_aggregates SET {assignments} WHERE id = 1",
                tuple(updated[column] for column in deltas),
            )
        return updated
