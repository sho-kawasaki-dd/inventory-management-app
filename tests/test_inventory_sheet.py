import math
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPagedPaintDevice,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
)
from PySide6.QtPdf import QPdfDocument

from inventory_manager_mini.core.models import ItemFilter, ItemRow, PurchaseInfo
from inventory_manager_mini.ui.printing import inventory_sheet_renderer
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


def _inventory_sheet_pdf_writer(path: Path, dpi: int = 300) -> QPdfWriter:
    writer = QPdfWriter(str(path))
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Landscape,
        QMarginsF(0, 0, 0, 0),
    )
    assert writer.setPageLayout(layout)
    writer.setResolution(dpi)
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


PRINTED_AT = "2026-10-13 12:00:00"
HEADINGS = [column.heading for column in INVENTORY_SHEET_COLUMNS]
FIRST_ROWS = INVENTORY_SHEET_SPEC.first_page_rows
OTHER_ROWS = INVENTORY_SHEET_SPEC.other_page_rows


def _make_rows(count: int) -> list[ItemRow]:
    return [
        replace(make_item_row(), id=index, code=f"TOOL-{index:04}") for index in range(1, count + 1)
    ]


class _FakeMetrics:
    def elidedText(self, text: str, mode: Qt.TextElideMode, width: int) -> str:
        assert mode == Qt.TextElideMode.ElideRight
        return text

    def horizontalAdvance(self, text: str) -> int:
        return len(text)

    def height(self) -> int:
        return 10

    def ascent(self) -> int:
        return 8


class _FakeDevice:
    def __init__(self, *, new_page_result: bool = True, dpi: int = 300) -> None:
        self.new_page_result = new_page_result
        self.dpi = dpi
        self.new_page_calls = 0

    def logicalDpiX(self) -> int:
        return self.dpi

    def logicalDpiY(self) -> int:
        return self.dpi

    def newPage(self) -> bool:
        self.new_page_calls += 1
        return self.new_page_result


class _FakePainter:
    """描画先の出力ページ(newPage の回数)ごとに文字・背景・下線の描画を記録する。"""

    def __init__(
        self,
        device: _FakeDevice,
        *,
        begin_result: bool = True,
        end_result: bool = True,
        draw_error: Exception | None = None,
        end_error: Exception | None = None,
    ) -> None:
        self.device = device
        self.begin_result = begin_result
        self.end_result = end_result
        self.draw_error = draw_error
        self.end_error = end_error
        self.begin_calls = 0
        self.end_calls = 0
        self.texts: dict[int, list[str]] = {}
        self.fills: dict[int, int] = {}
        self.lines: dict[int, int] = {}

    def _page(self) -> int:
        return self.device.new_page_calls

    def begin(self, device: object) -> bool:
        self.begin_calls += 1
        return self.begin_result

    def end(self) -> bool:
        self.end_calls += 1
        if self.end_error is not None:
            raise self.end_error
        return self.end_result

    def setFont(self, font: object) -> None:
        pass

    def setPen(self, pen: object) -> None:
        pass

    def fontMetrics(self) -> _FakeMetrics:
        return _FakeMetrics()

    def fillRect(self, rect: QRectF, color: QColor) -> None:
        self.fills[self._page()] = self.fills.get(self._page(), 0) + 1

    def drawLine(self, start: QPointF, end: QPointF) -> None:
        self.lines[self._page()] = self.lines.get(self._page(), 0) + 1

    def drawText(self, point: QPointF, text: str) -> None:
        if self.draw_error is not None:
            raise self.draw_error
        self.texts.setdefault(self._page(), []).append(text)


def _render_with_fake(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[ItemRow],
    *,
    page_numbers: list[int] | None = None,
    device: _FakeDevice | None = None,
    begin_result: bool = True,
    end_result: bool = True,
    draw_error: Exception | None = None,
    end_error: Exception | None = None,
) -> tuple[_FakePainter, _FakeDevice]:
    fake_device = device or _FakeDevice()
    painter = _FakePainter(
        fake_device,
        begin_result=begin_result,
        end_result=end_result,
        draw_error=draw_error,
        end_error=end_error,
    )
    monkeypatch.setattr(inventory_sheet_renderer, "QPainter", lambda: painter)
    monkeypatch.setattr(inventory_sheet_renderer, "print_font", lambda size, bold=False: None)
    render_inventory_sheet(
        cast(QPagedPaintDevice, fake_device),
        rows,
        "(絞り込みなし)",
        PRINTED_AT,
        INVENTORY_SHEET_SPEC,
        page_numbers=page_numbers,
    )
    return painter, fake_device


