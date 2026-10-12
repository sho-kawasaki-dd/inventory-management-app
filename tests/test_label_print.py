from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QMarginsF, QPointF, Qt
from PySide6.QtGui import QFont, QPagedPaintDevice, QPageLayout, QPageSize
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QPrinter

from inventory_manager_mini.core.models import ItemRow, PurchaseInfo
from inventory_manager_mini.ui.printing import fonts, label_renderer
from inventory_manager_mini.ui.printing.layout import (
    LABEL_72212,
    LabelEntry,
    LabelPlacement,
    assign_label_slots,
    slot_rect_mm,
)


def _item_row(note: str | None = "備考") -> ItemRow:
    return ItemRow(
        id=1,
        client_id=1,
        purchaser_id=1,
        code="ITEM-001",
        name="棚ラベルの品目",
        category_id=1,
        location_id=1,
        unit="個",
        quantity=10,
        reorder_threshold=1,
        reorder_quantity=None,
        purchase_url=None,
        supplier=None,
        manufacturer_part_number=None,
        application=None,
        reference_price=None,
        note=note,
        is_active=True,
        created_at="2026-10-13 00:00:00",
        updated_at="2026-10-13 00:00:00",
        client_name="利用者",
        purchaser_name="発注主体",
        category_path="消耗品",
        location_name="棚A-1",
        is_low_stock=False,
        purchase_info=PurchaseInfo(last_purchased_at=None, lot_quantity=None),
    )


class _FakeMetrics:
    def __init__(self, widths: list[int]) -> None:
        self.widths = widths

    def elidedText(self, text: str, mode: Qt.TextElideMode, width: int) -> str:
        assert mode == Qt.TextElideMode.ElideRight
        self.widths.append(width)
        return text

    def ascent(self) -> int:
        return 10


class _FakePainter:
    def __init__(
        self,
        *,
        begin_result: bool = True,
        end_result: bool = True,
        draw_error: Exception | None = None,
    ) -> None:
        self.begin_result = begin_result
        self.end_result = end_result
        self.draw_error = draw_error
        self.begin_calls = 0
        self.end_calls = 0
        self.draw_calls: list[tuple[float, float, str]] = []
        self.widths: list[int] = []

    def begin(self, device: object) -> bool:
        self.begin_calls += 1
        return self.begin_result

    def end(self) -> bool:
        self.end_calls += 1
        return self.end_result

    def setFont(self, font: object) -> None:
        pass

    def fontMetrics(self) -> _FakeMetrics:
        return _FakeMetrics(self.widths)

    def drawText(self, point: QPointF, text: str) -> None:
        if self.draw_error is not None:
            raise self.draw_error
        self.draw_calls.append((point.x(), point.y(), text))


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


def _as_paged_device(device: _FakeDevice) -> QPagedPaintDevice:
    return cast(QPagedPaintDevice, device)


def _install_fake_painter(monkeypatch, painter: _FakePainter) -> None:
    monkeypatch.setattr(label_renderer, "QPainter", lambda: painter)
    monkeypatch.setattr(
        label_renderer,
        "print_font",
        lambda point_size, bold: (point_size, bold),
    )


def _pdf_printer(path: Path) -> QPrinter:
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(path))
    layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Portrait,
        QMarginsF(0, 0, 0, 0),
    )
    assert printer.setPageLayout(layout)
    printer.setFullPage(True)
    return printer


def test_print_font_uses_first_available_candidate(monkeypatch, qapp) -> None:
    class FontDatabase:
        @staticmethod
        def families() -> list[str]:
            return ["Noto Sans CJK JP", "Yu Gothic UI"]

    monkeypatch.setattr(fonts, "QFontDatabase", FontDatabase)

    font = fonts.print_font(12, bold=True)

    assert font.family() == "Yu Gothic UI"
    assert font.pointSizeF() == 12
    assert font.bold()


def test_print_font_falls_back_to_system_font(monkeypatch, qapp) -> None:
    fallback_font = QFont("System Fallback")

    class FontDatabase:
        class SystemFont:
            GeneralFont = object()

        @staticmethod
        def families() -> list[str]:
            return []

        @staticmethod
        def systemFont(_font_type: object) -> QFont:
            return QFont(fallback_font)

    monkeypatch.setattr(fonts, "QFontDatabase", FontDatabase)

    font = fonts.print_font(9)

    assert font.family() == "System Fallback"
    assert font.pointSizeF() == 9
    assert not font.bold()


