from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from inventory_manager_mini.core.models import ItemRow, PurchaseInfo
from inventory_manager_mini.ui.dialogs.low_stock_notice_dialog import (
    LowStockNoticeDialog,
    LowStockNoticeTableModel,
)


def _row(index: int, **changes: object) -> ItemRow:
    row = ItemRow(
        id=index,
        client_id=1,
        purchaser_id=1,
        code=f"ST-{index:04d}",
        name=f"品目{index}",
        category_id=2,
        location_id=None,
        unit="個",
        quantity=index,
        reorder_threshold=1000 + index,
        reorder_quantity=None,
        purchase_url=None,
        supplier=None,
        manufacturer_part_number=None,
        application=None,
        reference_price=None,
        note=None,
        is_active=True,
        created_at="2026-01-01 00:00:00",
        updated_at="2026-01-01 00:00:00",
        client_name="総務",
        purchaser_name="本部",
        category_path="備品 / 文具",
        location_name=None,
        is_low_stock=True,
        purchase_info=PurchaseInfo(None, None),
    )
    return replace(row, **changes)


def _values(model: LowStockNoticeTableModel, row: int) -> list[str]:
    return [model.index(row, column).data() for column in range(model.columnCount())]


def test_model_has_five_columns_and_right_aligned_numbers() -> None:
    model = LowStockNoticeTableModel()
    model.set_rows([_row(1)])

    headers = [
        model.headerData(column, Qt.Orientation.Horizontal) for column in range(model.columnCount())
    ]
    assert headers == ["管理番号", "品名", "数量", "閾値", "発注主体"]
    assert _values(model, 0) == ["ST-0001", "品目1", "1", "1,001", "本部"]
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    assert model.index(0, 2).data(Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.index(0, 3).data(Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.index(0, 1).data(Qt.ItemDataRole.TextAlignmentRole) is None
    assert model.index(5, 0).data() is None
    assert model.index(0, 9).data() is None
    assert model.headerData(0, Qt.Orientation.Vertical) is None
    assert model.rowCount(model.index(0, 0)) == 0
    assert model.columnCount(model.index(0, 0)) == 0


def test_dialog_smoke_shows_rows_count_and_close_button(qtbot) -> None:
    dialog = LowStockNoticeDialog([_row(1), _row(2)])
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.model.rowCount() == 2
    assert dialog.model.columnCount() == 5
    assert "2 件" in dialog.message_label.text()
    assert _values(dialog.model, 1) == ["ST-0002", "品目2", "2", "1,002", "本部"]
    assert dialog.close_button.text() == "閉じる"
    assert not dialog.table.isSortingEnabled()


def test_close_button_accepts_dialog(qtbot) -> None:
    dialog = LowStockNoticeDialog([_row(1)])
    qtbot.addWidget(dialog)
    dialog.show()

    with qtbot.waitSignal(dialog.accepted, timeout=1000):
        qtbot.mouseClick(dialog.close_button, Qt.MouseButton.LeftButton)


@pytest.mark.parametrize("scale", [1.0, 1.5])
def test_dialog_fits_and_scrolls_with_long_names_and_many_rows(qtbot, scale: float) -> None:
    rows = [_row(index, name="長い品名" * 100) for index in range(1, 201)]
    dialog = LowStockNoticeDialog(rows)
    qtbot.addWidget(dialog)
    original_font = dialog.font()
    available = dialog.screen().availableGeometry()
    try:
        font = QFont(original_font)
        font.setPointSizeF(original_font.pointSizeF() * scale)
        dialog.setFont(font)
        dialog.show()
        qtbot.waitExposed(dialog)

        assert available.contains(dialog.frameGeometry())
        assert dialog.table.verticalScrollBar().maximum() > 0
        assert not dialog.close_button.isHidden()
        assert dialog.rect().contains(dialog.close_button.geometry())
        dialog.table.verticalScrollBar().setValue(dialog.table.verticalScrollBar().maximum())
        assert dialog.close_button.isEnabled()
    finally:
        dialog.setFont(original_font)
