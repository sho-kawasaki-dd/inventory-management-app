import pytest

from inventory_manager_mini.ui.printing.layout import (
    LABEL_72212,
    LabelEntry,
    LabelPlacement,
    LabelSpec,
    assign_label_slots,
    slot_rect_mm,
)


def test_assigns_all_slots_in_row_major_order() -> None:
    placements = assign_label_slots([LabelEntry(item_id=10, copies=13)], range(12))

    assert placements[:12] == [
        LabelPlacement(page_number=1, slot=slot, item_id=10) for slot in range(12)
    ]
    assert placements[12] == LabelPlacement(page_number=2, slot=0, item_id=10)


def test_assigns_selected_first_page_slots_then_full_following_pages() -> None:
    placements = assign_label_slots(
        [LabelEntry(item_id=1, copies=2), LabelEntry(item_id=2, copies=3)],
        [4, 1],
    )

    assert placements == [
        LabelPlacement(page_number=1, slot=1, item_id=1),
        LabelPlacement(page_number=1, slot=4, item_id=1),
        LabelPlacement(page_number=2, slot=0, item_id=2),
        LabelPlacement(page_number=2, slot=1, item_id=2),
        LabelPlacement(page_number=2, slot=2, item_id=2),
    ]


def test_overflow_fills_following_page_from_first_slot() -> None:
    placements = assign_label_slots([LabelEntry(item_id=1, copies=14)], [2, 5])

    assert [placement.slot for placement in placements[:2]] == [2, 5]
    assert [placement.slot for placement in placements[2:]] == list(range(12))
    assert {placement.page_number for placement in placements[2:]} == {2}


def test_assigns_entries_in_order_and_expands_each_entry() -> None:
    placements = assign_label_slots(
        [LabelEntry(item_id=7, copies=2), LabelEntry(item_id=3, copies=1)], [0, 2, 5]
    )

    assert [placement.item_id for placement in placements] == [7, 7, 3]
    assert [placement.slot for placement in placements] == [0, 2, 5]


@pytest.mark.parametrize("copies", [1, 99])
def test_accepts_copy_count_boundaries(copies: int) -> None:
    placements = assign_label_slots([LabelEntry(item_id=1, copies=copies)], range(12))

    assert len(placements) == copies
    assert placements[0] == LabelPlacement(page_number=1, slot=0, item_id=1)
    assert placements[-1].page_number == (copies - 1) // 12 + 1
    assert placements[-1].slot == (copies - 1) % 12


@pytest.mark.parametrize("copies", [0, 100, True])
def test_rejects_copy_count_outside_range(copies: int) -> None:
    with pytest.raises(ValueError, match="枚数"):
        assign_label_slots([LabelEntry(item_id=1, copies=copies)], range(12))


@pytest.mark.parametrize("slots", [[], [-1], [12], [0, 12]])
def test_rejects_empty_or_out_of_range_first_page_slots(slots: list[int]) -> None:
    with pytest.raises(ValueError, match="印字位置"):
        assign_label_slots([LabelEntry(item_id=1, copies=1)], slots)


def test_empty_entries_return_no_placements() -> None:
    assert assign_label_slots([], range(12)) == []


def test_slot_rectangles_match_label_spec_for_all_slots() -> None:
    for slot in range(LABEL_72212.columns * LABEL_72212.rows):
        row, column = divmod(slot, LABEL_72212.columns)
        assert slot_rect_mm(LABEL_72212, slot) == (
            LABEL_72212.left_margin_mm + column * LABEL_72212.horizontal_pitch_mm,
            LABEL_72212.top_margin_mm + row * LABEL_72212.vertical_pitch_mm,
            LABEL_72212.label_width_mm,
            LABEL_72212.label_height_mm,
        )


@pytest.mark.parametrize(
    ("slot", "offset_left_mm", "offset_top_mm"),
    [(0, 1.5, -2.0), (11, -3.0, 4.5)],
)
def test_slot_rect_applies_offsets(slot: int, offset_left_mm: float, offset_top_mm: float) -> None:
    x, y, width, height = slot_rect_mm(
        LABEL_72212, slot, offset_left_mm=offset_left_mm, offset_top_mm=offset_top_mm
    )
    base_x, base_y, _, _ = slot_rect_mm(LABEL_72212, slot)

    assert (x, y, width, height) == (
        base_x + offset_left_mm,
        base_y + offset_top_mm,
        LABEL_72212.label_width_mm,
        LABEL_72212.label_height_mm,
    )


def test_label_spec_fits_all_twelve_labels_on_a4() -> None:
    assert LABEL_72212.columns == 2
    assert LABEL_72212.rows == 6
    assert all(
        x >= 0 and y >= 0 and x + width <= 210 and y + height <= 297
        for x, y, width, height in (
            slot_rect_mm(LABEL_72212, slot)
            for slot in range(LABEL_72212.columns * LABEL_72212.rows)
        )
    )


def test_slot_rect_rejects_out_of_range_slot() -> None:
    spec = LabelSpec(
        columns=2,
        rows=6,
        label_width_mm=86.4,
        label_height_mm=42.3,
        left_margin_mm=18.6,
        top_margin_mm=21.6,
        horizontal_pitch_mm=86.4,
        vertical_pitch_mm=42.3,
    )

    with pytest.raises(ValueError, match="印字位置"):
        slot_rect_mm(spec, 12)
