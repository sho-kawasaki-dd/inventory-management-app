from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import cast
from urllib.parse import urlsplit

from inventory_manager_mini.core.errors import (
    AlreadyReversedError,
    CategoryCycleError,
    InactiveItemError,
    InactiveMasterError,
    MasterInUseError,
    NegativeStockError,
    PrefixLockedError,
    ReversalNotAllowedError,
    ValidationError,
)
from inventory_manager_mini.core.models import (
    Category,
    Client,
    Item,
    ItemFilter,
    ItemRow,
    ItemUpdate,
    Location,
    MovementRow,
    NewItem,
    PurchaseInfo,
    Purchaser,
    Reason,
    Staff,
    StockMovement,
)
from inventory_manager_mini.core.timeutil import utc_now, utc_now_str
from inventory_manager_mini.db.connection import transaction
from inventory_manager_mini.db.repositories import (
    ItemRepository,
    MasterRepository,
    MovementRepository,
    SettingsRepository,
)

_PREFIX_PATTERN = re.compile(r"^[A-Z0-9]{2,5}$")
_FISCAL_YEAR_KEY = "fiscal_year_start_month"


def validate_purchase_url(url: str | None) -> str | None:
    normalized = _normalize_optional(url, "販売ページ URL")
    if normalized is None:
        return None
    try:
        parsed = urlsplit(normalized)
        valid_host = parsed.hostname is not None
    except ValueError as error:
        raise ValidationError("販売ページ URL の形式が正しくありません") from error
    if parsed.scheme.lower() not in {"http", "https"} or not valid_host:
        raise ValidationError("販売ページ URL は http または https のホスト付き URL にしてください")
    return normalized