def _codes(texts: list[str]) -> list[str]:
    return [text for text in texts if text.startswith("TOOL-")]


def test_render_inventory_sheet_repeats_headings_and_numbers_footers(monkeypatch) -> None:
    row_count = FIRST_ROWS + OTHER_ROWS + 1
    rows = _make_rows(row_count)

    painter, device = _render_with_fake(monkeypatch, rows)

    assert device.new_page_calls == 2
    assert painter.begin_calls == painter.end_calls == 1
    expected_ranges = paginate(row_count, FIRST_ROWS, OTHER_ROWS)
    assert len(expected_ranges) == 3
    for page_index, row_range in enumerate(expected_ranges):
        texts = painter.texts[page_index]
        assert [text for text in texts if text in HEADINGS] == HEADINGS
        assert painter.fills[page_index] == 1
        assert painter.lines[page_index] == 1
        assert f"ページ {page_index + 1} / 3" in texts
        assert f"印刷日時 {PRINTED_AT}" in texts
        assert _codes(texts) == [rows[index].code for index in row_range]
        has_title = f"在庫表（{row_count:,} 件）" in texts
        assert has_title is (page_index == 0)


@pytest.mark.parametrize(
    "page_numbers",
    [None, [1], [2, 3], [5], [1, 5], [4, 2, 2]],
)
def test_render_inventory_sheet_range_keeps_original_page_numbers(
    monkeypatch, page_numbers: list[int] | None
) -> None:
    row_count = FIRST_ROWS + 3 * OTHER_ROWS + 1
    rows = _make_rows(row_count)
    page_ranges = paginate(row_count, FIRST_ROWS, OTHER_ROWS)
    assert len(page_ranges) == 5
    expected_pages = [1, 2, 3, 4, 5] if page_numbers is None else sorted(set(page_numbers))

    painter, device = _render_with_fake(monkeypatch, rows, page_numbers=page_numbers)

    assert device.new_page_calls == len(expected_pages) - 1
    assert sorted(painter.texts) == list(range(len(expected_pages)))
    for output_index, page_number in enumerate(expected_pages):
        texts = painter.texts[output_index]
        assert [text for text in texts if text in HEADINGS] == HEADINGS
        assert painter.fills[output_index] == 1
        assert f"ページ {page_number} / 5" in texts
        assert (f"在庫表（{row_count:,} 件）" in texts) is (page_number == 1)
        assert _codes(texts) == [rows[index].code for index in page_ranges[page_number - 1]]


def test_render_inventory_sheet_without_rows_does_not_start_painter(monkeypatch) -> None:
    painter, device = _render_with_fake(monkeypatch, [])

    assert painter.begin_calls == 0
    assert device.new_page_calls == 0


@pytest.mark.parametrize("page_numbers", [[], [0], [-1], [True], [6]])
def test_render_inventory_sheet_rejects_invalid_page_numbers_before_starting(
    monkeypatch, page_numbers: list[int]
) -> None:
    painter = _FakePainter(_FakeDevice())
    monkeypatch.setattr(inventory_sheet_renderer, "QPainter", lambda: painter)

    with pytest.raises(ValueError):
        render_inventory_sheet(
            cast(QPagedPaintDevice, painter.device),
            _make_rows(FIRST_ROWS + 3 * OTHER_ROWS + 1),
            "(絞り込みなし)",
            PRINTED_AT,
            INVENTORY_SHEET_SPEC,
            page_numbers=page_numbers,
        )

    assert painter.begin_calls == 0


def test_render_inventory_sheet_begin_failure_does_not_end_or_draw(monkeypatch) -> None:
    painter = _FakePainter(_FakeDevice(), begin_result=False)
    monkeypatch.setattr(inventory_sheet_renderer, "QPainter", lambda: painter)
    with pytest.raises(RuntimeError, match="描画を開始"):
        render_inventory_sheet(
            cast(QPagedPaintDevice, painter.device),
            _make_rows(3),
            "(絞り込みなし)",
            PRINTED_AT,
            INVENTORY_SHEET_SPEC,
        )
    assert painter.end_calls == 0
    assert painter.texts == {}


