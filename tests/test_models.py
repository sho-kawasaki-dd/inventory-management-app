from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest

from inventory_manager_mini.core.models import (
    DEFAULT_HISTORY_PAGE_SIZE,
    HISTORY_PAGE_SIZES,
    MAX_AGGREGATE_VALUE,
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    REASON_LABELS,
    Allocation,
    Category,
    Client,
    DashboardRow,
    FifoEstimate,
    GroupAxis,
    HistoryPage,
    HistoryPeriod,
    HistoryPeriodKind,
    Item,
    ItemChoice,
    ItemFilter,
    ItemRow,
    ItemUpdate,
    Location,
    Lot,
    MovementRow,
    NewItem,
    PeriodKind,
    PurchaseInfo,
    Purchaser,
    Reason,
    Staff,
    StockMovement,
)


def _field_names(model: type[Any]) -> set[str]:
    return {field.name for field in fields(model)}


@pytest.mark.parametrize(
    "model",
    [
        Client,
        Purchaser,
        Staff,
        Category,
        Location,
        Item,
        StockMovement,
        Lot,
        Allocation,
        FifoEstimate,
        NewItem,
        ItemUpdate,
        PurchaseInfo,
        ItemRow,
        MovementRow,
        ItemFilter,
        DashboardRow,
        ItemChoice,
        HistoryPeriod,
        HistoryPage,
    ],
)
def test_models_are_frozen_and_slotted(model: type[Any]) -> None:
    assert "__slots__" in model.__dict__
    assert model.__dataclass_params__.frozen


def test_master_and_inventory_models_match_the_data_contract() -> None:
    assert _field_names(Client) == {"id", "name", "is_active"}
    assert _field_names(Purchaser) == {"id", "name", "is_active"}
    assert _field_names(Staff) == {"id", "name", "is_active"}
    assert _field_names(Category) == {
        "id",
        "parent_id",
        "name",
        "code_prefix",
        "next_seq",
    }
    assert _field_names(Location) == {"id", "name"}
    assert _field_names(Item) == {
        "id",
        "client_id",
        "purchaser_id",
        "code",
        "name",
        "category_id",
        "location_id",
        "unit",
        "quantity",
        "reorder_threshold",
        "reorder_quantity",
        "purchase_url",
        "supplier",
        "manufacturer_part_number",
        "application",
        "reference_price",
        "note",
        "is_active",
        "created_at",
        "updated_at",
    }
    assert _field_names(StockMovement) == {
        "id",
        "item_id",
        "client_id",
        "purchaser_id",
        "staff_id",
        "reason",
        "delta",
        "unit_price",
        "used_for",
        "reversal_of",
        "note",
        "moved_at",
        "cost_amount",
    }
    assert _field_names(Lot) == {
        "id",
        "moved_at",
        "remaining_quantity",
        "unit_price",
        "purchaser_id",
    }
    assert _field_names(Allocation) == {"lot_id", "quantity"}
    assert _field_names(FifoEstimate) == {"cost_amount", "unpriced_quantity"}


def test_input_and_display_models_match_their_roles() -> None:
    assert {"initial_quantity", "initial_staff_id"} <= _field_names(NewItem)
    assert not {"code", "quantity", "unit"} & _field_names(ItemUpdate)
    assert {"client_name", "purchaser_name", "category_path", "location_name"} <= (
        _field_names(ItemRow)
    )
    assert {"is_low_stock", "purchase_info"} <= _field_names(ItemRow)
    assert {"code", "item_name", "client_name", "purchaser_name", "staff_name", "is_reversed"} <= (
        _field_names(MovementRow)
    )
    assert _field_names(PurchaseInfo) == {"last_purchased_at", "lot_quantity"}
    assert _field_names(ItemFilter) == {
        "text",
        "client_id",
        "purchaser_id",
        "category_id",
        "location_id",
        "low_stock_only",
        "include_inactive",
    }


def test_dashboard_model_and_enum_values() -> None:
    assert [reason.value for reason in Reason] == ["in", "out", "return", "dispose", "adjust"]
    assert [period.value for period in PeriodKind] == ["annual", "monthly"]
    assert [axis.value for axis in GroupAxis] == [
        "client",
        "purchaser",
        "category",
        "item",
    ]
    assert _field_names(ItemChoice) == {"id", "code", "name", "is_active"}
    assert _field_names(DashboardRow) == {
        "period_label",
        "period_start",
        "client_id",
        "client_name",
        "purchaser_id",
        "purchaser_name",
        "category_id",
        "category_name",
        "item_id",
        "item_code",
        "item_name",
        "inbound_quantity",
        "outbound_quantity",
        "expenditure",
        "unpriced_issue_quantity",
        "disposed_quantity",
        "disposal_amount",
    }


def test_reason_labels_cover_every_reason() -> None:
    assert set(REASON_LABELS) == set(Reason)
    assert list(REASON_LABELS.values()) == ["入庫", "出庫", "返品", "廃棄", "棚卸"]


def test_business_limits_match_the_development_plan() -> None:
    assert MAX_STOCK_QUANTITY == 1_000_000
    assert MAX_UNIT_PRICE == 10_000_000
    assert MAX_MOVEMENT_AMOUNT == 100_000_000
    assert MAX_AGGREGATE_VALUE == 1_000_000_000_000


def test_model_instances_are_immutable() -> None:
    client = Client(id=1, name="総務", is_active=True)

    with pytest.raises(FrozenInstanceError):
        client.name = "経理"  # type: ignore[misc]


def test_history_page_size_constants() -> None:
    assert HISTORY_PAGE_SIZES == (300, 500)
    assert DEFAULT_HISTORY_PAGE_SIZE == 300
    assert DEFAULT_HISTORY_PAGE_SIZE in HISTORY_PAGE_SIZES


def test_history_period_kind_values() -> None:
    assert [kind.value for kind in HistoryPeriodKind] == ["all", "annual", "monthly"]


def test_history_period_defaults_to_all_periods() -> None:
    period = HistoryPeriod()

    assert period.kind is HistoryPeriodKind.ALL
    assert period.fiscal_year is None
    assert period.month is None


def test_history_models_match_their_roles() -> None:
    assert _field_names(HistoryPeriod) == {"kind", "fiscal_year", "month"}
    assert _field_names(HistoryPage) == {
        "rows",
        "total_count",
        "page",
        "page_size",
        "total_pages",
    }
