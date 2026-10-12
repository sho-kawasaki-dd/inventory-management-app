from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import floor
from typing import Literal

from inventory_manager_mini.core.models import ItemFilter, ItemRow

INVENTORY_SHEET_PAPER_WIDTH_MM = 297.0
INVENTORY_SHEET_PAPER_HEIGHT_MM = 210.0
INVENTORY_SHEET_MARGIN_MM = 10.0
INVENTORY_SHEET_PRINTABLE_WIDTH_MM = INVENTORY_SHEET_PAPER_WIDTH_MM - 2 * INVENTORY_SHEET_MARGIN_MM
INVENTORY_SHEET_TITLE_HEIGHT_MM = 25.0
INVENTORY_SHEET_HEADER_HEIGHT_MM = 8.0
INVENTORY_SHEET_ROW_HEIGHT_MM = 7.0
INVENTORY_SHEET_FOOTER_HEIGHT_MM = 8.0


def _rows_per_inventory_sheet_page(title_height_mm: float) -> int:
    available_height_mm = (
        INVENTORY_SHEET_PAPER_HEIGHT_MM
        - 2 * INVENTORY_SHEET_MARGIN_MM
        - title_height_mm
        - INVENTORY_SHEET_HEADER_HEIGHT_MM
        - INVENTORY_SHEET_FOOTER_HEIGHT_MM
    )
    return floor(available_height_mm / INVENTORY_SHEET_ROW_HEIGHT_MM)


INVENTORY_SHEET_FIRST_PAGE_ROWS = _rows_per_inventory_sheet_page(INVENTORY_SHEET_TITLE_HEIGHT_MM)
INVENTORY_SHEET_OTHER_PAGE_ROWS = _rows_per_inventory_sheet_page(0.0)


def format_inventory_text(value: str | None) -> str:
    return value or ""


def format_inventory_number(value: int | None) -> str:
    return "" if value is None else f"{value:,}"


def format_inventory_low_stock(value: bool) -> str:
    return "○" if value else ""


def format_inventory_item_name(name: str | None, is_active: bool) -> str:
    formatted_name = format_inventory_text(name)
    return formatted_name if is_active or not formatted_name else f"{formatted_name} (廃止)"


InventorySheetAlignment = Literal["left", "right", "center"]


@dataclass(frozen=True, slots=True)
class InventorySheetColumn:
    heading: str
    width_mm: float
    alignment: InventorySheetAlignment
    value_getter: Callable[[ItemRow], str]


INVENTORY_SHEET_COLUMNS = (
    InventorySheetColumn("管理番号", 22.0, "left", lambda item: item.code),
    InventorySheetColumn(
        "品名", 46.0, "left", lambda item: format_inventory_item_name(item.name, item.is_active)
    ),
    InventorySheetColumn(
        "メーカー型番",
        32.0,
        "left",
        lambda item: format_inventory_text(item.manufacturer_part_number),
    ),
    InventorySheetColumn(
        "用途", 32.0, "left", lambda item: format_inventory_text(item.application)
    ),
    InventorySheetColumn("カテゴリ", 36.0, "left", lambda item: item.category_path),
    InventorySheetColumn(
        "保管場所", 26.0, "left", lambda item: format_inventory_text(item.location_name)
    ),
    InventorySheetColumn(
        "数量", 13.0, "right", lambda item: format_inventory_number(item.quantity)
    ),
    InventorySheetColumn("単位", 9.0, "left", lambda item: item.unit),
    InventorySheetColumn(
        "閾値", 13.0, "right", lambda item: format_inventory_number(item.reorder_threshold)
    ),
    InventorySheetColumn(
        "推奨発注数",
        17.0,
        "right",
        lambda item: format_inventory_number(item.reorder_quantity),
    ),
    InventorySheetColumn(
        "低在庫", 12.0, "center", lambda item: format_inventory_low_stock(item.is_low_stock)
    ),
    InventorySheetColumn("発注主体", 19.0, "left", lambda item: item.purchaser_name),
)


