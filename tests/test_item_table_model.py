from dataclasses import replace
from datetime import date

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QBrush, QColor

from inventory_manager_mini.core.models import ItemRow, PurchaseInfo
from inventory_manager_mini.ui.models.item_table_model import ItemSortProxyModel, ItemTableModel


def _item(item_id: int = 1, **changes: object) -> ItemRow:
    row = ItemRow(
        id=item_id,
        client_id=1,
        purchaser_id=1,
        code=f"TO-{item_id:04d}",
        name=f"品目{item_id}",
        category_id=1,
        location_id=None,
        unit="個",
        quantity=0,
        reorder_threshold=0,
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
        category_path="備品",
        location_name=None,
        is_low_stock=True,
        purchase_info=PurchaseInfo(None, None),
    )
    return replace(row, **changes)


def test_headers_and_column_count() -> None:
    model = ItemTableModel()

    assert model.columnCount() == 16
    assert [model.headerData(column, Qt.Orientation.Horizontal) for column in range(16)] == [
        "管理番号",
        "品名",
        "メーカー型番",
        "用途",
        "クライアント",
        "発注主体",
        "カテゴリ",
        "保管場所",
        "数量",
        "単位",
        "閾値",
        "推奨発注数",
        "参考価格",
        "仕入先",
        "最終購入日",
        "状態",
    ]


def test_display_values_roles_and_inactive_foreground(monkeypatch: pytest.MonkeyPatch) -> None:
    from inventory_manager_mini.ui.models import item_table_model

    monkeypatch.setattr(item_table_model, "local_date", lambda timestamp: date(2026, 2, 3))
    model = ItemTableModel()
    model.set_rows(
        [
            _item(
                quantity=1234,
                reorder_threshold=12,
                reorder_quantity=2000,
                reference_price=123456,
                supplier="仕入先A",
                manufacturer_part_number="MP-1",
                location_name="倉庫",
                category_path="備品 > 文具",
                is_active=False,
                purchase_info=PurchaseInfo("2026-02-02 23:00:00", 4),
            )
        ]
    )
    display = Qt.ItemDataRole.DisplayRole
    raw = Qt.ItemDataRole.UserRole

    assert model.data(model.index(0, 8), display) == "1,234"
    assert model.data(model.index(0, 11), display) == "2,000"
    assert model.data(model.index(0, 12), display) == "123,456円"
    assert model.data(model.index(0, 14), display) == "2026-02-03"
    assert model.data(model.index(0, 15), display) == "廃止"
    assert model.data(model.index(0, 6), raw) == "備品 > 文具"
    assert model.data(model.index(0, 8), raw) == 1234
    assert model.data(model.index(0, 11), raw) == 2000
    assert model.data(model.index(0, 12), raw) == 123456
    assert model.data(model.index(0, 14), raw) == date(2026, 2, 3)
    assert model.data(model.index(0, 7), display) == "倉庫"
    assert model.data(model.index(0, 2), display) == "MP-1"
    assert model.data(model.index(0, 13), display) == "仕入先A"
    assert model.data(model.index(0, 7), Qt.ItemDataRole.ForegroundRole) == QBrush(
        QColor(Qt.GlobalColor.gray)
    )
    assert model.data(model.index(0, 7), Qt.ItemDataRole.BackgroundRole) is None


def test_background_role_highlights_only_active_low_stock_rows() -> None:
    model = ItemTableModel()
    model.set_rows(
        [
            _item(1, is_low_stock=True),
            _item(2, quantity=5, is_low_stock=False),
            _item(3, is_active=False, is_low_stock=True),
        ]
    )

    for column in range(model.columnCount()):
        assert model.data(model.index(0, column), Qt.ItemDataRole.BackgroundRole) == QBrush(
            QColor("#FFF3CD")
        )
        assert model.data(model.index(1, column), Qt.ItemDataRole.BackgroundRole) is None
        assert model.data(model.index(2, column), Qt.ItemDataRole.BackgroundRole) is None
    assert model.data(model.index(2, 0), Qt.ItemDataRole.ForegroundRole) == QBrush(
        QColor(Qt.GlobalColor.gray)
    )


def test_none_values_are_blank_and_numeric_columns_are_right_aligned() -> None:
    model = ItemTableModel()
    model.set_rows([_item()])
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

    assert model.data(model.index(0, 2)) == ""
    assert model.data(model.index(0, 3)) == ""
    assert model.data(model.index(0, 7)) == ""
    assert model.data(model.index(0, 11)) == ""
    assert model.data(model.index(0, 12)) == ""
    assert model.data(model.index(0, 14)) == ""
    for column in (8, 10, 11, 12):
        assert model.data(model.index(0, column), Qt.ItemDataRole.TextAlignmentRole) == right
    assert model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole) is None
    assert model.data(model.index(0, 3), Qt.ItemDataRole.TextAlignmentRole) is None


def test_set_rows_and_row_lookups() -> None:
    model = ItemTableModel()
    model.set_rows([_item(10), _item(20)])

    assert model.rowCount() == 2
    assert model.row_at(1).id == 20
    assert model.row_of(20) == 1
    assert model.row_of(99) is None

    model.set_rows([_item(30)])

    assert model.rowCount() == 1
    assert model.row_at(0).id == 30
    assert model.row_of(10) is None


@pytest.mark.parametrize(
    ("column", "rows", "expected"),
    [
        (8, [_item(1, quantity=10), _item(2, quantity=2)], [2, 1]),
        (1, [_item(1, name="Zebra"), _item(2, name="Apple")], [2, 1]),
        (
            11,
            [_item(1, reorder_quantity=None), _item(2, reorder_quantity=3)],
            [2, 1],
        ),
    ],
)
def test_proxy_sorts_values_and_places_none_last(column: int, rows, expected: list[int]) -> None:
    source = ItemTableModel()
    source.set_rows(rows)
    proxy = ItemSortProxyModel()
    proxy.setSourceModel(source)
    proxy.sort(column, Qt.SortOrder.AscendingOrder)

    assert [proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(2)] == [
        source.row_at(index - 1).code for index in expected
    ]


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (Qt.SortOrder.AscendingOrder, [2, 3, 1]),
        (Qt.SortOrder.DescendingOrder, [3, 2, 1]),
    ],
)
def test_proxy_sorts_application_and_places_none_last(order, expected: list[int]) -> None:
    source = ItemTableModel()
    source.set_rows(
        [
            _item(1, application=None),
            _item(2, application="用途A"),
            _item(3, application="用途B"),
        ]
    )
    proxy = ItemSortProxyModel()
    proxy.setSourceModel(source)
    proxy.sort(3, order)

    assert [proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(3)] == [
        source.row_at(index - 1).code for index in expected
    ]


def test_proxy_sort_preserves_multiple_selected_source_rows() -> None:
    source = ItemTableModel()
    source.set_rows([_item(1, quantity=10), _item(2, quantity=2), _item(3, quantity=5)])
    proxy = ItemSortProxyModel()
    proxy.setSourceModel(source)
    selection = QItemSelectionModel(proxy)
    proxy.set_item_selection_model(selection)
    selected_source_indexes = [source.index(0, 0), source.index(2, 0)]
    for source_index in selected_source_indexes:
        selection.select(
            proxy.mapFromSource(source_index),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
    assert len(selection.selectedRows()) == 2

    proxy.sort(8, Qt.SortOrder.AscendingOrder)

    selected_proxy_rows = selection.selectedRows()
    assert len(selected_proxy_rows) == 2
    assert {proxy.mapToSource(index).row() for index in selected_proxy_rows} == {
        index.row() for index in selected_source_indexes
    }
