from collections.abc import Iterable
from dataclasses import dataclass


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
