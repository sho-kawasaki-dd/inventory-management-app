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
    model.set_rows([_row(client_id=1, client_name="総務", purchaser_id=2, purchaser_name="本部")])
    headers = [model.headerData(column, Qt.Orientation.Horizontal) for column in range(7)]
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    assert model.columnCount() == 7
    assert headers == [
        "年度/月",
        "クライアント",
        "発注主体",
        "入庫数",
        "出庫数",
        "支出額",
        "単価未登録数量",
    ]
    assert [model.data(model.index(0, column)) for column in range(7)] == [
        "2026-04",
        "総務",
        "本部",
        "1,234",
        "5,678",
        "901,234円",
        "12",
    ]
    assert model.data(model.index(0, 3), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(0, 5), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(0, 2), Qt.ItemDataRole.TextAlignmentRole) is None


def test_unfiltered_axes_and_empty_data() -> None:
    model = DashboardTableModel()
    model.set_rows([_row()])

    assert model.data(model.index(0, 1)) == "(すべて)"
    assert model.data(model.index(0, 2)) == "(すべて)"

    model.set_rows([])
    assert model.rowCount() == 0
    assert model.columnCount() == 7


def test_disposal_model_shows_only_rows_with_disposal_activity() -> None:
    model = DashboardTableModel(disposal_only=True)
    model.set_rows(
        [
            _row(disposed_quantity=0, disposal_amount=0),
            _row(period_label="2026-05", disposed_quantity=12, disposal_amount=0),
            _row(period_label="2026-06", disposed_quantity=0, disposal_amount=3456),
        ]
    )

    assert model.columnCount() == 5
    assert [model.headerData(column, Qt.Orientation.Horizontal) for column in range(5)] == [
        "年度/月",
        "クライアント",
        "発注主体",
        "廃棄数",
        "廃棄額",
    ]
    assert model.rowCount() == 2
    assert [model.row_at(row).period_label for row in range(model.rowCount())] == [
        "2026-05",
        "2026-06",
    ]
    assert model.data(model.index(0, 3)) == "12"
    assert model.data(model.index(1, 4)) == "3,456円"

    model.set_rows([_row()])
    assert model.rowCount() == 0
