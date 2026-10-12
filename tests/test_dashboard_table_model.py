from dataclasses import replace
from datetime import date

from PySide6.QtCore import Qt

from inventory_manager_mini.core.models import DashboardRow
from inventory_manager_mini.ui.models.dashboard_table_model import DashboardTableModel


def _row(**changes: object) -> DashboardRow:
    row = DashboardRow(
        period_label="2026-04",
        period_start=date(2026, 4, 1),
        client_id=None,
        client_name=None,
        purchaser_id=None,
        purchaser_name=None,
        category_id=None,
        category_name=None,
        item_id=None,
        item_code=None,
        item_name=None,
        inbound_quantity=1234,
        outbound_quantity=5678,
        expenditure=901234,
        unpriced_issue_quantity=12,
        disposed_quantity=0,
        disposal_amount=0,
    )
    return replace(row, **changes)


def test_breakdown_columns_values_and_alignment() -> None:
    model = DashboardTableModel()
    model.set_rows(
        [
            _row(
                client_id=1,
                client_name="総務",
                purchaser_id=2,
                purchaser_name="本部",
                category_id=3,
                category_name="電気 > ケーブル",
                item_id=4,
                item_code="A-001",
                item_name="LANケーブル",
            )
        ]
    )
    headers = [model.headerData(column, Qt.Orientation.Horizontal) for column in range(9)]
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    assert model.columnCount() == 9
    assert headers == [
        "年度/月",
        "クライアント",
        "発注主体",
        "カテゴリ",
        "品目",
        "入庫数",
        "出庫数",
        "支出額",
        "単価未登録数量",
    ]
    assert [model.data(model.index(0, column)) for column in range(9)] == [
        "2026-04",
        "総務",
        "本部",
        "電気 > ケーブル",
        "A-001 LANケーブル",
        "1,234",
        "5,678",
        "901,234円",
        "12",
    ]
    assert model.data(model.index(0, 5), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(0, 7), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(0, 2), Qt.ItemDataRole.TextAlignmentRole) is None


def test_unfiltered_axes_and_empty_data() -> None:
    model = DashboardTableModel()
    model.set_rows([_row()])

    assert [model.data(model.index(0, column)) for column in range(5)] == [
        "2026-04",
        "(すべて)",
        "(すべて)",
        "(すべて)",
        "(すべて)",
    ]

    model.set_rows([])
    assert model.rowCount() == 0
    assert model.columnCount() == 9


def test_disposal_model_shows_only_rows_with_disposal_activity() -> None:
    model = DashboardTableModel(disposal_only=True)
    model.set_rows(
        [
            _row(disposed_quantity=0, disposal_amount=0),
            _row(period_label="2026-05", disposed_quantity=12, disposal_amount=0),
            _row(period_label="2026-06", disposed_quantity=0, disposal_amount=3456),
        ]
    )

    assert model.columnCount() == 7
    assert [model.headerData(column, Qt.Orientation.Horizontal) for column in range(7)] == [
        "年度/月",
        "クライアント",
        "発注主体",
        "カテゴリ",
        "品目",
        "廃棄数",
        "廃棄額",
    ]
    assert model.rowCount() == 2
    assert [model.row_at(row).period_label for row in range(model.rowCount())] == [
        "2026-05",
        "2026-06",
    ]
    assert model.data(model.index(0, 5)) == "12"
    assert model.data(model.index(1, 6)) == "3,456円"
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    assert model.data(model.index(0, 5), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(1, 6), Qt.ItemDataRole.TextAlignmentRole) == right

    model.set_rows([_row()])
    assert model.rowCount() == 0
