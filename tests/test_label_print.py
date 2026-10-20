from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from PySide6.QtCore import QMarginsF, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPagedPaintDevice,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHeaderView, QSpinBox

from inventory_manager_mini.core.models import ItemRow, PurchaseInfo
from inventory_manager_mini.ui import error_handling
from inventory_manager_mini.ui.printing import fonts, label_renderer, preview_dialog
from inventory_manager_mini.ui.printing.layout import (
    LABEL_72212,
    LabelEntry,
    LabelPlacement,
    assign_label_slots,
    slot_rect_mm,
)
from tests.conftest import assert_buttons_reachable, assert_fits_work_area


def _item_row(
    note: str | None = "備考",
    *,
    manufacturer_part_number: str | None = "MODEL-001",
    application: str | None = "機器補修用",
) -> ItemRow:
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
        manufacturer_part_number=manufacturer_part_number,
        application=application,
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

    def boundingRect(self, text: str) -> QRect:
        return QRect(0, -self.ascent(), len(text), self.ascent())


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
    top_offsets_mm = (3.0, 9.0, 15.0, 21.0, 27.0, 33.0)
    assert device.new_page_calls == 0
    assert len(painter.draw_calls) == 6
    assert painter.draw_calls[0][0] == pytest.approx(expected_x)
    assert [call[1] for call in painter.draw_calls] == pytest.approx(
        [(slot_y + offset) * dpi / 25.4 + 10 for offset in top_offsets_mm]
    )
    assert [call[2] for call in painter.draw_calls] == [
        "棚ラベルの品目",
        "MODEL-001",
        "機器補修用",
        "ITEM-001",
        "棚A-1",
        "備考 2行目",
    ]
    assert painter.widths == [expected_width] * 6
    assert painter.draw_calls[-1][2] == "備考 2行目"


@pytest.mark.parametrize("manufacturer_part_number", ["MODEL-001", None, ""])
@pytest.mark.parametrize("application", ["機器補修用", None, ""])
def test_render_labels_keeps_blank_rows_in_their_fixed_positions(
    monkeypatch,
    manufacturer_part_number: str | None,
    application: str | None,
) -> None:
    painter = _FakePainter()
    device = _FakeDevice()
    _install_fake_painter(monkeypatch, painter)
    item = _item_row(
        manufacturer_part_number=manufacturer_part_number,
        application=application,
    )

    label_renderer.render_labels(
        _as_paged_device(device),
        [LabelPlacement(1, 0, 1)],
        {1: item},
        LABEL_72212,
        0.0,
        0.0,
    )

    offsets = (3.0, 9.0, 15.0, 21.0, 27.0, 33.0)
    texts = (
        item.name,
        item.manufacturer_part_number or "",
        item.application or "",
        item.code,
        item.location_name or "",
        item.note or "",
    )
    slot_y = slot_rect_mm(LABEL_72212, 0)[1]
    expected_rows = [(text, offset) for text, offset in zip(texts, offsets, strict=True) if text]
    assert [call[2] for call in painter.draw_calls] == [text for text, _ in expected_rows]
    assert [call[1] for call in painter.draw_calls] == pytest.approx(
        [(slot_y + offset) * device.dpi / 25.4 + 10 for _, offset in expected_rows]
    )
    assert (
        painter.widths == [math.floor((LABEL_72212.label_width_mm - 6.0) * device.dpi / 25.4)] * 6
    )


def test_render_labels_replaces_newlines_in_application_and_note(monkeypatch) -> None:
    painter = _FakePainter()
    _install_fake_painter(monkeypatch, painter)
    item = _item_row(
        "備考1\r\n備考2\r備考3\n備考4",
        application="用途1\r\n用途2\r用途3\n用途4",
    )

    label_renderer.render_labels(
        _as_paged_device(_FakeDevice()),
        [LabelPlacement(1, 0, 1)],
        {1: item},
        LABEL_72212,
        0.0,
        0.0,
    )

    assert [call[2] for call in painter.draw_calls] == [
        "棚ラベルの品目",
        "MODEL-001",
        "用途1 用途2 用途3 用途4",
        "ITEM-001",
        "棚A-1",
        "備考1 備考2 備考3 備考4",
    ]
    assert all("\r" not in call[2] and "\n" not in call[2] for call in painter.draw_calls)


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
    assert len(painter.draw_calls) == 6
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


def test_label_print_dialog_defaults_and_updates_page_count(qtbot) -> None:
    from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog

    second_item = replace(_item_row(), id=2, code="ITEM-002", name="2番目の品目")
    dialog = LabelPrintDialog([_item_row(), second_item])
    qtbot.addWidget(dialog)

    assert [spin.value() for spin in dialog.quantity_spins] == [1, 1]
    assert dialog.entries == [LabelEntry(1, 1), LabelEntry(2, 1)]
    assert dialog.first_page_slots == list(range(12))
    assert dialog.count_label.text() == "合計 2 枚 / 用紙 1 枚"

    dialog.quantity_spins[0].setValue(12)
    assert dialog.count_label.text() == "合計 13 枚 / 用紙 2 枚"
    assert dialog.entries[0] == LabelEntry(1, 12)


