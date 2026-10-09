from dataclasses import FrozenInstanceError, fields
from typing import Any

import pytest

from inventory_manager_mini.core.models import (
    REASON_LABELS,
    Category,
    Client,
    DashboardRow,
    GroupBy,
    Item,
    ItemFilter,
    ItemRow,
    ItemUpdate,
    Location,
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
        NewItem,
        ItemUpdate,
        PurchaseInfo,
        ItemRow,
        MovementRow,
        ItemFilter,
        DashboardRow,
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
    }


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
    assert [group.value for group in GroupBy] == [
        "none",
        "client",
        "purchaser",
        "client_purchaser",
    ]
    assert _field_names(DashboardRow) == {
        "period_label",
        "period_start",
        "client_id",
        "client_name",
        "purchaser_id",
        "purchaser_name",
        "inbound_quantity",
        "outbound_quantity",
        "expenditure",
        "unpriced_issue_count",
        "disposed_quantity",
        "disposal_amount",
    }


def test_reason_labels_cover_every_reason() -> None:
    assert set(REASON_LABELS) == set(Reason)
    assert list(REASON_LABELS.values()) == ["入庫", "出庫", "返品", "廃棄", "棚卸"]


def test_model_instances_are_immutable() -> None:
    client = Client(id=1, name="総務", is_active=True)

    with pytest.raises(FrozenInstanceError):
        client.name = "経理"  # type: ignore[misc]
