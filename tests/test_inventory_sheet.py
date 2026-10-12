from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter
from PySide6.QtPdf import QPdfDocument

from inventory_manager_mini.core.models import ItemFilter, ItemRow, PurchaseInfo
from inventory_manager_mini.ui.printing.inventory_sheet_renderer import render_inventory_sheet
from inventory_manager_mini.ui.printing.layout import (
    INVENTORY_SHEET_COLUMNS,
    INVENTORY_SHEET_FIRST_PAGE_ROWS,
    INVENTORY_SHEET_FOOTER_HEIGHT_MM,
    INVENTORY_SHEET_HEADER_HEIGHT_MM,
    INVENTORY_SHEET_MARGIN_MM,
    INVENTORY_SHEET_OTHER_PAGE_ROWS,
    INVENTORY_SHEET_PAPER_HEIGHT_MM,
    INVENTORY_SHEET_PRINTABLE_WIDTH_MM,
    INVENTORY_SHEET_ROW_HEIGHT_MM,
    INVENTORY_SHEET_SPEC,
    INVENTORY_SHEET_TITLE_HEIGHT_MM,
    format_inventory_item_name,
    format_inventory_low_stock,
    format_inventory_number,
    format_inventory_text,
    paginate,
    summarize_item_filter,
)


def make_item_row() -> ItemRow:
    return ItemRow(
        id=1,
        client_id=2,
        purchaser_id=3,
        code="TOOL-001",
        name="ドライバー",
        category_id=4,
        location_id=None,
        unit="本",
        quantity=1200,
        reorder_threshold=1000,
        reorder_quantity=None,
        purchase_url=None,
        supplier=None,
        manufacturer_part_number="DRV-01",
        application="組立用",
        reference_price=None,
        note=None,
        is_active=True,
        created_at="2026-01-01 00:00:00",
        updated_at="2026-01-01 00:00:00",
        client_name="総務",
        purchaser_name="備品費",
        category_path="工具 / ドライバー",
        location_name=None,
        is_low_stock=True,
        purchase_info=PurchaseInfo(last_purchased_at=None, lot_quantity=None),
    )


def _inventory_sheet_pdf_writer(path: Path) -> QPdfWriter:
    writer = QPdfWriter(str(path))
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Landscape,
        QMarginsF(0, 0, 0, 0),
    )
    assert writer.setPageLayout(layout)
    writer.setResolution(300)
    return writer


def _pdf_page_count(path: Path) -> int:
    document = QPdfDocument()
    assert document.load(str(path)) == QPdfDocument.Error.None_
    return document.pageCount()


def test_inventory_sheet_columns_have_expected_order_and_total_width() -> None:
    assert [column.heading for column in INVENTORY_SHEET_COLUMNS] == [
        "管理番号",
        "品名",
        "メーカー型番",
        "用途",
        "カテゴリ",
        "保管場所",
        "数量",
        "単位",
        "閾値",
        "推奨発注数",
        "低在庫",
        "発注主体",
    ]
    assert sum(column.width_mm for column in INVENTORY_SHEET_COLUMNS) == (
        INVENTORY_SHEET_PRINTABLE_WIDTH_MM
    )
    assert INVENTORY_SHEET_PRINTABLE_WIDTH_MM == 277.0
    assert [column.width_mm for column in INVENTORY_SHEET_COLUMNS] == [
        22,
        46,
        32,
        32,
        36,
        26,
        13,
        9,
        13,
        17,
        12,
        19,
    ]
    assert [column.alignment for column in INVENTORY_SHEET_COLUMNS[6:10]] == [
        "right",
        "left",
        "right",
        "right",
    ]
    assert INVENTORY_SHEET_COLUMNS[10].alignment == "center"


def test_inventory_sheet_column_getters_return_formatted_row_values() -> None:
    row = make_item_row()

    assert [column.value_getter(row) for column in INVENTORY_SHEET_COLUMNS] == [
        "TOOL-001",
        "ドライバー",
        "DRV-01",
        "組立用",
        "工具 / ドライバー",
        "",
        "1,200",
        "本",
        "1,000",
        "",
        "○",
        "備品費",
    ]
    inactive_row = replace(row, is_active=False)
    assert INVENTORY_SHEET_COLUMNS[1].value_getter(inactive_row) == "ドライバー (廃止)"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (1234567, "1,234,567"), (None, "")],
)
def test_formats_inventory_numbers(value: int | None, expected: str) -> None:
    assert format_inventory_number(value) == expected


@pytest.mark.parametrize(("value", "expected"), [(True, "○"), (False, "")])
def test_formats_low_stock(value: bool, expected: str) -> None:
    assert format_inventory_low_stock(value) == expected


def test_formats_empty_optional_values_and_inactive_item_names() -> None:
    assert format_inventory_text(None) == ""
    assert format_inventory_item_name("品目", True) == "品目"
    assert format_inventory_item_name("品目", False) == "品目 (廃止)"
    assert format_inventory_item_name(None, False) == ""


