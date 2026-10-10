from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QModelIndex, QPoint, Qt
from PySide6.QtWidgets import QMessageBox, QStyleOptionViewItem

from inventory_manager_mini.core.models import ItemRow, NewItem, PurchaseInfo
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.signals import DataBus
from inventory_manager_mini.ui.widgets import alert_panel
from inventory_manager_mini.ui.widgets.alert_panel import (
    AlertPanel,
    LowStockTableModel,
    OpenUrlButtonDelegate,
)

_URL_COLUMN = LowStockTableModel.URL_COLUMN


def _context(
    inventory: InventoryService, master: MasterService, settings: SettingsService
) -> AppContext:
    return AppContext(
        inventory=inventory,
        master=master,
        settings=settings,
        data_bus=DataBus(),
        db_path=Path("test.db"),
        schema_version=1,
        app_version="0.1.0",
    )


def _new_item(name: str, **changes: object) -> NewItem:
    return replace(
        NewItem(client_id=1, purchaser_id=1, name=name, category_id=2),
        **changes,
    )


@pytest.fixture
def context(
    inventory: InventoryService, master: MasterService, settings: SettingsService
) -> AppContext:
    return _context(inventory, master, settings)


@pytest.fixture
def panel(qtbot, context: AppContext) -> AlertPanel:
    widget = AlertPanel(context)
    qtbot.addWidget(widget)
    widget.resize(900, 300)
    widget.show()
    return widget


def _headers(model: LowStockTableModel) -> list[str]:
    return [
        model.headerData(column, Qt.Orientation.Horizontal) for column in range(model.columnCount())
    ]


def _row_values(model: LowStockTableModel, row: int) -> list[str]:
    return [model.index(row, column).data() for column in range(model.columnCount())]


def _cell_center(panel: AlertPanel, row: int, column: int) -> QPoint:
    return panel.table.visualRect(panel.model.index(row, column)).center()


def _row_with_url(url: str | None) -> ItemRow:
    return ItemRow(
        id=1,
        client_id=1,
        purchaser_id=1,
        code="ST-0001",
        name="異常データ",
        category_id=2,
        location_id=None,
        unit="個",
        quantity=0,
        reorder_threshold=0,
        reorder_quantity=None,
        purchase_url=url,
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


def test_smoke_empty_panel_shows_zero_count_and_headers(panel: AlertPanel) -> None:
    panel.refresh()

    assert panel.windowTitle() == "低在庫 (0)"
    assert panel.model.rowCount() == 0
    assert _headers(panel.model) == [
        "管理番号",
        "品名",
        "メーカー型番",
        "数量",
        "閾値",
        "推奨発注数",
        "発注主体",
        "販売ページ",
    ]
    assert panel.table.selectionBehavior() == panel.table.SelectionBehavior.SelectRows
    assert panel.table.selectionMode() == panel.table.SelectionMode.SingleSelection
    assert not panel.table.isSortingEnabled()


def test_refresh_lists_low_stock_in_code_order_with_count(
    panel: AlertPanel, context: AppContext
) -> None:
    inventory = context.inventory
    inventory.create_item(
        _new_item(
            "鉛筆",
            initial_quantity=1,
            initial_staff_id=1,
            reorder_threshold=2,
            reorder_quantity=10,
            manufacturer_part_number="P-100",
            purchase_url="https://example.com/p",
        )
    )
    inventory.create_item(_new_item("十分な在庫", initial_quantity=5, initial_staff_id=1))
    inventory.create_item(_new_item("閾値ゼロ", purchaser_id=2, category_id=1))
    panel.refresh()

    assert panel.windowTitle() == "低在庫 (2)"
    assert panel.model.rowCount() == 2
    codes = [panel.model.index(row, 0).data() for row in range(2)]
    assert codes == sorted(codes)
    assert _row_values(panel.model, 0) == [
        "ST-0001",
        "鉛筆",
        "P-100",
        "1",
        "2",
        "10",
        "本部",
        "開く",
    ]
    assert panel.model.index(1, 1).data() == "閾値ゼロ"
    assert panel.model.index(1, 5).data() == ""
    assert panel.model.index(1, 6).data() == "部門"
    right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    for column in (3, 4, 5):
        assert panel.model.index(0, column).data(Qt.ItemDataRole.TextAlignmentRole) == right
    assert panel.model.index(0, 1).data(Qt.ItemDataRole.TextAlignmentRole) is None


def test_inactive_items_are_excluded_and_zero_threshold_is_included(
    panel: AlertPanel, context: AppContext
) -> None:
    inactive = context.inventory.create_item(_new_item("廃止品目"))
    active = context.inventory.create_item(_new_item("有効品目"))
    context.inventory.deactivate_item(inactive.id)
    panel.refresh()

    assert [panel.model.row_at(row).id for row in range(panel.model.rowCount())] == [active.id]

    context.inventory.reactivate_item(inactive.id)
    panel.refresh()

    assert panel.model.rowCount() == 2
    assert panel.windowTitle() == "低在庫 (2)"


def test_double_click_emits_item_id(qtbot, panel: AlertPanel, context: AppContext) -> None:
    context.inventory.create_item(_new_item("品目A"))
    target = context.inventory.create_item(_new_item("品目B"))
    panel.refresh()
    position = _cell_center(panel, 1, 1)
    qtbot.mouseClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=position)

    with qtbot.waitSignal(panel.item_activated, timeout=1000) as blocker:
        qtbot.mouseDClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=position)

    assert blocker.args == [target.id]