def _normalize_optional(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _required_text(value: str, label: str) -> str:
    normalized = _normalize_optional(value, label)
    if normalized is None:
        raise ValidationError(f"{label}を入力してください")
    return normalized


def _integer(value: int, label: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationError(f"{label}は{minimum}以上の整数で指定してください")
    return value


def _optional_integer(value: int | None, label: str, minimum: int) -> int | None:
    return None if value is None else _integer(value, label, minimum)


def _prefix(value: str) -> str:
    normalized = _required_text(value, "接頭辞")
    if _PREFIX_PATTERN.fullmatch(normalized) is None:
        raise ValidationError("接頭辞は英大文字または数字の 2〜5 文字で指定してください")
    return normalized


def _active_master[MASTER: Client | Purchaser | Staff](master: MASTER | None, label: str) -> MASTER:
    if master is None:
        raise ValidationError(f"{label}が見つかりません")
    if not master.is_active:
        raise InactiveMasterError(f"無効化された{label}は選択できません")
    return master


def _required_master(master: object | None, label: str) -> object:
    if master is None:
        raise ValidationError(f"{label}が見つかりません")
    return master


def _normalize_new_item(new: NewItem) -> NewItem:
    return NewItem(
        client_id=_integer(new.client_id, "クライアント ID", 1),
        purchaser_id=_integer(new.purchaser_id, "発注主体 ID", 1),
        name=_required_text(new.name, "品名"),
        category_id=_integer(new.category_id, "カテゴリ ID", 1),
        location_id=_optional_integer(new.location_id, "保管場所 ID", 1),
        initial_quantity=_integer(new.initial_quantity, "初期数量", 0),
        initial_staff_id=_optional_integer(new.initial_staff_id, "担当者 ID", 1),
        reorder_threshold=_integer(new.reorder_threshold, "在庫閾値", 0),
        reorder_quantity=_optional_integer(new.reorder_quantity, "推奨発注数", 1),
        purchase_url=validate_purchase_url(new.purchase_url),
        supplier=_normalize_optional(new.supplier, "仕入先"),
        manufacturer_part_number=_normalize_optional(new.manufacturer_part_number, "メーカー型番"),
        application=_normalize_optional(new.application, "用途"),
        reference_price=_optional_integer(new.reference_price, "参考価格", 0),
        note=_normalize_optional(new.note, "備考"),
    )


def _normalize_item_update(update: ItemUpdate) -> ItemUpdate:
    return ItemUpdate(
        id=_integer(update.id, "品目 ID", 1),
        client_id=_integer(update.client_id, "クライアント ID", 1),
        purchaser_id=_integer(update.purchaser_id, "発注主体 ID", 1),
        name=_required_text(update.name, "品名"),
        category_id=_integer(update.category_id, "カテゴリ ID", 1),
        location_id=_optional_integer(update.location_id, "保管場所 ID", 1),
        reorder_threshold=_integer(update.reorder_threshold, "在庫閾値", 0),
        reorder_quantity=_optional_integer(update.reorder_quantity, "推奨発注数", 1),
        purchase_url=validate_purchase_url(update.purchase_url),
        supplier=_normalize_optional(update.supplier, "仕入先"),
        manufacturer_part_number=_normalize_optional(
            update.manufacturer_part_number, "メーカー型番"
        ),
        application=_normalize_optional(update.application, "用途"),
        reference_price=_optional_integer(update.reference_price, "参考価格", 0),
        note=_normalize_optional(update.note, "備考"),
    )


class InventoryService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.items = ItemRepository(conn)
        self.movements = MovementRepository(conn)
        self.masters = MasterRepository(conn)

    def create_item(self, new: NewItem) -> Item:
        value = _normalize_new_item(new)
        with transaction(self.conn):
            _active_master(self.masters.get_client(value.client_id), "クライアント")
            _active_master(self.masters.get_purchaser(value.purchaser_id), "発注主体")
            _required_master(self.masters.get_category(value.category_id), "カテゴリ")
            if value.location_id is not None:
                _required_master(self.masters.get_location(value.location_id), "保管場所")
            if value.initial_quantity > 0:
                if value.initial_staff_id is None:
                    raise ValidationError("初期数量を記録する担当者を選択してください")
                _active_master(self.masters.get_staff(value.initial_staff_id), "担当者")

            code = self.items.allocate_code(value.category_id)
            item = self.items.insert(
                client_id=value.client_id,
                purchaser_id=value.purchaser_id,
                code=code,
                name=value.name,
                category_id=value.category_id,
                location_id=value.location_id,
                reorder_threshold=value.reorder_threshold,
                reorder_quantity=value.reorder_quantity,
                purchase_url=value.purchase_url,
                supplier=value.supplier,
                manufacturer_part_number=value.manufacturer_part_number,
                application=value.application,
                reference_price=value.reference_price,
                note=value.note,
            )
            if value.initial_quantity > 0:
                if value.initial_staff_id is None:
                    raise RuntimeError("初期数量の担当者検証に失敗しました")
                self.movements.insert(
                    item_id=item.id,
                    client_id=item.client_id,
                    purchaser_id=item.purchaser_id,
                    staff_id=value.initial_staff_id,
                    reason=Reason.ADJUST,
                    delta=value.initial_quantity,
                    unit_price=item.reference_price,
                    used_for=None,
                    reversal_of=None,
                    note=None,
                    moved_at=utc_now_str(self.clock()),
                )
                self.items.add_quantity(item.id, value.initial_quantity)
            result = self.items.get(item.id)
            if result is None:
                raise RuntimeError("登録した品目を取得できません")
            return result

    def update_item(self, update: ItemUpdate) -> Item:
        value = _normalize_item_update(update)
        with transaction(self.conn):
            current = self._require_item(value.id)
            self._require_selectable_client(value.client_id, current.client_id)
            self._require_selectable_purchaser(value.purchaser_id, current.purchaser_id)
            _required_master(self.masters.get_category(value.category_id), "カテゴリ")
            if value.location_id is not None:
                _required_master(self.masters.get_location(value.location_id), "保管場所")
            return self.items.update(value)

    def deactivate_item(self, item_id: int) -> Item:
        with transaction(self.conn):
            self._require_item(item_id)
            return self.items.set_active(item_id, False)

    def reactivate_item(self, item_id: int) -> Item:
        with transaction(self.conn):
            self._require_item(item_id)
            return self.items.set_active(item_id, True)

    def get_item(self, item_id: int) -> Item | None:
        return self.items.get(item_id)

    def list_items(self, filter: ItemFilter) -> list[ItemRow]:
        return self.items.list(filter)

    def list_low_stock(self) -> list[ItemRow]:
        return self.items.list_low_stock()

    def get_purchase_info(self, item_id: int) -> PurchaseInfo:
        return self.items.get_purchase_info(item_id)

    def receive(
        self,
        item_id: int,
        staff_id: int,
        quantity: int,
        unit_price: int | None = None,
        update_reference_price: bool = False,
        note: str | None = None,
    ) -> StockMovement:
        actual_price = _optional_integer(unit_price, "入庫単価", 0)
        if type(update_reference_price) is not bool:
            raise ValidationError("参考価格の更新指定が正しくありません")
        return self._change_stock(
            item_id,
            staff_id,
            _integer(quantity, "数量", 1),
            Reason.IN,
            actual_price,
            None,
            note,
            update_reference_price,
        )

    def issue(
        self,
        item_id: int,
        staff_id: int,
        quantity: int,
        used_for: str,
        note: str | None = None,
    ) -> StockMovement:
        normalized_used_for = _required_text(used_for, "使用先")
        return self._change_stock(
            item_id,
            staff_id,
            -_integer(quantity, "数量", 1),
            Reason.OUT,
            None,
            normalized_used_for,
            note,
        )

    def return_to_supplier(
        self, item_id: int, staff_id: int, quantity: int, note: str | None = None
    ) -> StockMovement:
        return self._change_stock(
            item_id,
            staff_id,
            -_integer(quantity, "数量", 1),
            Reason.RETURN,
            None,
            None,
            note,
        )

    def dispose(
        self, item_id: int, staff_id: int, quantity: int, note: str | None = None
    ) -> StockMovement:
        return self._change_stock(
            item_id,
            staff_id,
            -_integer(quantity, "数量", 1),
            Reason.DISPOSE,
            None,
            None,
            note,
        )

    def stocktake(
        self,
        item_id: int,
        staff_id: int,
        actual_quantity: int,
        note: str | None = None,
    ) -> StockMovement:
        actual = _integer(actual_quantity, "棚卸後の数量", 0)
        normalized_note = _normalize_optional(note, "備考")
        with transaction(self.conn):
            item = self._require_active_item(item_id)
            staff = self._require_active_staff(staff_id)
            return self._record_movement(
                item,
                staff.id,
                Reason.ADJUST,
                actual - item.quantity,
                item.reference_price,
                None,
                normalized_note,
            )

    def reverse(self, movement_id: int, staff_id: int, note: str | None = None) -> StockMovement:
        normalized_note = _normalize_optional(note, "備考")
        with transaction(self.conn):
            original = self.movements.get(_integer(movement_id, "履歴 ID", 1))
            if original is None:
                raise ValidationError("履歴が見つかりません")
            if original.reversal_of is not None:
                raise ReversalNotAllowedError("取り消し行は取り消せません")
            if self.movements.is_reversed(original.id):
                raise AlreadyReversedError("この履歴はすでに取り消されています")
            if original.reason is Reason.ADJUST and original.delta == 0:
                raise ReversalNotAllowedError("差分 0 の棚卸履歴は取り消せません")
            item = self._require_active_item(original.item_id)
            staff = self._require_active_staff(staff_id)
            delta = -original.delta
            if item.quantity + delta < 0:
                raise NegativeStockError("取り消し後の在庫数が負になるため実行できません")
            return self._record_movement(
                item,
                staff.id,
                original.reason,
                delta,
                original.unit_price,
                original.used_for,
                normalized_note,
                client_id=original.client_id,
                purchaser_id=original.purchaser_id,
                reversal_of=original.id,
            )

    def list_history(self, item_id: int) -> list[MovementRow]:
        return self.movements.list_by_item(item_id)

    def list_all_history(self) -> list[MovementRow]:
        return self.movements.list_all()

    def reversal_block_reason(self, movement_id: int) -> str | None:
        original = self.movements.get(movement_id)
        if original is None:
            return "履歴が見つかりません"
        if original.reversal_of is not None:
            return "取り消し行は取り消せません"
        if self.movements.is_reversed(original.id):
            return "この履歴はすでに取り消されています"
        if original.reason is Reason.ADJUST and original.delta == 0:
            return "差分 0 の棚卸履歴は取り消せません"
        item = self.items.get(original.item_id)
        if item is None or not item.is_active:
            return "廃止品目の履歴は取り消せません"
        if item.quantity - original.delta < 0:
            return "取り消し後の在庫数が負になるため実行できません"
        return None

    def _change_stock(
        self,
        item_id: int,
        staff_id: int,
        delta: int,
        reason: Reason,
        unit_price: int | None,
        used_for: str | None,
        note: str | None,
        update_reference_price: bool = False,
    ) -> StockMovement:
        normalized_note = _normalize_optional(note, "備考")
        with transaction(self.conn):
            item = self._require_active_item(item_id)
            staff = self._require_active_staff(staff_id)
            if item.quantity + delta < 0:
                raise NegativeStockError("在庫数が負になるため実行できません")
            effective_price = item.reference_price if unit_price is None else unit_price
            if (
                reason is Reason.IN
                and update_reference_price
                and effective_price != item.reference_price
            ):
                self.conn.execute(
                    "UPDATE items SET reference_price = ? WHERE id = ?",
                    (effective_price, item.id),
                )
            return self._record_movement(
                item,
                staff.id,
                reason,
                delta,
                effective_price,
                used_for,
                normalized_note,
            )

    def _record_movement(
        self,
        item: Item,
        staff_id: int,
        reason: Reason,
        delta: int,
        unit_price: int | None,
        used_for: str | None,
        note: str | None,
        *,
        client_id: int | None = None,
        purchaser_id: int | None = None,
        reversal_of: int | None = None,
    ) -> StockMovement:
        movement = self.movements.insert(
            item_id=item.id,
            client_id=item.client_id if client_id is None else client_id,
            purchaser_id=item.purchaser_id if purchaser_id is None else purchaser_id,
            staff_id=staff_id,
            reason=reason,
            delta=delta,
            unit_price=unit_price,
            used_for=used_for,
            reversal_of=reversal_of,
            note=note,
            moved_at=utc_now_str(self.clock()),
        )
        self.items.add_quantity(item.id, delta)
        return movement

    def _require_item(self, item_id: int) -> Item:
        item = self.items.get(item_id)
        if item is None:
            raise ValidationError("品目が見つかりません")
        return item

    def _require_active_item(self, item_id: int) -> Item:
        item = self._require_item(_integer(item_id, "品目 ID", 1))
        if not item.is_active:
            raise InactiveItemError("廃止品目には在庫操作できません")
        return item

    def _require_active_staff(self, staff_id: int) -> Staff:
        return _active_master(self.masters.get_staff(_integer(staff_id, "担当者 ID", 1)), "担当者")

    def _require_selectable_client(self, client_id: int, current_id: int) -> None:
        client = self.masters.get_client(client_id)
        if client is None:
            raise ValidationError("クライアントが見つかりません")
        if client_id != current_id and not client.is_active:
            raise InactiveMasterError("無効化されたクライアントへ変更できません")

    def _require_selectable_purchaser(self, purchaser_id: int, current_id: int) -> None:
        purchaser = self.masters.get_purchaser(purchaser_id)
        if purchaser is None:
            raise ValidationError("発注主体が見つかりません")
        if purchaser_id != current_id and not purchaser.is_active:
            raise InactiveMasterError("無効化された発注主体へ変更できません")


class MasterService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repository = MasterRepository(conn)

    def add_client(self, name: str) -> Client:
        with transaction(self.conn):
            return self.repository.insert_client(_required_text(name, "クライアント名"))

    def add_purchaser(self, name: str) -> Purchaser:
        with transaction(self.conn):
            return self.repository.insert_purchaser(_required_text(name, "発注主体名"))

    def add_staff(self, name: str) -> Staff:
        with transaction(self.conn):
            return self.repository.insert_staff(_required_text(name, "担当者名"))

    def add_category(self, name: str, code_prefix: str, parent_id: int | None = None) -> Category:
        normalized_name = _required_text(name, "カテゴリ名")
        normalized_prefix = _prefix(code_prefix)
        normalized_parent_id = _optional_integer(parent_id, "親カテゴリ ID", 1)
        with transaction(self.conn):
            if normalized_parent_id is not None:
                _required_master(self.repository.get_category(normalized_parent_id), "親カテゴリ")
            return self.repository.insert_category(
                normalized_name, normalized_prefix, normalized_parent_id
            )

    def add_location(self, name: str) -> Location:
        with transaction(self.conn):
            return self.repository.insert_location(_required_text(name, "保管場所名"))

    def rename_client(self, entity_id: int, name: str) -> Client:
        with transaction(self.conn):
            return self.repository.update_client_name(
                _integer(entity_id, "クライアント ID", 1), _required_text(name, "クライアント名")
            )

    def rename_purchaser(self, entity_id: int, name: str) -> Purchaser:
        with transaction(self.conn):
            return self.repository.update_purchaser_name(
                _integer(entity_id, "発注主体 ID", 1), _required_text(name, "発注主体名")
            )

    def rename_staff(self, entity_id: int, name: str) -> Staff:
        with transaction(self.conn):
            return self.repository.update_staff_name(
                _integer(entity_id, "担当者 ID", 1), _required_text(name, "担当者名")
            )

    def rename_category(self, entity_id: int, name: str) -> Category:
        with transaction(self.conn):
            return self.repository.update_category_name(
                _integer(entity_id, "カテゴリ ID", 1), _required_text(name, "カテゴリ名")
            )

    def move_category(self, entity_id: int, parent_id: int | None) -> Category:
        category_id = _integer(entity_id, "カテゴリ ID", 1)
        new_parent_id = _optional_integer(parent_id, "親カテゴリ ID", 1)
        with transaction(self.conn):
            _required_master(self.repository.get_category(category_id), "カテゴリ")
            if new_parent_id is not None:
                _required_master(self.repository.get_category(new_parent_id), "親カテゴリ")
                if new_parent_id in self.repository.descendant_ids(category_id):
                    raise CategoryCycleError("自身または子孫を親カテゴリに指定できません")
            return self.repository.update_category_parent(category_id, new_parent_id)

    def change_category_prefix(self, entity_id: int, code_prefix: str) -> Category:
        category_id = _integer(entity_id, "カテゴリ ID", 1)
        normalized_prefix = _prefix(code_prefix)
        with transaction(self.conn):
            category = self.repository.get_category(category_id)
            if category is None:
                raise ValidationError("カテゴリが見つかりません")
            if category.next_seq > 1 and normalized_prefix != category.code_prefix:
                raise PrefixLockedError("採番実績のあるカテゴリの接頭辞は変更できません")
            return self.repository.update_category_prefix(category_id, normalized_prefix)

    def rename_location(self, entity_id: int, name: str) -> Location:
        with transaction(self.conn):
            return self.repository.update_location_name(
                _integer(entity_id, "保管場所 ID", 1), _required_text(name, "保管場所名")
            )

    def deactivate_client(self, entity_id: int) -> Client:
        return cast(Client, self._set_active("client", entity_id, False))

    def reactivate_client(self, entity_id: int) -> Client:
        return cast(Client, self._set_active("client", entity_id, True))

    def deactivate_purchaser(self, entity_id: int) -> Purchaser:
        return cast(Purchaser, self._set_active("purchaser", entity_id, False))

    def reactivate_purchaser(self, entity_id: int) -> Purchaser:
        return cast(Purchaser, self._set_active("purchaser", entity_id, True))

    def deactivate_staff(self, entity_id: int) -> Staff:
        return cast(Staff, self._set_active("staff", entity_id, False))

    def reactivate_staff(self, entity_id: int) -> Staff:
        return cast(Staff, self._set_active("staff", entity_id, True))

    def delete_client(self, entity_id: int) -> None:
        self._delete("client", entity_id)

    def delete_purchaser(self, entity_id: int) -> None:
        self._delete("purchaser", entity_id)

    def delete_staff(self, entity_id: int) -> None:
        self._delete("staff", entity_id)

    def delete_category(self, entity_id: int) -> None:
        self._delete("category", entity_id)

    def delete_location(self, entity_id: int) -> None:
        self._delete("location", entity_id)

    def list_clients(self, include_inactive: bool = False) -> list[Client]:
        return self.repository.list_clients(include_inactive)

    def list_purchasers(self, include_inactive: bool = False) -> list[Purchaser]:
        return self.repository.list_purchasers(include_inactive)

    def list_staff(self, include_inactive: bool = False) -> list[Staff]:
        return self.repository.list_staff(include_inactive)

    def list_categories(self) -> list[Category]:
        return self.repository.list_categories()

    def list_locations(self) -> list[Location]:
        return self.repository.list_locations()

    def client_in_use(self, entity_id: int) -> bool:
        return self.repository.client_in_use(entity_id)

    def purchaser_in_use(self, entity_id: int) -> bool:
        return self.repository.purchaser_in_use(entity_id)

    def staff_in_use(self, entity_id: int) -> bool:
        return self.repository.staff_in_use(entity_id)

    def category_in_use(self, entity_id: int) -> bool:
        return self.repository.category_in_use(entity_id)

    def location_in_use(self, entity_id: int) -> bool:
        return self.repository.location_in_use(entity_id)

    def can_delete_client(self, entity_id: int) -> bool:
        return self._exists("client", entity_id) and not self.client_in_use(entity_id)

    def can_delete_purchaser(self, entity_id: int) -> bool:
        return self._exists("purchaser", entity_id) and not self.purchaser_in_use(entity_id)

    def can_delete_staff(self, entity_id: int) -> bool:
        return self._exists("staff", entity_id) and not self.staff_in_use(entity_id)

    def can_delete_category(self, entity_id: int) -> bool:
        return self._exists("category", entity_id) and not self.category_in_use(entity_id)

    def can_delete_location(self, entity_id: int) -> bool:
        return self._exists("location", entity_id) and not self.location_in_use(entity_id)

    def _set_active(self, kind: str, entity_id: int, is_active: bool) -> Client | Purchaser | Staff:
        normalized_id = _integer(entity_id, "マスタ ID", 1)
        with transaction(self.conn):
            if kind == "client":
                return self.repository.set_client_active(normalized_id, is_active)
            if kind == "purchaser":
                return self.repository.set_purchaser_active(normalized_id, is_active)
            return self.repository.set_staff_active(normalized_id, is_active)

    def _delete(self, kind: str, entity_id: int) -> None:
        normalized_id = _integer(entity_id, "マスタ ID", 1)
        with transaction(self.conn):
            if not self._exists(kind, normalized_id):
                raise ValidationError("マスタが見つかりません")
            if self._in_use(kind, normalized_id):
                raise MasterInUseError("使用中または保持が必要なマスタは削除できません")
            if kind == "client":
                self.repository.delete_client(normalized_id)
            elif kind == "purchaser":
                self.repository.delete_purchaser(normalized_id)
            elif kind == "staff":
                self.repository.delete_staff(normalized_id)
            elif kind == "category":
                self.repository.delete_category(normalized_id)
            else:
                self.repository.delete_location(normalized_id)

    def _exists(self, kind: str, entity_id: int) -> bool:
        if kind == "client":
            return self.repository.get_client(entity_id) is not None
        if kind == "purchaser":
            return self.repository.get_purchaser(entity_id) is not None
        if kind == "staff":
            return self.repository.get_staff(entity_id) is not None
        if kind == "category":
            return self.repository.get_category(entity_id) is not None
        return self.repository.get_location(entity_id) is not None

    def _in_use(self, kind: str, entity_id: int) -> bool:
        if kind == "client":
            return self.client_in_use(entity_id)
        if kind == "purchaser":
            return self.purchaser_in_use(entity_id)
        if kind == "staff":
            return self.staff_in_use(entity_id)
        if kind == "category":
            return self.category_in_use(entity_id)
        return self.location_in_use(entity_id)


class SettingsService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repository = SettingsRepository(conn)

    def get_fiscal_year_start_month(self) -> int:
        value = self.repository.get(_FISCAL_YEAR_KEY)
        if value is None:
            raise ValidationError("年度開始月の設定がありません")
        try:
            month = int(value)
        except ValueError as error:
            raise ValidationError("年度開始月の設定が不正です") from error
        if not 1 <= month <= 12 or str(month) != value:
            raise ValidationError("年度開始月の設定が不正です")
        return month

    def set_fiscal_year_start_month(self, month: int) -> None:
        normalized_month = _integer(month, "年度開始月", 1)
        if normalized_month > 12:
            raise ValidationError("年度開始月は 1〜12 で指定してください")
        with transaction(self.conn):
            self.repository.set(_FISCAL_YEAR_KEY, str(normalized_month))

    def validate_all(self) -> list[str]:
        values = self.repository.all()
        reasons: list[str] = []
        value = values.get(_FISCAL_YEAR_KEY)
        if value is None:
            reasons.append("年度開始月の設定がありません")
        else:
            try:
                month = int(value)
            except ValueError:
                reasons.append("年度開始月は正規の整数表記で指定してください")
            else:
                if not 1 <= month <= 12 or str(month) != value:
                    reasons.append("年度開始月は 1〜12 の正規の整数表記で指定してください")
        unknown = sorted(set(values) - {_FISCAL_YEAR_KEY})
        if unknown:
            reasons.append("未知の設定キーがあります: " + ", ".join(unknown))
        return reasons
