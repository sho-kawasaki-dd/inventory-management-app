import logging
import math
from collections.abc import Iterable, Mapping

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPagedPaintDevice, QPainter

from inventory_manager_mini.core.models import ItemRow
from inventory_manager_mini.ui.printing.fonts import print_font
from inventory_manager_mini.ui.printing.layout import (
    LabelPlacement,
    LabelSpec,
    slot_rect_mm,
)

logger = logging.getLogger(__name__)

_LABEL_INSET_MM = 3.0
_LABEL_TEXT_ROWS = (
    (3.0, 10, True),
    (9.0, 10, True),
    (15.0, 10, False),
    (21.0, 10, False),
    (27.0, 10, False),
    (33.0, 10, False),
)


def _mm_to_device(mm: float, dpi: int) -> float:
    return mm * dpi / 25.4


def _item_label_text(item: ItemRow) -> tuple[str, str, str, str, str, str]:
    application = (
        (item.application or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    )
    note = (item.note or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return (
        item.name,
        item.manufacturer_part_number or "",
        application,
        item.code,
        item.location_name or "",
        note,
    )


def render_labels(
    device: QPagedPaintDevice,
    placements: Iterable[LabelPlacement],
    items_by_id: Mapping[int, ItemRow],
    spec: LabelSpec,
    offset_left_mm: float,
    offset_top_mm: float,
    *,
    page_numbers: Iterable[int] | None = None,
) -> None:
    placement_list = list(placements)
    if not placement_list:
        return

    placements_by_page: dict[int, list[LabelPlacement]] = {}
    for placement in placement_list:
        placements_by_page.setdefault(placement.page_number, []).append(placement)

    available_pages = set(placements_by_page)
    if page_numbers is None:
        output_pages = sorted(available_pages)
    else:
        requested_pages = list(page_numbers)
        if any(
            not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1
            for page_number in requested_pages
        ):
            raise ValueError("ページ番号は1以上の整数で指定してください")
        output_pages = sorted(set(requested_pages))
        if not output_pages or not set(output_pages).issubset(available_pages):
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

            for placement in sorted(
                placements_by_page[page_number], key=lambda current: current.slot
            ):
                x_mm, y_mm, width_mm, _ = slot_rect_mm(
                    spec,
                    placement.slot,
                    offset_left_mm,
                    offset_top_mm,
                )
                inner_x = _mm_to_device(x_mm + _LABEL_INSET_MM, dpi_x)
                inner_width = math.floor(_mm_to_device(width_mm - 2 * _LABEL_INSET_MM, dpi_x))
                item_text = _item_label_text(items_by_id[placement.item_id])

                for (top_offset_mm, point_size, bold), text in zip(
                    _LABEL_TEXT_ROWS, item_text, strict=True
                ):
                    painter.setFont(print_font(point_size, bold))
                    metrics = painter.fontMetrics()
                    elided_text = metrics.elidedText(
                        text,
                        Qt.TextElideMode.ElideRight,
                        inner_width,
                    )
                    if elided_text:
                        baseline = _mm_to_device(y_mm + top_offset_mm, dpi_y) + metrics.ascent()
                        painter.drawText(QPointF(inner_x, baseline), elided_text)
    except BaseException as error:
        drawing_error = error
        raise
    finally:
        try:
            ended = painter.end()
        except Exception:
            if drawing_error is None:
                raise
            logger.exception("ラベル描画の終了処理にも失敗しました")
        else:
            if not ended:
                if drawing_error is None:
                    raise RuntimeError("印刷の描画を終了できません")
                logger.error("ラベル描画の例外後にQPainter.end()も失敗しました")