def test_double_click_on_button_column_does_not_emit(
    qtbot, panel: AlertPanel, context: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alert_panel.QDesktopServices, "openUrl", lambda _url: True)
    context.inventory.create_item(_new_item("品目A", purchase_url="https://example.com"))
    panel.refresh()
    received: list[int] = []
    panel.item_activated.connect(received.append)
    position = _cell_center(panel, 0, _URL_COLUMN)
    qtbot.mouseClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=position)

    qtbot.mouseDClick(panel.table.viewport(), Qt.MouseButton.LeftButton, pos=position)

    assert received == []


def test_button_click_opens_validated_url(
    qtbot, panel: AlertPanel, context: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        alert_panel.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
    )
    context.inventory.create_item(_new_item("http品目", purchase_url="  http://example.com/a  "))
    context.inventory.create_item(_new_item("https品目", purchase_url="https://example.com/b"))
    panel.refresh()

    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=_cell_center(panel, 0, _URL_COLUMN)
    )
    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=_cell_center(panel, 1, _URL_COLUMN)
    )

    assert opened == ["http://example.com/a", "https://example.com/b"]


def test_button_click_outside_button_or_on_other_column_does_not_open(
    qtbot, panel: AlertPanel, context: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        alert_panel.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
    )
    context.inventory.create_item(_new_item("品目", purchase_url="https://example.com"))
    panel.refresh()
    cell = panel.table.visualRect(panel.model.index(0, _URL_COLUMN))

    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=cell.topLeft() + QPoint(1, 1)
    )
    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=_cell_center(panel, 0, 1)
    )
    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.RightButton, pos=_cell_center(panel, 0, _URL_COLUMN)
    )

    assert opened == []


def test_button_without_url_is_inactive(
    qtbot, panel: AlertPanel, context: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        alert_panel.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
    )
    context.inventory.create_item(_new_item("URLなし"))
    panel.refresh()

    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=_cell_center(panel, 0, _URL_COLUMN)
    )
    image = panel.table.viewport().grab().toImage()

    assert opened == []
    assert not image.isNull()
    assert panel.model.index(0, _URL_COLUMN).data(Qt.ItemDataRole.UserRole) is None


@pytest.mark.parametrize(
    "bad_url",
    ["javascript:alert(1)", "https:///path", "ftp://example.com/file", "http://[::1"],
)
def test_invalid_url_is_revalidated_and_reports_error(
    qtbot, panel: AlertPanel, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    opened: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr(
        alert_panel.QDesktopServices, "openUrl", lambda url: opened.append(url.toString())
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, _title, message: warnings.append(message)
    )
    panel.model.set_rows([_row_with_url(bad_url)])

    qtbot.mouseClick(
        panel.table.viewport(), Qt.MouseButton.LeftButton, pos=_cell_center(panel, 0, _URL_COLUMN)
    )

    assert opened == []
    assert len(warnings) == 1


def test_delegate_ignores_non_mouse_events(panel: AlertPanel) -> None:
    panel.model.set_rows([_row_with_url("https://example.com")])
    delegate: OpenUrlButtonDelegate = panel.url_delegate

    assert not delegate.editorEvent(
        QEvent(QEvent.Type.KeyPress),
        panel.model,
        QStyleOptionViewItem(),
        panel.model.index(0, _URL_COLUMN),
    )


def test_model_rejects_invalid_indexes(panel: AlertPanel) -> None:
    panel.model.set_rows([_row_with_url(None)])

    assert panel.model.data(QModelIndex()) is None
    assert panel.model.index(0, 0).data() == "ST-0001"
    assert panel.model.rowCount(panel.model.index(0, 0)) == 0
    assert panel.model.columnCount(panel.model.index(0, 0)) == 0
    assert panel.model.headerData(0, Qt.Orientation.Vertical) is None
    assert panel.model.headerData(99, Qt.Orientation.Horizontal) is None


def test_refresh_failure_keeps_previous_rows(
    panel: AlertPanel, context: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    context.inventory.create_item(_new_item("品目"))
    panel.refresh()
    errors: list[str] = []
    monkeypatch.setattr(
        alert_panel, "run_guarded", lambda parent, func: (errors.append("called"), (False, None))[1]
    )

    panel.refresh()

    assert errors == ["called"]
    assert panel.model.rowCount() == 1
    assert panel.windowTitle() == "低在庫 (1)"