def test_render_inventory_sheet_new_page_failure_stops_later_pages(monkeypatch) -> None:
    device = _FakeDevice(new_page_result=False)
    painter = _FakePainter(device)
    monkeypatch.setattr(inventory_sheet_renderer, "QPainter", lambda: painter)
    monkeypatch.setattr(inventory_sheet_renderer, "print_font", lambda size, bold=False: None)

    with pytest.raises(RuntimeError, match="ページを切り替え"):
        render_inventory_sheet(
            cast(QPagedPaintDevice, device),
            _make_rows(FIRST_ROWS + OTHER_ROWS + 1),
            "(絞り込みなし)",
            PRINTED_AT,
            INVENTORY_SHEET_SPEC,
        )

    assert device.new_page_calls == 1
    assert painter.end_calls == 1
    assert "ページ 1 / 3" in painter.texts[0]
    assert all("ページ 2 / 3" not in texts for texts in painter.texts.values())


def test_render_inventory_sheet_drawing_error_stops_and_ends_painter(monkeypatch) -> None:
    device = _FakeDevice()
    painter = _FakePainter(device, draw_error=ValueError("draw failed"))
    monkeypatch.setattr(inventory_sheet_renderer, "QPainter", lambda: painter)
    monkeypatch.setattr(inventory_sheet_renderer, "print_font", lambda size, bold=False: None)

    with pytest.raises(ValueError, match="draw failed"):
        render_inventory_sheet(
            cast(QPagedPaintDevice, device),
            _make_rows(FIRST_ROWS + 1),
            "(絞り込みなし)",
            PRINTED_AT,
            INVENTORY_SHEET_SPEC,
        )

    assert painter.begin_calls == painter.end_calls == 1
    assert device.new_page_calls == 0


def test_render_inventory_sheet_end_failure_is_reported(monkeypatch) -> None:
    with pytest.raises(RuntimeError, match="描画を終了"):
        _render_with_fake(monkeypatch, _make_rows(3), end_result=False)


def test_render_inventory_sheet_end_exception_is_reported(monkeypatch) -> None:
    with pytest.raises(OSError, match="end failed"):
        _render_with_fake(monkeypatch, _make_rows(3), end_error=OSError("end failed"))


@pytest.mark.parametrize(
    ("end_result", "end_error", "expected_log"),
    [
        (False, None, "QPainter.end()も失敗"),
        (True, OSError("end failed"), "終了処理にも失敗"),
    ],
)
def test_render_inventory_sheet_keeps_drawing_error_when_end_also_fails(
    monkeypatch, caplog, end_result: bool, end_error: Exception | None, expected_log: str
) -> None:
    with pytest.raises(ValueError, match="draw failed"):
        _render_with_fake(
            monkeypatch,
            _make_rows(3),
            draw_error=ValueError("draw failed"),
            end_result=end_result,
            end_error=end_error,
        )

    assert expected_log in caplog.text


class _MeasuringPainter:
    """実際の QPainter に委譲し、描画文字列と描画先メトリクスでの幅を記録する。"""

    def __init__(self) -> None:
        self._painter = QPainter()
        self.drawn: list[tuple[str, int]] = []

    def begin(self, device: object) -> bool:
        return self._painter.begin(cast(QPagedPaintDevice, device))

    def end(self) -> bool:
        return self._painter.end()

    def setFont(self, font: QFont) -> None:
        self._painter.setFont(font)

    def setPen(self, pen: QPen) -> None:
        self._painter.setPen(pen)

    def fontMetrics(self) -> QFontMetrics:
        return self._painter.fontMetrics()

    def fillRect(self, rect: QRectF, color: QColor) -> None:
        self._painter.fillRect(rect, color)

    def drawLine(self, start: QPointF, end: QPointF) -> None:
        self._painter.drawLine(start, end)

    def drawText(self, point: QPointF, text: str) -> None:
        self.drawn.append((text, self._painter.fontMetrics().horizontalAdvance(text)))
        self._painter.drawText(point, text)