def test_render_labels_writes_pdf_with_one_page_per_layout_page(qtbot, tmp_path: Path) -> None:
    placements = assign_label_slots([LabelEntry(item_id=1, copies=13)], range(12))
    pdf_path = tmp_path / "labels.pdf"
    printer = _pdf_printer(pdf_path)

    label_renderer.render_labels(
        printer,
        placements,
        {1: _item_row("備考\n2行目")},
        LABEL_72212,
        0.0,
        0.0,
    )

    assert pdf_path.read_bytes().startswith(b"%PDF-")
    document = QPdfDocument()
    document.load(str(pdf_path))
    assert document.pageCount() == 2


@pytest.mark.parametrize("dpi", [300, 1200])
def test_render_labels_uses_device_metrics_and_preserves_selected_slot(
    monkeypatch, dpi: int
) -> None:
    painter = _FakePainter()
    device = _FakeDevice(dpi=dpi)
    _install_fake_painter(monkeypatch, painter)
    placements = [
        LabelPlacement(page_number=1, slot=0, item_id=1),
        LabelPlacement(page_number=2, slot=11, item_id=1),
    ]

    label_renderer.render_labels(
        _as_paged_device(device),
        placements,
        {1: _item_row("備考\n2行目")},
        LABEL_72212,
        1.0,
        -0.5,
        page_numbers=[2],
    )

    slot_x, slot_y, _, _ = slot_rect_mm(LABEL_72212, 11, 1.0, -0.5)
    expected_x = (slot_x + 3.0) * dpi / 25.4
    expected_width = math.floor((LABEL_72212.label_width_mm - 6.0) * dpi / 25.4)
    assert device.new_page_calls == 0
    assert len(painter.draw_calls) == 4
    assert painter.draw_calls[0][0] == pytest.approx(expected_x)
    assert painter.draw_calls[0][1] == pytest.approx((slot_y + 3.0) * dpi / 25.4 + 10)
    assert painter.widths == [expected_width] * 4
    assert painter.draw_calls[-1][2] == "備考 2行目"


def test_render_labels_empty_placements_do_not_start_painter(monkeypatch) -> None:
    monkeypatch.setattr(
        label_renderer,
        "QPainter",
        lambda: pytest.fail("empty placements must not create a painter"),
    )

    label_renderer.render_labels(_as_paged_device(_FakeDevice()), [], {}, LABEL_72212, 0.0, 0.0)


def test_render_labels_begin_failure_does_not_end_painter(monkeypatch) -> None:
    painter = _FakePainter(begin_result=False)
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(RuntimeError, match="描画を開始"):
        label_renderer.render_labels(
            _as_paged_device(_FakeDevice()),
            [LabelPlacement(1, 0, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
        )

    assert painter.end_calls == 0
    assert painter.draw_calls == []


def test_render_labels_new_page_failure_stops_before_later_page(monkeypatch) -> None:
    painter = _FakePainter()
    device = _FakeDevice(new_page_result=False)
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(RuntimeError, match="ページを切り替え"):
        label_renderer.render_labels(
            _as_paged_device(device),
            [LabelPlacement(1, 0, 1), LabelPlacement(2, 1, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
        )

    assert device.new_page_calls == 1
    assert len(painter.draw_calls) == 4
    assert painter.end_calls == 1


def test_render_labels_end_failure_is_reported(monkeypatch) -> None:
    painter = _FakePainter(end_result=False)
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(RuntimeError, match="描画を終了"):
        label_renderer.render_labels(
            _as_paged_device(_FakeDevice()),
            [LabelPlacement(1, 0, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
        )


def test_render_labels_preserves_drawing_error_when_end_also_fails(monkeypatch, caplog) -> None:
    painter = _FakePainter(end_result=False, draw_error=ValueError("draw failed"))
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(ValueError, match="draw failed"):
        label_renderer.render_labels(
            _as_paged_device(_FakeDevice()),
            [LabelPlacement(1, 0, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
        )

    assert painter.end_calls == 1
    assert "QPainter.end()も失敗" in caplog.text


def test_render_labels_rejects_page_selection_without_targets(monkeypatch) -> None:
    painter = _FakePainter()
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(ValueError, match="印刷対象のページ"):
        label_renderer.render_labels(
            _as_paged_device(_FakeDevice()),
            [LabelPlacement(1, 0, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
            page_numbers=[2],
        )

    assert painter.begin_calls == 0