@pytest.mark.parametrize(
    ("manufacturer_part_number", "application"),
    [
        (manufacturer_part_number, application)
        for manufacturer_part_number in ("MODEL-001", None, "")
        for application in ("機器補修用", None, "")
    ],
)
def test_label_print_dialog_displays_item_details_and_quantity_in_last_column(
    qtbot,
    manufacturer_part_number: str | None,
    application: str | None,
) -> None:
    from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog

    dialog = LabelPrintDialog(
        [
            _item_row(
                manufacturer_part_number=manufacturer_part_number,
                application=application,
            )
        ]
    )
    qtbot.addWidget(dialog)

    table = dialog.item_table
    header = table.horizontalHeader()
    assert table.columnCount() == 5
    header_labels = []
    for column in range(5):
        header_item = table.horizontalHeaderItem(column)
        assert header_item is not None
        header_labels.append(header_item.text())
    assert header_labels == [
        "管理番号",
        "品名",
        "メーカー型番",
        "用途",
        "枚数",
    ]
    assert [header.sectionResizeMode(column) for column in range(5)] == [
        QHeaderView.ResizeMode.ResizeToContents,
        QHeaderView.ResizeMode.Stretch,
        QHeaderView.ResizeMode.Interactive,
        QHeaderView.ResizeMode.Interactive,
        QHeaderView.ResizeMode.Fixed,
    ]
    assert [table.columnWidth(column) for column in (2, 3, 4)] == [140, 140, 90]

    expected_texts = [
        "ITEM-001",
        "棚ラベルの品目",
        manufacturer_part_number or "",
        application or "",
    ]
    for column, expected_text in enumerate(expected_texts):
        cell = table.item(0, column)
        assert cell is not None
        assert cell.text() == expected_text
        assert not cell.flags() & Qt.ItemFlag.ItemIsEditable
        if column > 0:
            assert cell.toolTip() == expected_text

    assert table.cellWidget(0, 2) is None
    assert table.cellWidget(0, 3) is None
    quantity_spin = table.cellWidget(0, 4)
    assert isinstance(quantity_spin, QSpinBox)
    assert quantity_spin is dialog.quantity_spins[0]
    assert quantity_spin.minimum() == 1
    assert quantity_spin.maximum() == 99


def test_label_print_dialog_requires_at_least_one_slot(qtbot) -> None:
    from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog

    dialog = LabelPrintDialog([_item_row()])
    qtbot.addWidget(dialog)
    ok_button = dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)

    dialog.clear_all_button.click()

    assert dialog.first_page_slots == []
    assert dialog.error_label.text() == "印字位置を 1 つ以上選択してください"
    assert not ok_button.isEnabled()

    dialog.select_all_button_widget.click()

    assert dialog.first_page_slots == list(range(12))
    assert dialog.error_label.text() == ""
    assert ok_button.isEnabled()


@pytest.mark.parametrize("font_scale", [1.0, 1.5])
def test_label_print_dialog_fits_at_large_font_scale(qtbot, font_scale: float) -> None:
    from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog

    rows = [
        replace(
            _item_row(
                manufacturer_part_number=f"MODEL-{index:03d}-" + "X" * 80,
                application="機器補修用" * 20,
            ),
            id=index,
            code=f"ITEM-{index:03d}",
            name=f"長い品目名{index}-" + "部品" * 20,
        )
        for index in range(1, 41)
    ]
    dialog = LabelPrintDialog(rows)
    qtbot.addWidget(dialog)
    original_font = dialog.font()
    large_font = QFont(original_font)
    large_font.setPointSizeF(original_font.pointSizeF() * font_scale)
    dialog.setFont(large_font)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert_fits_work_area(dialog)
    assert_buttons_reachable(
        dialog,
        dialog.button_box.button(QDialogButtonBox.StandardButton.Ok),
        dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel),
    )
    assert dialog.item_table.verticalScrollBar().maximum() > 0
    first_quantity_spin = dialog.quantity_spins[0]
    assert first_quantity_spin.isVisible()
    first_spin_rect = QRect(
        first_quantity_spin.mapTo(dialog.item_table.viewport(), QPoint(0, 0)),
        first_quantity_spin.size(),
    )
    assert dialog.item_table.viewport().rect().contains(first_spin_rect)
    long_texts = (
        rows[0].name,
        rows[0].manufacturer_part_number or "",
        rows[0].application or "",
    )
    for column, expected_text in enumerate(long_texts, start=1):
        cell = dialog.item_table.item(0, column)
        assert cell is not None
        assert cell.text() == expected_text
        assert cell.toolTip() == expected_text

    dialog.item_table.setColumnWidth(2, 500)
    dialog.item_table.setColumnWidth(3, 500)
    horizontal_scrollbar = dialog.item_table.horizontalScrollBar()
    qtbot.waitUntil(lambda: horizontal_scrollbar.maximum() > 0)
    header = dialog.item_table.horizontalHeader()
    viewport_width = dialog.item_table.viewport().width()
    visible_columns: set[int] = set()
    for scroll_position in range(
        horizontal_scrollbar.minimum(), horizontal_scrollbar.maximum() + 1
    ):
        horizontal_scrollbar.setValue(scroll_position)
        for column in range(dialog.item_table.columnCount()):
            column_left = header.sectionViewportPosition(column)
            column_right = column_left + header.sectionSize(column)
            if column_left < viewport_width and column_right > 0:
                visible_columns.add(column)
    assert visible_columns == set(range(dialog.item_table.columnCount()))

    first_slot_checkbox = dialog.slot_checkboxes[0]
    dialog.scroll_area.ensureWidgetVisible(first_slot_checkbox)
    checkbox_rect = QRect(
        first_slot_checkbox.mapTo(dialog.scroll_area.viewport(), QPoint(0, 0)),
        first_slot_checkbox.size(),
    )
    assert dialog.scroll_area.viewport().rect().contains(checkbox_rect)


