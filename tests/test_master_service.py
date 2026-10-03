from __future__ import annotations

import sqlite3

import pytest

from inventory_manager_mini.core.errors import (
    CategoryCycleError,
    MasterInUseError,
    PrefixLockedError,
    ValidationError,
)
from inventory_manager_mini.core.models import NewItem
from inventory_manager_mini.core.services import InventoryService, MasterService


def test_unused_masters_can_be_physically_deleted(master: MasterService) -> None:
    client = master.add_client("一時クライアント")
    purchaser = master.add_purchaser("一時発注主体")
    staff = master.add_staff("一時担当")
    category = master.add_category("一時カテゴリ", "TMP")
    location = master.add_location("一時保管場所")

    assert all(
        (
            master.can_delete_client(client.id),
            master.can_delete_purchaser(purchaser.id),
            master.can_delete_staff(staff.id),
            master.can_delete_category(category.id),
            master.can_delete_location(location.id),
        )
    )
    master.delete_client(client.id)
    master.delete_purchaser(purchaser.id)
    master.delete_staff(staff.id)
    master.delete_category(category.id)
    master.delete_location(location.id)
    assert client.id not in {row.id for row in master.list_clients(include_inactive=True)}
    assert purchaser.id not in {row.id for row in master.list_purchasers(include_inactive=True)}
    assert staff.id not in {row.id for row in master.list_staff(include_inactive=True)}
    assert category.id not in {row.id for row in master.list_categories()}
    assert location.id not in {row.id for row in master.list_locations()}


def test_in_use_masters_can_be_deactivated_but_not_deleted(
    master: MasterService,
    inventory: InventoryService,
) -> None:
    item = inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="使用中",
            category_id=2,
            location_id=1,
            initial_quantity=2,
            initial_staff_id=1,
        )
    )
    assert item.quantity == 2
    assert not master.can_delete_client(1)
    assert not master.can_delete_purchaser(1)
    assert not master.can_delete_staff(1)
    assert not master.can_delete_category(2)
    assert not master.can_delete_location(1)

    for delete in (
        master.delete_client,
        master.delete_purchaser,
        master.delete_staff,
        master.delete_category,
        master.delete_location,
    ):
        with pytest.raises(MasterInUseError):
            delete(1 if delete != master.delete_category else 2)

    assert master.deactivate_client(1).is_active is False
    assert master.deactivate_purchaser(1).is_active is False
    assert master.deactivate_staff(1).is_active is False
    assert master.reactivate_client(1).is_active is True
    assert master.reactivate_purchaser(1).is_active is True
    assert master.reactivate_staff(1).is_active is True


def test_category_parent_changes_reject_self_child_and_grandchild(
    master: MasterService,
) -> None:
    child = master.add_category("子", "CH", parent_id=1)
    grandchild = master.add_category("孫", "GC", parent_id=child.id)

    with pytest.raises(CategoryCycleError):
        master.move_category(1, 1)
    with pytest.raises(CategoryCycleError):
        master.move_category(1, child.id)
    with pytest.raises(CategoryCycleError):
        master.move_category(1, grandchild.id)

    assert master.move_category(grandchild.id, None).parent_id is None


@pytest.mark.parametrize("prefix", ["a1", "A", "ABCDEF", "A-B", "ＡＢ"])
def test_category_prefix_requires_two_to_five_ascii_uppercase_or_digits(
    master: MasterService, prefix: str
) -> None:
    with pytest.raises(ValidationError):
        master.add_category("不正", prefix)


def test_category_prefix_is_locked_after_code_allocation(
    master: MasterService,
    inventory: InventoryService,
) -> None:
    category = master.add_category("採番前", "BE")
    assert master.change_category_prefix(category.id, "BF").code_prefix == "BF"
    inventory.create_item(
        NewItem(client_id=1, purchaser_id=1, name="採番", category_id=category.id)
    )
    assert not master.can_delete_category(category.id)
    with pytest.raises(PrefixLockedError):
        master.change_category_prefix(category.id, "BG")
    assert master.change_category_prefix(category.id, "BF").code_prefix == "BF"


def test_category_name_duplicates_are_rejected_only_within_same_parent(
    master: MasterService,
) -> None:
    first = master.add_category("共通", "C1", parent_id=1)
    other_parent = master.add_category("別親", "C2")
    second = master.add_category("共通", "C3", parent_id=other_parent.id)
    assert second.name == first.name

    with pytest.raises(ValidationError):
        master.add_category("共通", "C4", parent_id=1)
    with pytest.raises(ValidationError):
        master.rename_category(first.id, "文具")

    assert master.rename_category(first.id, "共通名変更").name == "共通名変更"


def test_duplicate_master_names_and_rename_are_validated(master: MasterService) -> None:
    client = master.add_client("重複候補")
    with pytest.raises(ValidationError):
        master.add_client("重複候補")
    with pytest.raises(ValidationError):
        master.rename_client(client.id, "総務")
    assert master.rename_client(client.id, "名称変更").name == "名称変更"


def test_delete_rejects_children_and_missing_master(master: MasterService) -> None:
    child = master.add_category("削除対象の子", "DC", parent_id=1)
    with pytest.raises(MasterInUseError):
        master.delete_category(1)
    assert not master.can_delete_category(1)
    master.delete_category(child.id)
    with pytest.raises(ValidationError):
        master.delete_location(999)


def test_master_reads_and_usage_checks_do_not_open_transactions(
    master: MasterService,
    seeded_conn: sqlite3.Connection,
) -> None:
    assert master.list_clients()
    assert master.client_in_use(1) is False
    assert master.can_delete_client(1) is True
    assert seeded_conn.in_transaction is False
