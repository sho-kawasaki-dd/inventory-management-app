from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Reason(StrEnum):
    IN = "in"
    OUT = "out"
    RETURN = "return"
    DISPOSE = "dispose"
    ADJUST = "adjust"


REASON_LABELS: dict[Reason, str] = {
    Reason.IN: "入庫",
    Reason.OUT: "出庫",
    Reason.RETURN: "返品",
    Reason.DISPOSE: "廃棄",
    Reason.ADJUST: "棚卸",
}

MAX_STOCK_QUANTITY = 1_000_000
MAX_UNIT_PRICE = 10_000_000
MAX_MOVEMENT_AMOUNT = 100_000_000
MAX_AGGREGATE_VALUE = 1_000_000_000_000

HISTORY_PAGE_SIZES: tuple[int, int] = (300, 500)
DEFAULT_HISTORY_PAGE_SIZE: int = 300


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
    cost_amount: int | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class Lot:
    id: int
    moved_at: str
    remaining_quantity: int
    unit_price: int | None
    purchaser_id: int


@dataclass(frozen=True, slots=True)
class Allocation:
    lot_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class FifoEstimate:
    cost_amount: int
    unpriced_quantity: int


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


class GroupAxis(StrEnum):
    CLIENT = "client"
    PURCHASER = "purchaser"
    CATEGORY = "category"
    ITEM = "item"


@dataclass(frozen=True, slots=True)
class ItemChoice:
    id: int
    code: str
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class DashboardRow:
    period_label: str
    period_start: date
    client_id: int | None
    client_name: str | None
    purchaser_id: int | None
    purchaser_name: str | None
    category_id: int | None
    category_name: str | None
    item_id: int | None
    item_code: str | None
    item_name: str | None
    inbound_quantity: int
    outbound_quantity: int
    expenditure: int
    unpriced_issue_quantity: int
    disposed_quantity: int
    disposal_amount: int


@dataclass(frozen=True, slots=True)
class DashboardResult:
    totals: tuple[DashboardRow, ...]
    breakdown: tuple[DashboardRow, ...]


class HistoryPeriodKind(StrEnum):
    ALL = "all"
    ANNUAL = "annual"
    MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class HistoryPeriod:
    kind: HistoryPeriodKind = HistoryPeriodKind.ALL
    fiscal_year: int | None = None
    month: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryPage:
    rows: tuple[MovementRow, ...]
    total_count: int
    page: int
    page_size: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class ActiveStocktake:
    client_id: int
    client_name: str
    session_id: str
    staff_id: int
    staff_name: str
    started_at: str
    item_count: int
    category_id: int | None = None
    category_path: str | None = None


@dataclass(frozen=True, slots=True)
class MobileStocktakeItem:
    item_id: int
    code: str
    name: str
    manufacturer_part_number: str | None
    location_name: str | None
    book_quantity: int
    unit: str


@dataclass(frozen=True, slots=True)
class MobileStocktakeSheet:
    session_id: str
    client_id: int
    client_name: str
    staff_id: int
    staff_name: str
    started_at: str
    items: tuple[MobileStocktakeItem, ...]
    category_id: int | None = None
    category_path: str | None = None


@dataclass(frozen=True, slots=True)
class MobileStocktakeEntry:
    item_id: int
    actual_quantity: int
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MobileStocktakePayload:
    session_id: str
    client_id: int
    entries: tuple[MobileStocktakeEntry, ...]


@dataclass(frozen=True, slots=True)
class MobileStocktakePreviewRow:
    item_id: int
    code: str
    name: str
    location_name: str | None
    book_quantity: int
    actual_quantity: int
    delta: int
    note: str | None


@dataclass(frozen=True, slots=True)
class MobileStocktakePreview:
    session_id: str
    client_id: int
    client_name: str
    staff_id: int
    staff_name: str
    started_at: str
    rows: tuple[MobileStocktakePreviewRow, ...]
    unchanged_count: int
    increased_count: int
    decreased_count: int
    category_id: int | None = None
    category_path: str | None = None
