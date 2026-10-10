from dataclasses import replace
from datetime import datetime

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor

from inventory_manager_mini.core.models import MovementRow, Reason
from inventory_manager_mini.ui.models.movement_table_model import MovementTableModel


def _movement(movement_id: int = 1, **changes: object) -> MovementRow:
    row = MovementRow(
        id=movement_id,
        item_id=1,
        client_id=1,
        purchaser_id=1,
        staff_id=1,
        reason=Reason.IN,
        delta=1,
        unit_price=None,
        used_for=None,
        reversal_of=None,
        note=None,
        moved_at="2026-01-01 00:00:00",
        code="TO-0001",
        item_name="品目1",
        client_name="総務",
        purchaser_name="本部",
        staff_name="担当者A",
        is_reversed=False,
    )
    return replace(row, **changes)


def test_headers_and_newest_first_order() -> None:
    model = MovementTableModel()
    model.set_rows(
        [
            _movement(2, moved_at="2026-01-02 00:00:00"),
            _movement(3, moved_at="2026-01-02 00:00:00"),
            _movement(4, moved_at="2026-01-01 00:00:00"),
        ]
    )

    assert model.columnCount() == 12
    assert [model.headerData(column, Qt.Orientation.Horizontal) for column in range(12)] == [
        "履歴 ID",
        "日時",
        "種別",
        "数量",
        "単価",
        "原価金額",
        "クライアント",
        "発注主体",
        "担当者",
        "使用先",
        "メモ",
        "取り消し状態",
    ]
    assert [model.row_at(row).id for row in range(model.rowCount())] == [3, 2, 4]


def test_display_values_roles_and_canceled_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inventory_manager_mini.ui.models import movement_table_model

    monkeypatch.setattr(
        movement_table_model,
        "format_local",
        lambda timestamp: datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S local"
        ),
    )
    model = MovementTableModel()
    model.set_rows(
        [
            _movement(
                10,
                reason=Reason.OUT,
                delta=-1234,
                unit_price=123456,
                cost_amount=1234567,
                used_for="会議室",
                note="備品補充",
                moved_at="2026-02-03 04:05:06",
                is_reversed=True,
            ),
            _movement(11, reversal_of=10, delta=1234, cost_amount=-1234567),
        ]
    )
    display = Qt.ItemDataRole.DisplayRole
    raw = Qt.ItemDataRole.UserRole

    assert model.data(model.index(0, 1), display) == "2026-02-03 04:05:06 local"
    assert model.data(model.index(0, 2), display) == "出庫"
    assert model.data(model.index(0, 3), display) == "-1,234"
    assert model.data(model.index(0, 4), display) == "123,456円"
    assert model.data(model.index(0, 5), display) == "1,234,567円"
    assert model.data(model.index(0, 6), display) == "総務"
    assert model.data(model.index(0, 7), display) == "本部"
    assert model.data(model.index(0, 8), display) == "担当者A"
    assert model.data(model.index(0, 9), display) == "会議室"
    assert model.data(model.index(0, 10), display) == "備品補充"
    assert model.data(model.index(0, 11), display) == "取り消し済み"
    assert model.data(model.index(0, 3), raw) == -1234
    assert model.data(model.index(0, 5), raw) == 1234567
    assert model.data(model.index(1, 3), display) == "+1,234"
    assert model.data(model.index(1, 5), display) == "-1,234,567円"
    assert model.data(model.index(1, 11), display) == "#10 の取り消し"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.ForegroundRole) == QBrush(
        QColor(Qt.GlobalColor.gray)
    )
    assert model.data(model.index(1, 0), Qt.ItemDataRole.ForegroundRole) == QBrush(
        QColor(Qt.GlobalColor.gray)
    )


def test_none_values_are_blank_and_numeric_columns_are_right_aligned() -> None:
    model = MovementTableModel()
    model.set_rows([_movement(1234)])
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    assert model.data(model.index(0, 0)) == "1234"
    assert model.data(model.index(0, 4)) == ""
    assert model.data(model.index(0, 5)) == ""
    assert model.data(model.index(0, 9)) == ""
    assert model.data(model.index(0, 10)) == ""
    assert model.data(model.index(0, 11)) == ""
    for column in (0, 3, 4, 5):
        assert model.data(model.index(0, column), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(0, 6), Qt.ItemDataRole.TextAlignmentRole) is None


def test_set_rows_and_row_lookups() -> None:
    model = MovementTableModel()
    model.set_rows([_movement(10), _movement(20)])

    assert model.rowCount() == 2
    assert model.row_at(0).id == 20
    assert model.row_of(10) == 1
    assert model.row_of(99) is None

    model.set_rows([_movement(30)])

    assert model.rowCount() == 1
    assert model.row_at(0).id == 30
    assert model.row_of(10) is None