def test_label_print_dialog_does_not_retain_settings_between_instances(qtbot) -> None:
    from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog

    first = LabelPrintDialog([_item_row()])
    qtbot.addWidget(first)
    first.quantity_spins[0].setValue(42)
    first.clear_all_button.click()

    second = LabelPrintDialog([_item_row()])
    qtbot.addWidget(second)

    assert second.quantity_spins[0].value() == 1
    assert second.first_page_slots == list(range(12))


_ORIENTATIONS = [QPageLayout.Orientation.Portrait, QPageLayout.Orientation.Landscape]


def _opposite(orientation: QPageLayout.Orientation) -> QPageLayout.Orientation:
    if orientation == QPageLayout.Orientation.Portrait:
        return QPageLayout.Orientation.Landscape
    return QPageLayout.Orientation.Portrait


@pytest.mark.parametrize("orientation", _ORIENTATIONS)
def test_print_preview_locks_page_layout_and_replaces_native_actions(
    qtbot, orientation: QPageLayout.Orientation
) -> None:
    from PySide6.QtGui import QPageLayout, QPageSize

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    printer = _pdf_printer(Path("preview.pdf"))
    calls: list[tuple[object, object]] = []
    dialog = PrintPreviewDialog(
        printer,
        orientation,
        3,
        lambda device, pages: calls.append((device, pages)),
    )
    qtbot.addWidget(dialog)

    actions = dialog._toolbar.actions()
    assert actions[20] is dialog.print_action
    assert dialog.print_action.objectName() == "managedPrintAction"
    assert all(not actions[index].isVisible() for index in (7, 8, 21))
    assert all(not actions[index].isEnabled() for index in (7, 8, 21))

    changed_layout = QPageLayout(
        QPageSize(QPageSize.PageSizeId.Letter),
        _opposite(orientation),
        QMarginsF(5, 5, 5, 5),
        QPageLayout.Unit.Millimeter,
    )
    assert printer.setPageLayout(changed_layout)
    printer.setFullPage(False)
    dialog.paintRequested.emit(printer)

    actual = printer.pageLayout()
    margins = actual.margins(QPageLayout.Unit.Millimeter)
    assert calls == [(printer, None)]
    assert actual.pageSize().id() == QPageSize.PageSizeId.A4
    assert actual.orientation() == orientation
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0)
    assert printer.fullPage()


@pytest.mark.parametrize("orientation", _ORIENTATIONS)
def test_print_preview_configures_zero_margin_on_printer_with_minimum_margins(
    qtbot, monkeypatch, orientation: QPageLayout.Orientation
) -> None:
    from PySide6.QtGui import QPageLayout

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    original = QPrinter.setPageLayout

    def reject_zero_margin_outside_full_page(printer: QPrinter, layout: QPageLayout) -> bool:
        if layout.mode() != QPageLayout.Mode.FullPageMode:
            return False
        return original(printer, layout)

    printer = _pdf_printer(Path("preview.pdf"))
    calls: list[object] = []
    dialog = PrintPreviewDialog(printer, orientation, 1, lambda device, pages: calls.append(device))
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QPrinter, "setPageLayout", reject_zero_margin_outside_full_page)

    dialog.paintRequested.emit(printer)

    assert calls == [printer]
    assert printer.fullPage()


