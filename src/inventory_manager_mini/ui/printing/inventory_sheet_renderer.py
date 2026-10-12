import logging
import math
from collections.abc import Iterable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPagedPaintDevice, QPainter, QPen

from inventory_manager_mini.core.models import ItemRow
from inventory_manager_mini.ui.printing.fonts import print_font
from inventory_manager_mini.ui.printing.layout import InventorySheetSpec, paginate

logger = logging.getLogger(__name__)

_TITLE_FONT_SIZE = 16
_HEADER_FONT_SIZE = 9
_ROW_FONT_SIZE = 9
_FOOTER_FONT_SIZE = 8
_CELL_INSET_MM = 1.0


def _mm_to_device(mm: float, dpi: int) -> float:
    return mm * dpi / 25.4


def _draw_text(
    painter: QPainter,
    text: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    alignment: str,
    dpi_x: int,
    dpi_y: int,
    point_size: int,
    *,
    bold: bool = False,
) -> None:
    painter.setFont(print_font(point_size, bold))
    metrics = painter.fontMetrics()
    inner_width = math.floor(_mm_to_device(width_mm - 2 * _CELL_INSET_MM, dpi_x))
    displayed_text = metrics.elidedText(
        text,
        Qt.TextElideMode.ElideRight,
        max(0, inner_width),
    )
    if not displayed_text:
        return

    text_width = metrics.horizontalAdvance(displayed_text)
    text_x = _mm_to_device(x_mm + _CELL_INSET_MM, dpi_x)
    if alignment == "right":
        text_x += inner_width - text_width
    elif alignment == "center":
        text_x += (inner_width - text_width) / 2
    baseline = (
        _mm_to_device(y_mm, dpi_y)
        + (_mm_to_device(height_mm, dpi_y) - metrics.height()) / 2
        + metrics.ascent()
    )
    painter.drawText(QPointF(text_x, baseline), displayed_text)


def _draw_page_title(
    painter: QPainter,
    spec: InventorySheetSpec,
    row_count: int,
    filter_summary: str,
    dpi_x: int,
    dpi_y: int,
) -> None:
    _draw_text(
        painter,
        f"在庫表（{row_count:,} 件）",
        spec.margin_mm,
        spec.margin_mm + 1.0,
        spec.printable_width_mm,
        9.0,
        "left",
        dpi_x,
        dpi_y,
        _TITLE_FONT_SIZE,
        bold=True,
    )
    _draw_text(
        painter,
        filter_summary,
        spec.margin_mm,
        spec.margin_mm + 12.0,
        spec.printable_width_mm,
        8.0,
        "left",
        dpi_x,
        dpi_y,
        _FOOTER_FONT_SIZE,
    )


def _draw_column_heading(
    painter: QPainter,
    spec: InventorySheetSpec,
    header_y_mm: float,
    dpi_x: int,
    dpi_y: int,
) -> None:
    x_mm = spec.margin_mm
    header_top = _mm_to_device(header_y_mm, dpi_y)
    header_height = _mm_to_device(spec.header_height_mm, dpi_y)
    printable_width = _mm_to_device(spec.printable_width_mm, dpi_x)
    painter.fillRect(
        QRectF(
            _mm_to_device(spec.margin_mm, dpi_x),
            header_top,
            printable_width,
            header_height,
        ),
        QColor(232, 232, 232),
    )
    for column in spec.columns:
        _draw_text(
            painter,
            column.heading,
            x_mm,
            header_y_mm,
            column.width_mm,
            spec.header_height_mm,
            column.alignment,
            dpi_x,
            dpi_y,
            _HEADER_FONT_SIZE,
            bold=True,
        )
        x_mm += column.width_mm

    painter.setPen(QPen(QColor(0, 0, 0), 1))
    painter.drawLine(
        QPointF(_mm_to_device(spec.margin_mm, dpi_x), header_top + header_height),
        QPointF(
            _mm_to_device(spec.margin_mm, dpi_x) + printable_width,
            header_top + header_height,
        ),
    )