@pytest.mark.parametrize("dpi", [300, 1200])
def test_render_inventory_sheet_elides_long_text_within_cell_width_at_device_dpi(
    monkeypatch, qapp, tmp_path: Path, dpi: int
) -> None:
    long_row = replace(
        make_item_row(),
        code="C" * 200,
        name="品" * 200,
        manufacturer_part_number="M" * 200,
        application="用" * 200,
        category_path="類" * 200,
        location_name="場" * 200,
        unit="単" * 200,
        purchaser_name="発" * 200,
        quantity=1_000_000,
        reorder_threshold=1_000_000,
        reorder_quantity=1_000_000,
        is_low_stock=True,
    )
    painter = _MeasuringPainter()
    monkeypatch.setattr(inventory_sheet_renderer, "QPainter", lambda: painter)

    render_inventory_sheet(
        _inventory_sheet_pdf_writer(tmp_path / f"width-{dpi}.pdf", dpi),
        [long_row],
        "検" * 500,
        PRINTED_AT,
        INVENTORY_SHEET_SPEC,
    )

    # タイトル2行・見出し12列・明細12列・フッタ2行の順に描かれる
    assert len(painter.drawn) == 2 + 12 + 12 + 2
    row_cells = painter.drawn[14:26]
    inset_mm = inventory_sheet_renderer._CELL_INSET_MM
    for column, (text, width) in zip(INVENTORY_SHEET_COLUMNS, row_cells, strict=True):
        inner_width = math.floor((column.width_mm - 2 * inset_mm) * dpi / 25.4)
        original = column.value_getter(long_row)
        assert width <= inner_width, column.heading
        if column.alignment == "left":
            assert text != original, column.heading
            assert text.endswith("…"), column.heading
    summary_text, summary_width = painter.drawn[1]
    assert summary_text.endswith("…")
    summary_inner = math.floor((INVENTORY_SHEET_PRINTABLE_WIDTH_MM - 2 * inset_mm) * dpi / 25.4)
    assert summary_width <= summary_inner


def test_render_inventory_sheet_handles_very_long_text_in_pdf(qapp, tmp_path: Path) -> None:
    long_row = replace(make_item_row(), name="品" * 500, application="用" * 500)
    pdf_path = tmp_path / "long.pdf"

    render_inventory_sheet(
        _inventory_sheet_pdf_writer(pdf_path),
        [long_row],
        "検" * 500,
        PRINTED_AT,
        INVENTORY_SHEET_SPEC,
    )

    assert pdf_path.read_bytes().startswith(b"%PDF-")
    assert _pdf_page_count(pdf_path) == 1


@pytest.mark.parametrize(
    ("page_numbers", "expected_pages"),
    [(None, 5), ([1, 2, 3, 4, 5], 5), ([2, 3], 2), ([5], 1), ([1, 5], 2), ([3, 3], 1)],
)
def test_render_inventory_sheet_pdf_page_range_outputs_only_requested_pages(
    qapp, tmp_path: Path, page_numbers: list[int] | None, expected_pages: int
) -> None:
    rows = _make_rows(FIRST_ROWS + 3 * OTHER_ROWS + 1)
    pdf_path = tmp_path / "range.pdf"

    render_inventory_sheet(
        _inventory_sheet_pdf_writer(pdf_path),
        rows,
        "(絞り込みなし)",
        PRINTED_AT,
        INVENTORY_SHEET_SPEC,
        page_numbers=page_numbers,
    )

    assert _pdf_page_count(pdf_path) == expected_pages


@pytest.mark.perf
def test_render_inventory_sheet_five_thousand_rows_in_pdf(qapp, tmp_path: Path) -> None:
    rows = _make_rows(5000)
    pdf_path = tmp_path / "five-thousand.pdf"

    started_at = time.perf_counter()
    render_inventory_sheet(
        _inventory_sheet_pdf_writer(pdf_path),
        rows,
        "(絞り込みなし)",
        PRINTED_AT,
        INVENTORY_SHEET_SPEC,
    )
    elapsed = time.perf_counter() - started_at

    expected_pages = len(paginate(len(rows), FIRST_ROWS, OTHER_ROWS))
    assert _pdf_page_count(pdf_path) == expected_pages
    print(f"品目 5,000 件の在庫表 PDF 出力({expected_pages} ページ): {elapsed:.3f} 秒")


@pytest.mark.parametrize(
    ("row_count", "expected_pages"),
    [
        (FIRST_ROWS, 1),
        (FIRST_ROWS + 1, 2),
        (FIRST_ROWS + OTHER_ROWS, 2),
        (FIRST_ROWS + OTHER_ROWS + 1, 3),
    ],
)
def test_paginate_boundaries_with_inventory_sheet_capacities(
    row_count: int, expected_pages: int
) -> None:
    pages = paginate(row_count, FIRST_ROWS, OTHER_ROWS)

    assert len(pages) == expected_pages
    assert len(pages[0]) == min(row_count, FIRST_ROWS)
    assert sum(len(page) for page in pages) == row_count