@pytest.mark.parametrize("orientation", _ORIENTATIONS)
def test_print_preview_fits_work_area(qtbot, orientation: QPageLayout.Orientation) -> None:

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    dialog = PrintPreviewDialog(
        _pdf_printer(Path("preview-work-area.pdf")),
        orientation,
        1,
        lambda _device, _pages: None,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(10)

    assert_fits_work_area(dialog)


@pytest.mark.parametrize(
    ("print_range", "first_page", "last_page", "expected"),
    [
        ("all", 1, 4, None),
        ("range", 2, 3, [2, 3]),
        ("range", 4, 4, [4]),
    ],
)
def test_print_preview_passes_selected_original_page_numbers(
    monkeypatch,
    qtbot,
    print_range: str,
    first_page: int,
    last_page: int,
    expected: list[int] | None,
) -> None:
    from PySide6.QtGui import QPageLayout
    from PySide6.QtPrintSupport import QAbstractPrintDialog

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    enum_range = QAbstractPrintDialog.PrintRange
    selected_range = enum_range.AllPages if print_range == "all" else enum_range.PageRange

    class FakePrintDialog:
        def __init__(self, printer, _parent) -> None:
            self.printer = printer
            self.options = {}

        def setMinMax(self, minimum, maximum) -> None:
            self.minimum, self.maximum = minimum, maximum

        def setFromTo(self, first, last) -> None:
            self.from_page, self.to_page = first, last

        def setOption(self, option, enabled) -> None:
            self.options[option] = enabled

        def setPrintRange(self, print_range) -> None:
            self.configured_range = print_range

        def exec(self) -> int:
            changed_layout = QPageLayout(
                QPageSize.PageSizeId.Letter,
                QPageLayout.Orientation.Landscape,
                QMarginsF(4, 4, 4, 4),
                QPageLayout.Unit.Millimeter,
            )
            assert self.printer.setPageLayout(changed_layout)
            self.printer.setFullPage(False)
            return QDialog.DialogCode.Accepted

        def printRange(self):
            return selected_range

        def fromPage(self) -> int:
            return first_page

        def toPage(self) -> int:
            return last_page

    fake_dialogs: list[FakePrintDialog] = []
    original_init = FakePrintDialog.__init__

    def record_fake_dialog(self, printer, parent) -> None:
        original_init(self, printer, parent)
        fake_dialogs.append(self)

    monkeypatch.setattr(FakePrintDialog, "__init__", record_fake_dialog)
    monkeypatch.setattr(preview_dialog, "QPrintDialog", FakePrintDialog)
    printer = _pdf_printer(Path("preview-range.pdf"))
    calls: list[tuple[object, object, bool]] = []

    def render(device, page_numbers) -> None:
        calls.append((device, page_numbers, device.fullPage()))

    dialog = PrintPreviewDialog(
        printer,
        QPageLayout.Orientation.Landscape,
        4,
        render,
    )
    qtbot.addWidget(dialog)
    dialog.print_action.trigger()

    assert calls == [(printer, expected, True)]
    fake_dialog = fake_dialogs[0]
    assert (fake_dialog.minimum, fake_dialog.maximum) == (1, 4)
    assert (fake_dialog.from_page, fake_dialog.to_page) == (1, 4)
    assert fake_dialog.configured_range == enum_range.AllPages
    assert fake_dialog.options[QAbstractPrintDialog.PrintDialogOption.PrintPageRange]
    assert not fake_dialog.options[QAbstractPrintDialog.PrintDialogOption.PrintSelection]
    assert not fake_dialog.options[QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage]

    actual = printer.pageLayout()
    margins = actual.margins(QPageLayout.Unit.Millimeter)
    assert actual.pageSize().id() == QPageSize.PageSizeId.A4
    assert actual.orientation() == QPageLayout.Orientation.Landscape
    assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == (0, 0, 0, 0)


@pytest.mark.parametrize("orientation", _ORIENTATIONS)
@pytest.mark.parametrize("failure", ["page_layout", "full_page"])
def test_print_preview_does_not_render_when_page_configuration_fails(
    monkeypatch, qtbot, failure: str, orientation: QPageLayout.Orientation
) -> None:
    from PySide6.QtPrintSupport import QPrinter

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(error_handling.QMessageBox, "critical", lambda *args: messages.append(args))
    printer = _pdf_printer(Path(f"preview-{failure}-error.pdf"))
    calls: list[object] = []
    dialog = PrintPreviewDialog(
        printer,
        orientation,
        1,
        lambda device, _pages: calls.append(device),
    )
    qtbot.addWidget(dialog)

    if failure == "page_layout":
        monkeypatch.setattr(QPrinter, "setPageLayout", lambda _printer, _layout: False)
    else:
        monkeypatch.setattr(QPrinter, "fullPage", lambda _printer: False)

    dialog.paintRequested.emit(printer)

    assert calls == []
    assert len(messages) == 1


def test_print_preview_cancel_does_not_render(monkeypatch, qtbot) -> None:
    from PySide6.QtGui import QPageLayout
    from PySide6.QtWidgets import QDialog

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    class CancelledPrintDialog:
        def __init__(self, _printer, _parent) -> None:
            pass

        def setMinMax(self, _minimum, _maximum) -> None:
            pass

        def setFromTo(self, _first, _last) -> None:
            pass

        def setOption(self, _option, _enabled) -> None:
            pass

        def setPrintRange(self, _print_range) -> None:
            pass

        def exec(self) -> int:
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(preview_dialog, "QPrintDialog", CancelledPrintDialog)
    calls: list[object] = []
    dialog = PrintPreviewDialog(
        _pdf_printer(Path("preview-cancel.pdf")),
        QPageLayout.Orientation.Portrait,
        2,
        lambda device, _pages: calls.append(device),
    )
    qtbot.addWidget(dialog)

    dialog.print_action.trigger()

    assert calls == []


def test_print_preview_rejects_invalid_page_range(qtbot) -> None:
    from PySide6.QtGui import QPageLayout
    from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    class InvalidRangeDialog:
        def printRange(self):
            return QAbstractPrintDialog.PrintRange.PageRange

        def fromPage(self) -> int:
            return 0

        def toPage(self) -> int:
            return 2

    dialog = PrintPreviewDialog(
        _pdf_printer(Path("preview-invalid-range.pdf")),
        QPageLayout.Orientation.Portrait,
        2,
        lambda _device, _pages: None,
    )
    qtbot.addWidget(dialog)

    with pytest.raises(ValueError, match="範囲が不正"):
        dialog._selected_page_numbers(cast(QPrintDialog, InvalidRangeDialog()))


def test_print_preview_reads_multiple_page_ranges_from_printer(qtbot) -> None:
    from PySide6.QtGui import QPageLayout, QPageRanges
    from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    printer = _pdf_printer(Path("preview-multiple-ranges.pdf"))
    page_ranges = QPageRanges()
    page_ranges.addRange(2, 3)
    page_ranges.addPage(5)
    printer.setPageRanges(page_ranges)

    class MultipleRangeDialog:
        def printer(self):
            return printer

        def printRange(self):
            return QAbstractPrintDialog.PrintRange.PageRange

        def fromPage(self) -> int:
            return 2

        def toPage(self) -> int:
            return 5

    dialog = PrintPreviewDialog(
        printer,
        QPageLayout.Orientation.Portrait,
        5,
        lambda _device, _pages: None,
    )
    qtbot.addWidget(dialog)

    assert dialog._selected_page_numbers(cast(QPrintDialog, MultipleRangeDialog())) == [2, 3, 5]


def test_print_preview_rejects_page_range_beyond_document(qtbot) -> None:
    from PySide6.QtGui import QPageLayout, QPageRanges
    from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    printer = _pdf_printer(Path("preview-out-of-range.pdf"))
    page_ranges = QPageRanges()
    page_ranges.addRange(2, 4)
    printer.setPageRanges(page_ranges)

    class OutOfRangeDialog:
        def printer(self):
            return printer

        def printRange(self):
            return QAbstractPrintDialog.PrintRange.PageRange

        def fromPage(self) -> int:
            return 2

        def toPage(self) -> int:
            return 4

    dialog = PrintPreviewDialog(
        printer,
        QPageLayout.Orientation.Portrait,
        3,
        lambda _device, _pages: None,
    )
    qtbot.addWidget(dialog)

    with pytest.raises(ValueError, match="範囲が不正"):
        dialog._selected_page_numbers(cast(QPrintDialog, OutOfRangeDialog()))


@pytest.mark.parametrize("orientation", _ORIENTATIONS)
def test_print_preview_paint_signal_reports_failure_once_and_closes_later(
    monkeypatch, qtbot, caplog, orientation: QPageLayout.Orientation
) -> None:

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(error_handling.QMessageBox, "critical", lambda *args: messages.append(args))
    printer = _pdf_printer(Path("preview-error.pdf"))
    draw_count = 0

    def render(_device, _pages) -> None:
        nonlocal draw_count
        draw_count += 1
        raise RuntimeError("描画テスト失敗")

    dialog = PrintPreviewDialog(
        printer,
        orientation,
        1,
        render,
    )
    qtbot.addWidget(dialog)
    rejected: list[bool] = []
    dialog.rejected.connect(lambda: rejected.append(True))

    dialog.paintRequested.emit(printer)
    dialog.paintRequested.emit(printer)
    dialog.print_action.trigger()
    qtbot.wait(10)

    assert draw_count == 1
    assert len(messages) == 1
    assert "予期しないエラー" in str(messages[0][2])
    assert "描画テスト失敗" in caplog.text
    assert rejected == [True]


def test_print_preview_aborts_failed_print_without_duplicate_notification(
    monkeypatch, qtbot, caplog
) -> None:
    from PySide6.QtGui import QPageLayout
    from PySide6.QtPrintSupport import QPrinter

    from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog

    messages: list[tuple[object, ...]] = []
    abort_calls: list[QPrinter] = []
    monkeypatch.setattr(error_handling.QMessageBox, "critical", lambda *args: messages.append(args))
    monkeypatch.setattr(QPrinter, "abort", lambda printer: abort_calls.append(printer) or False)
    printer = _pdf_printer(Path("preview-abort.pdf"))

    def render(_device, _pages) -> None:
        raise RuntimeError("実印刷失敗")

    dialog = PrintPreviewDialog(
        printer,
        QPageLayout.Orientation.Portrait,
        1,
        render,
    )
    qtbot.addWidget(dialog)
    dialog._printing = True

    assert not dialog._render_safely(printer, [1])
    qtbot.wait(10)

    assert abort_calls == [printer]
    assert len(messages) == 1
    assert "中止できませんでした" in caplog.text


@pytest.fixture(autouse=True)
def _run_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_label_print_dialog_page_count_follows_first_page_slots(qtbot) -> None:
    from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog

    dialog = LabelPrintDialog([_item_row()])
    qtbot.addWidget(dialog)
    dialog.clear_all_button.click()
    dialog.slot_checkboxes[0].setChecked(True)
    dialog.slot_checkboxes[5].setChecked(True)
    dialog.quantity_spins[0].setValue(14)

    assert dialog.first_page_slots == [0, 5]
    assert dialog.count_label.text() == "合計 14 枚 / 用紙 2 枚"

    dialog.quantity_spins[0].setValue(15)
    assert dialog.count_label.text() == "合計 15 枚 / 用紙 3 枚"


class _MeasuringPainter:
    """実際の QPainter に委譲し、描画文字列と描画先メトリクスでの幅を記録する。"""

    def __init__(self) -> None:
        self._painter = QPainter()
        self.drawn: list[tuple[str, int, float, bool, str, float, int, int, QRect]] = []

    def begin(self, device: object) -> bool:
        return self._painter.begin(cast(QPagedPaintDevice, device))

    def end(self) -> bool:
        return self._painter.end()

    def setFont(self, font: QFont) -> None:
        self._painter.setFont(font)

    def fontMetrics(self) -> QFontMetrics:
        return self._painter.fontMetrics()

    def drawText(self, point: QPointF, text: str) -> None:
        font = self._painter.font()
        metrics = self._painter.fontMetrics()
        width = metrics.horizontalAdvance(text)
        self.drawn.append(
            (
                text,
                width,
                font.pointSizeF(),
                font.bold(),
                font.family(),
                point.y(),
                metrics.ascent(),
                metrics.descent(),
                metrics.boundingRect(text),
            )
        )
        self._painter.drawText(point, text)


def _pdf_writer(path: Path, dpi: int) -> QPdfWriter:
    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(dpi)
    return writer


@pytest.mark.parametrize("dpi", [300, 1200])
def test_render_labels_elides_long_text_within_inner_width_at_device_dpi(
    monkeypatch, qapp, tmp_path: Path, dpi: int
) -> None:
    painter = _MeasuringPainter()
    monkeypatch.setattr(label_renderer, "QPainter", lambda: painter)
    long_item = replace(
        _item_row(
            "備" * 200,
            manufacturer_part_number="M" * 200,
            application="用" * 200,
        ),
        name="品" * 200,
        code="C" * 200,
        location_name="場" * 200,
    )
    short_item = replace(
        _item_row("備考", manufacturer_part_number="M-1", application="補修用"),
        id=2,
        name="短い品名",
        code="S-1",
        location_name="棚B",
    )

    label_renderer.render_labels(
        _pdf_writer(tmp_path / f"width-{dpi}.pdf", dpi),
        [LabelPlacement(1, 0, 1), LabelPlacement(1, 1, 2)],
        {1: long_item, 2: short_item},
        LABEL_72212,
        0.0,
        0.0,
    )

    inner_width = math.floor((LABEL_72212.label_width_mm - 6.0) * dpi / 25.4)
    long_rows, short_rows = painter.drawn[:6], painter.drawn[6:]
    assert [row[2] for row in long_rows] == [10] * 6
    assert [row[3] for row in long_rows] == [True, True, False, False, False, False]
    for row, original in zip(
        long_rows,
        (
            long_item.name,
            long_item.manufacturer_part_number,
            long_item.application,
            long_item.code,
            long_item.location_name,
            long_item.note,
        ),
        strict=True,
    ):
        text, width = row[0], row[1]
        assert original is not None
        assert text != original
        assert text.endswith("…")
        assert width <= inner_width
    assert [row[0] for row in short_rows] == [
        "短い品名",
        "M-1",
        "補修用",
        "S-1",
        "棚B",
        "備考",
    ]
    assert all(row[1] <= inner_width for row in short_rows)


@pytest.mark.parametrize("dpi", [300, 1200])
def test_render_labels_keeps_text_within_vertical_label_insets_at_device_dpi(
    monkeypatch, qapp, tmp_path: Path, dpi: int
) -> None:
    painter = _MeasuringPainter()
    monkeypatch.setattr(label_renderer, "QPainter", lambda: painter)
    short_item = replace(
        _item_row("備考 gyp"),
        name="品名 日本語 Agyp",
        manufacturer_part_number="型番 gyp",
        application="用途 日本語 gyp",
        code="ID gyp",
        location_name="棚 gyp",
    )
    long_item = replace(
        _item_row("備考 " + "日本語 gyp " * 80),
        id=2,
        name="品名 " + "日本語 Agyp " * 80,
        manufacturer_part_number="型番 " + "日本語 gyp " * 80,
        application="用途 " + "日本語 gyp " * 80,
        code="ID " + "日本語 gyp " * 80,
        location_name="棚 " + "日本語 gyp " * 80,
    )
    placements = [LabelPlacement(1, 0, 1), LabelPlacement(1, 1, 2)]

    label_renderer.render_labels(
        _pdf_writer(tmp_path / f"vertical-{dpi}.pdf", dpi),
        placements,
        {1: short_item, 2: long_item},
        LABEL_72212,
        0.0,
        0.0,
    )

    assert len(painter.drawn) == 12
    offsets_mm = (3.0, 9.0, 15.0, 21.0, 27.0, 33.0)
    bold_rows = (True, True, False, False, False, False)
    # QFontMetrics の整数 boundingRect と mm→pixel 換算の丸め差として 1px だけ許容する。
    rounding_tolerance_px = 1
    for item_index, slot in enumerate((0, 1)):
        slot_y = slot_rect_mm(LABEL_72212, slot)[1]
        label_top = (slot_y + 3.0) * dpi / 25.4
        label_bottom = (slot_y + LABEL_72212.label_height_mm - 3.0) * dpi / 25.4
        item_rows = painter.drawn[item_index * 6 : (item_index + 1) * 6]
        for row_index, (row, offset_mm, bold) in enumerate(
            zip(item_rows, offsets_mm, bold_rows, strict=True)
        ):
            _, width, point_size, actual_bold, family, baseline, ascent, descent, bounds = row
            assert point_size == 10
            assert actual_bold is bold
            assert family == fonts.print_font(10, bold).family()
            assert ascent > 0
            assert descent >= 0
            assert baseline == pytest.approx((slot_y + offset_mm) * dpi / 25.4 - bounds.top())
            text_top = baseline + bounds.top()
            text_bottom = baseline + bounds.bottom()
            assert text_top >= label_top - rounding_tolerance_px
            assert text_bottom <= label_bottom + rounding_tolerance_px
            if row_index < len(item_rows) - 1:
                next_row = item_rows[row_index + 1]
                next_top = next_row[5] + next_row[8].top()
                assert text_bottom <= next_top + rounding_tolerance_px
            assert width <= math.floor((LABEL_72212.label_width_mm - 6.0) * dpi / 25.4)


def test_render_labels_offsets_move_drawing_position(monkeypatch) -> None:
    dpi = 300
    results: list[tuple[float, float]] = []
    for offset_left, offset_top in ((0.0, 0.0), (2.0, -1.0)):
        painter = _FakePainter()
        _install_fake_painter(monkeypatch, painter)
        label_renderer.render_labels(
            _as_paged_device(_FakeDevice(dpi=dpi)),
            [LabelPlacement(1, 3, 1)],
            {1: _item_row()},
            LABEL_72212,
            offset_left,
            offset_top,
        )
        results.append((painter.draw_calls[0][0], painter.draw_calls[0][1]))

    assert results[1][0] - results[0][0] == pytest.approx(2.0 * dpi / 25.4)
    assert results[1][1] - results[0][1] == pytest.approx(-1.0 * dpi / 25.4)


def test_render_labels_handles_very_long_text_in_pdf(tmp_path: Path, qapp) -> None:
    long_item = replace(
        _item_row(
            "備" * 500,
            manufacturer_part_number="型" * 500,
            application="用" * 500,
        ),
        name="品" * 500,
        location_name="場" * 500,
    )
    pdf_path = tmp_path / "long.pdf"

    label_renderer.render_labels(
        _pdf_printer(pdf_path),
        [LabelPlacement(1, 0, 1)],
        {1: long_item},
        LABEL_72212,
        0.0,
        0.0,
    )

    assert pdf_path.read_bytes().startswith(b"%PDF-")


@pytest.mark.parametrize(
    ("page_numbers", "expected_pages"),
    [(None, 3), ([1, 2, 3], 3), ([2, 3], 2), ([3], 1), ([1, 3], 2), ([2, 2], 1)],
)
def test_render_labels_page_range_outputs_only_requested_pages(
    qapp, tmp_path: Path, page_numbers: list[int] | None, expected_pages: int
) -> None:
    placements = assign_label_slots([LabelEntry(item_id=1, copies=30)], range(12))
    pdf_path = tmp_path / "range.pdf"

    label_renderer.render_labels(
        _pdf_printer(pdf_path),
        placements,
        {1: _item_row()},
        LABEL_72212,
        0.0,
        0.0,
        page_numbers=page_numbers,
    )

    document = QPdfDocument()
    document.load(str(pdf_path))
    assert document.pageCount() == expected_pages


@pytest.mark.parametrize(
    ("page_numbers", "expected_new_pages", "expected_slots"),
    [
        ([2], 0, [0, 1, 2]),
        ([1], 0, [5]),
        ([1, 2], 1, [5, 0, 1, 2]),
        (None, 1, [5, 0, 1, 2]),
    ],
)
def test_render_labels_range_keeps_original_slots_without_reapplying_first_page_checks(
    monkeypatch,
    page_numbers: list[int] | None,
    expected_new_pages: int,
    expected_slots: list[int],
) -> None:
    painter = _FakePainter()
    device = _FakeDevice(dpi=300)
    _install_fake_painter(monkeypatch, painter)
    placements = assign_label_slots([LabelEntry(item_id=1, copies=4)], [5])

    label_renderer.render_labels(
        _as_paged_device(device),
        placements,
        {1: _item_row()},
        LABEL_72212,
        0.0,
        0.0,
        page_numbers=page_numbers,
    )

    expected_xs = [
        (slot_rect_mm(LABEL_72212, slot)[0] + 3.0) * 300 / 25.4 for slot in expected_slots
    ]
    drawn_xs = [call[0] for call in painter.draw_calls[0::6]]
    assert device.new_page_calls == expected_new_pages
    assert len(painter.draw_calls) == 6 * len(expected_slots)
    assert drawn_xs == pytest.approx(expected_xs)


def test_render_labels_drawing_error_stops_later_pages_and_ends_painter(monkeypatch) -> None:
    painter = _FakePainter(draw_error=ValueError("draw failed"))
    device = _FakeDevice()
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(ValueError, match="draw failed"):
        label_renderer.render_labels(
            _as_paged_device(device),
            [LabelPlacement(1, 0, 1), LabelPlacement(2, 0, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
        )

    assert painter.begin_calls == 1
    assert painter.end_calls == 1
    assert device.new_page_calls == 0


class _EndRaisingPainter(_FakePainter):
    def end(self) -> bool:
        self.end_calls += 1
        raise OSError("end failed")


def test_render_labels_keeps_drawing_error_when_end_raises(monkeypatch, caplog) -> None:
    painter = _EndRaisingPainter(draw_error=ValueError("draw failed"))
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
    assert "終了処理にも失敗" in caplog.text


def test_render_labels_reports_end_exception_without_drawing_error(monkeypatch) -> None:
    painter = _EndRaisingPainter()
    _install_fake_painter(monkeypatch, painter)

    with pytest.raises(OSError, match="end failed"):
        label_renderer.render_labels(
            _as_paged_device(_FakeDevice()),
            [LabelPlacement(1, 0, 1)],
            {1: _item_row()},
            LABEL_72212,
            0.0,
            0.0,
        )


def test_render_labels_leaves_deselected_slots_blank(qapp, tmp_path: Path) -> None:
    scale = 3
    placements = assign_label_slots([LabelEntry(item_id=1, copies=2)], [0, 3])
    pdf_path = tmp_path / "slots.pdf"

    label_renderer.render_labels(
        _pdf_printer(pdf_path), placements, {1: _item_row()}, LABEL_72212, 0.0, 0.0
    )

    document = QPdfDocument()
    document.load(str(pdf_path))
    image = document.render(0, QSize(210 * scale, 297 * scale))
    blank = QColor(image.pixelColor(0, 0))

    for slot in range(LABEL_72212.columns * LABEL_72212.rows):
        x, y, width, height = slot_rect_mm(LABEL_72212, slot)
        crop = image.copy(
            QRect(
                round(x * scale) + 2,
                round(y * scale) + 2,
                round(width * scale) - 4,
                round(height * scale) - 4,
            )
        )
        empty = QImage(crop.size(), crop.format())
        empty.fill(blank)
        assert (crop == empty) is (slot not in (0, 3)), f"slot {slot}"