def summarize_item_filter(
    item_filter: ItemFilter,
    *,
    client_name: str | None = None,
    purchaser_name: str | None = None,
    category_path: str | None = None,
    location_name: str | None = None,
) -> str:
    parts: list[str] = []
    if item_filter.text:
        parts.append(f"検索: {item_filter.text}")
    for label, entity_id, resolved_name in (
        ("クライアント", item_filter.client_id, client_name),
        ("発注主体", item_filter.purchaser_id, purchaser_name),
        ("カテゴリ", item_filter.category_id, category_path),
        ("保管場所", item_filter.location_id, location_name),
    ):
        if entity_id is not None:
            parts.append(f"{label}: {resolved_name or f'ID {entity_id}'}")
    if item_filter.low_stock_only:
        parts.append("低在庫のみ")
    if item_filter.include_inactive:
        parts.append("廃止品目を含む")
    return " / ".join(parts) if parts else "(絞り込みなし)"


def paginate(row_count: int, first_page_rows: int, other_page_rows: int) -> list[range]:
    if row_count < 0 or first_page_rows <= 0 or other_page_rows <= 0:
        raise ValueError("行数とページあたりの行数が不正です")
    pages: list[range] = []
    start = 0
    while start < row_count:
        page_capacity = first_page_rows if start == 0 else other_page_rows
        end = min(start + page_capacity, row_count)
        pages.append(range(start, end))
        start = end
    return pages


@dataclass(frozen=True, slots=True)
class LabelSpec:
    columns: int
    rows: int
    label_width_mm: float
    label_height_mm: float
    left_margin_mm: float
    top_margin_mm: float
    horizontal_pitch_mm: float
    vertical_pitch_mm: float


# エーワン 72212 の寸法は実測前の推定値。
LABEL_72212 = LabelSpec(
    columns=2,
    rows=6,
    label_width_mm=86.4,
    label_height_mm=42.3,
    left_margin_mm=18.6,
    top_margin_mm=21.6,
    horizontal_pitch_mm=86.4,
    vertical_pitch_mm=42.3,
)


@dataclass(frozen=True, slots=True)
class LabelEntry:
    item_id: int
    copies: int


@dataclass(frozen=True, slots=True)
class LabelPlacement:
    page_number: int
    slot: int
    item_id: int


def assign_label_slots(
    entries: Iterable[LabelEntry],
    first_page_slots: Iterable[int],
    spec: LabelSpec = LABEL_72212,
) -> list[LabelPlacement]:
    capacity = spec.columns * spec.rows
    slots = tuple(first_page_slots)
    if not slots:
        raise ValueError("最初の用紙には印字位置が必要です")
    if any(not isinstance(slot, int) or isinstance(slot, bool) for slot in slots):
        raise ValueError("印字位置は整数で指定してください")
    if any(slot < 0 or slot >= capacity for slot in slots):
        raise ValueError("印字位置が範囲外です")
    first_page_slots_sorted = sorted(set(slots))

    placements: list[LabelPlacement] = []
    label_index = 0
    first_page_count = len(first_page_slots_sorted)
    for entry in entries:
        if (
            not isinstance(entry.copies, int)
            or isinstance(entry.copies, bool)
            or not 1 <= entry.copies <= 99
        ):
            raise ValueError("枚数は1〜99で指定してください")
        for _ in range(entry.copies):
            if label_index < first_page_count:
                page_number = 1
                slot = first_page_slots_sorted[label_index]
            else:
                overflow_index = label_index - first_page_count
                page_number = 2 + overflow_index // capacity
                slot = overflow_index % capacity
            placements.append(
                LabelPlacement(page_number=page_number, slot=slot, item_id=entry.item_id)
            )
            label_index += 1

    return placements


def slot_rect_mm(
    spec: LabelSpec,
    slot: int,
    offset_left_mm: float = 0.0,
    offset_top_mm: float = 0.0,
) -> tuple[float, float, float, float]:
    capacity = spec.columns * spec.rows
    if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < capacity:
        raise ValueError("印字位置が範囲外です")
    row, column = divmod(slot, spec.columns)
    return (
        spec.left_margin_mm + column * spec.horizontal_pitch_mm + offset_left_mm,
        spec.top_margin_mm + row * spec.vertical_pitch_mm + offset_top_mm,
        spec.label_width_mm,
        spec.label_height_mm,
    )