def test_summarizes_empty_item_filter() -> None:
    assert summarize_item_filter(ItemFilter()) == "(絞り込みなし)"


def test_summarizes_all_item_filter_conditions_in_order() -> None:
    item_filter = ItemFilter(
        text="ドライバー",
        client_id=2,
        purchaser_id=3,
        category_id=4,
        location_id=5,
        low_stock_only=True,
        include_inactive=True,
    )

    assert summarize_item_filter(
        item_filter,
        client_name="総務",
        purchaser_name="備品費",
        category_path="工具 / ドライバー",
        location_name="倉庫 A",
    ) == (
        "検索: ドライバー / クライアント: 総務 / 発注主体: 備品費 / "
        "カテゴリ: 工具 / ドライバー / 保管場所: 倉庫 A / 低在庫のみ / 廃止品目を含む"
    )


def test_summarizes_selected_master_ids_when_names_are_unavailable() -> None:
    assert summarize_item_filter(ItemFilter(client_id=12, location_id=34)) == (
        "クライアント: ID 12 / 保管場所: ID 34"
    )


def test_inventory_sheet_page_capacities_come_from_fixed_mm_dimensions() -> None:
    first_page_height = (
        INVENTORY_SHEET_PAPER_HEIGHT_MM
        - 2 * INVENTORY_SHEET_MARGIN_MM
        - INVENTORY_SHEET_TITLE_HEIGHT_MM
        - INVENTORY_SHEET_HEADER_HEIGHT_MM
        - INVENTORY_SHEET_FOOTER_HEIGHT_MM
    )
    other_page_height = (
        INVENTORY_SHEET_PAPER_HEIGHT_MM
        - 2 * INVENTORY_SHEET_MARGIN_MM
        - INVENTORY_SHEET_HEADER_HEIGHT_MM
        - INVENTORY_SHEET_FOOTER_HEIGHT_MM
    )

    assert int(first_page_height // INVENTORY_SHEET_ROW_HEIGHT_MM) == (
        INVENTORY_SHEET_FIRST_PAGE_ROWS
    )
    assert int(other_page_height // INVENTORY_SHEET_ROW_HEIGHT_MM) == (
        INVENTORY_SHEET_OTHER_PAGE_ROWS
    )
    assert INVENTORY_SHEET_FIRST_PAGE_ROWS < INVENTORY_SHEET_OTHER_PAGE_ROWS


@pytest.mark.parametrize(
    ("row_count", "expected"),
    [
        (0, []),
        (3, [range(0, 3)]),
        (4, [range(0, 3), range(3, 4)]),
        (8, [range(0, 3), range(3, 5), range(5, 7), range(7, 8)]),
        (2, [range(0, 2)]),
    ],
)
def test_paginate_uses_first_page_and_following_page_capacities(
    row_count: int, expected: list[range]
) -> None:
    assert paginate(row_count, first_page_rows=3, other_page_rows=2) == expected


@pytest.mark.parametrize(
    ("row_count", "first_page_rows", "other_page_rows"),
    [(-1, 3, 2), (1, 0, 2), (1, 3, 0)],
)
def test_paginate_rejects_invalid_counts(
    row_count: int, first_page_rows: int, other_page_rows: int
) -> None:
    with pytest.raises(ValueError):
        paginate(row_count, first_page_rows, other_page_rows)


def test_render_inventory_sheet_writes_pages_and_preserves_original_page_range(
    qapp, tmp_path: Path
) -> None:
    rows = [
        replace(make_item_row(), id=row_index, code=f"TOOL-{row_index:03}")
        for row_index in range(1, INVENTORY_SHEET_SPEC.first_page_rows + 2)
    ]
    all_pages_path = tmp_path / "inventory-all-pages.pdf"
    selected_page_path = tmp_path / "inventory-page-2.pdf"

    render_inventory_sheet(
        _inventory_sheet_pdf_writer(all_pages_path),
        rows,
        "(絞り込みなし)",
        "2026-10-13 12:00:00",
        INVENTORY_SHEET_SPEC,
    )
    render_inventory_sheet(
        _inventory_sheet_pdf_writer(selected_page_path),
        rows,
        "(絞り込みなし)",
        "2026-10-13 12:00:00",
        INVENTORY_SHEET_SPEC,
        page_numbers=[2],
    )

    assert all_pages_path.read_bytes().startswith(b"%PDF-")
    assert _pdf_page_count(all_pages_path) == 2
    assert _pdf_page_count(selected_page_path) == 1


def test_render_inventory_sheet_rejects_page_outside_document() -> None:
    with pytest.raises(ValueError, match="印刷対象"):
        render_inventory_sheet(
            _inventory_sheet_pdf_writer(Path("unused.pdf")),
            [make_item_row()],
            "(絞り込みなし)",
            "2026-10-13 12:00:00",
            INVENTORY_SHEET_SPEC,
            page_numbers=[2],
        )
