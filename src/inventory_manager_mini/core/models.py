from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Reason(StrEnum):
    IN = "in"
    OUT = "out"
    RETURN = "return"
    DISPOSE = "dispose"
    ADJUST = "adjust"


@dataclass(frozen=True, slots=True)
class Client:
    id: int
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class Purchaser:
    id: int
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class Staff:
    id: int
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class Category:
    id: int
    parent_id: int | None
    name: str
    code_prefix: str
    next_seq: int


@dataclass(frozen=True, slots=True)
class Location:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    client_id: int
    purchaser_id: int
    code: str
    name: str
    category_id: int
    location_id: int | None
    unit: str
    quantity: int
    reorder_threshold: int
    reorder_quantity: int | None
    purchase_url: str | None
    supplier: str | None
    manufacturer_part_number: str | None
    application: str | None
    reference_price: int | None
    note: str | None
    is_active: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StockMovement:
    id: int
    item_id: int
    client_id: int
    purchaser_id: int
    staff_id: int
    reason: Reason
    delta: int
    unit_price: int | None
    used_for: str | None
    reversal_of: int | None
    note: str | None
    moved_at: str


@dataclass(frozen=True, slots=True)
class NewItem:
    client_id: int
    purchaser_id: int
    name: str
    category_id: int
    location_id: int | None = None
    initial_quantity: int = 0
    initial_staff_id: int | None = None
    reorder_threshold: int = 0
    reorder_quantity: int | None = None
    purchase_url: str | None = None
    supplier: str | None = None
    manufacturer_part_number: str | None = None
    application: str | None = None
    reference_price: int | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ItemUpdate:
    id: int
    client_id: int
    purchaser_id: int
    name: str
    category_id: int
    location_id: int | None
    reorder_threshold: int
    reorder_quantity: int | None
    purchase_url: str | None
    supplier: str | None
    manufacturer_part_number: str | None
    application: str | None
    reference_price: int | None
    note: str | None


@dataclass(frozen=True, slots=True)
class PurchaseInfo:
    last_purchased_at: str | None
    lot_quantity: int | None


@dataclass(frozen=True, slots=True)
class ItemRow(Item):
    client_name: str
    purchaser_name: str
    category_path: str
    location_name: str | None
    is_low_stock: bool
    purchase_info: PurchaseInfo


@dataclass(frozen=True, slots=True)
class MovementRow(StockMovement):
    code: str
    item_name: str
    client_name: str
    purchaser_name: str
    staff_name: str
    is_reversed: bool


@dataclass(frozen=True, slots=True)
class ItemFilter:
    text: str | None = None
    client_id: int | None = None
    purchaser_id: int | None = None
    category_id: int | None = None
    location_id: int | None = None
    low_stock_only: bool = False
    include_inactive: bool = False


class PeriodKind(StrEnum):
    ANNUAL = "annual"
    MONTHLY = "monthly"


class GroupBy(StrEnum):
    NONE = "none"
    CLIENT = "client"
    PURCHASER = "purchaser"
    CLIENT_PURCHASER = "client_purchaser"


@dataclass(frozen=True, slots=True)
class DashboardRow:
    period_label: str
    period_start: date
    client_id: int | None
    client_name: str | None
    purchaser_id: int | None
    purchaser_name: str | None
    inbound_quantity: int
    outbound_quantity: int
    expenditure: int
    unpriced_issue_count: int
    disposed_quantity: int
    disposal_amount: int