def _draw_inventory_row(
    painter: QPainter,
    row: ItemRow,
    spec: InventorySheetSpec,
    y_mm: float,
    dpi_x: int,
    dpi_y: int,
) -> None:
    x_mm = spec.margin_mm
    for column in spec.columns:
        _draw_text(
            painter,
            column.value_getter(row),
            x_mm,
            y_mm,
            column.width_mm,
            spec.row_height_mm,
            column.alignment,
            dpi_x,
            dpi_y,
            _ROW_FONT_SIZE,
        )
        x_mm += column.width_mm


def _draw_footer(
    painter: QPainter,
    spec: InventorySheetSpec,
    printed_at_local: str,
    page_number: int,
    total_pages: int,
    dpi_x: int,
    dpi_y: int,
) -> None:
    footer_y = spec.paper_height_mm - spec.margin_mm - spec.footer_height_mm
    _draw_text(
        painter,
        f"印刷日時 {printed_at_local}",
        spec.margin_mm,
        footer_y,
        spec.printable_width_mm / 2,
        spec.footer_height_mm,
        "left",
        dpi_x,
        dpi_y,
        _FOOTER_FONT_SIZE,
    )
    _draw_text(
        painter,
        f"ページ {page_number} / {total_pages}",
        spec.margin_mm + spec.printable_width_mm / 2,
        footer_y,
        spec.printable_width_mm / 2,
        spec.footer_height_mm,
        "right",
        dpi_x,
        dpi_y,
        _FOOTER_FONT_SIZE,
    )


def render_inventory_sheet(
    device: QPagedPaintDevice,
    rows: Iterable[ItemRow],
    filter_summary: str,
    printed_at_local: str,
    spec: InventorySheetSpec,
    *,
    page_numbers: Iterable[int] | None = None,
) -> None:
    row_list = list(rows)
    page_ranges = paginate(len(row_list), spec.first_page_rows, spec.other_page_rows)
    if not page_ranges:
        return

    pages_by_number = {
        page_number: row_range for page_number, row_range in enumerate(page_ranges, 1)
    }
    if page_numbers is None:
        output_pages = list(pages_by_number)
    else:
        requested_pages = list(page_numbers)
        if any(
            not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1
            for page_number in requested_pages
        ):
            raise ValueError("ページ番号は1以上の整数で指定してください")
        output_pages = sorted(set(requested_pages))
        if not output_pages or not set(output_pages).issubset(pages_by_number):
            raise ValueError("印刷対象のページがありません")

    dpi_x = device.logicalDpiX()
    dpi_y = device.logicalDpiY()
    if dpi_x <= 0 or dpi_y <= 0:
        raise RuntimeError("描画デバイスの解像度を取得できません")

    painter = QPainter()
    if not painter.begin(device):
        raise RuntimeError("印刷の描画を開始できません")

    drawing_error: BaseException | None = None
    try:
        for page_index, page_number in enumerate(output_pages):
            if page_index > 0 and not device.newPage():
                raise RuntimeError("印刷ページを切り替えられません")

            if page_number == 1:
                _draw_page_title(
                    painter,
                    spec,
                    len(row_list),
                    filter_summary,
                    dpi_x,
                    dpi_y,
                )
                header_y_mm = spec.margin_mm + spec.title_height_mm
            else:
                header_y_mm = spec.margin_mm

            _draw_column_heading(painter, spec, header_y_mm, dpi_x, dpi_y)
            row_y_mm = header_y_mm + spec.header_height_mm
            for row_index in pages_by_number[page_number]:
                _draw_inventory_row(
                    painter,
                    row_list[row_index],
                    spec,
                    row_y_mm,
                    dpi_x,
                    dpi_y,
                )
                row_y_mm += spec.row_height_mm

            _draw_footer(
                painter,
                spec,
                printed_at_local,
                page_number,
                len(page_ranges),
                dpi_x,
                dpi_y,
            )
    except BaseException as error:
        drawing_error = error
        raise
    finally:
        try:
            ended = painter.end()
        except Exception:
            if drawing_error is None:
                raise
            logger.exception("在庫表描画の終了処理にも失敗しました")
        else:
            if not ended:
                if drawing_error is None:
                    raise RuntimeError("印刷の描画を終了できません")
                logger.error("在庫表描画の例外後にQPainter.end()も失敗しました")
