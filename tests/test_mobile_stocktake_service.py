from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inventory_manager_mini.core import services as service_module
from inventory_manager_mini.core.errors import (
    FileOutputError,
    StocktakeInProgressError,
    ValidationError,
)
from inventory_manager_mini.core.mobile_stocktake import encode_stocktake_payload
from inventory_manager_mini.core.models import (
    ItemUpdate,
    MobileStocktakeEntry,
    MobileStocktakePayload,
    NewItem,
    Reason,
)
from inventory_manager_mini.core.services import BackupService, InventoryService, MasterService


def _new_item(
    inventory: InventoryService,
    name: str,
    *,
    client_id: int = 1,
    quantity: int = 0,
    reference_price: int | None = 100,
):
    return inventory.create_item(
        NewItem(
            client_id=client_id,
            purchaser_id=1,
            name=name,
            category_id=2,
            initial_quantity=quantity,
            initial_staff_id=1 if quantity else None,
            reference_price=reference_price,
        )
    )


def _update_item(item: object, **changes: object) -> ItemUpdate:
    values: dict[str, object] = {
        "id": item.id,  # type: ignore[attr-defined]
        "client_id": item.client_id,  # type: ignore[attr-defined]
        "purchaser_id": item.purchaser_id,  # type: ignore[attr-defined]
        "name": item.name,  # type: ignore[attr-defined]
        "category_id": item.category_id,  # type: ignore[attr-defined]
        "location_id": item.location_id,  # type: ignore[attr-defined]
        "reorder_threshold": item.reorder_threshold,  # type: ignore[attr-defined]
        "reorder_quantity": item.reorder_quantity,  # type: ignore[attr-defined]
        "purchase_url": item.purchase_url,  # type: ignore[attr-defined]
        "supplier": item.supplier,  # type: ignore[attr-defined]
        "manufacturer_part_number": item.manufacturer_part_number,  # type: ignore[attr-defined]
        "application": item.application,  # type: ignore[attr-defined]
        "reference_price": item.reference_price,  # type: ignore[attr-defined]
        "note": item.note,  # type: ignore[attr-defined]
    }
    values.update(changes)
    return ItemUpdate(**values)  # type: ignore[arg-type]


def _payload(sheet: object, entries: tuple[MobileStocktakeEntry, ...]) -> str:
    return encode_stocktake_payload(
        MobileStocktakePayload(
            session_id=sheet.session_id,  # type: ignore[attr-defined]
            client_id=sheet.client_id,  # type: ignore[attr-defined]
            entries=entries,
        )
    )


def test_mobile_stocktake_preview_and_completion_apply_all_deltas_atomically(
    inventory: InventoryService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    unchanged = _new_item(inventory, "変更なし", quantity=2)
    increased = _new_item(inventory, "増加", quantity=1, reference_price=250)
    decreased = _new_item(inventory, "減少", quantity=4)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = _payload(
        sheet,
        (
            MobileStocktakeEntry(decreased.id, 2, "棚の下"),
            MobileStocktakeEntry(increased.id, 3, "追加分"),
            MobileStocktakeEntry(unchanged.id, 2),
        ),
    )

    preview = inventory.preview_mobile_stocktake(result)
    assert (preview.unchanged_count, preview.increased_count, preview.decreased_count) == (1, 1, 1)
    assert [row.delta for row in preview.rows] == [0, 2, -2]
    assert preview.rows[-1].note == "棚の下"

    fixed_clock.advance(seconds=1)
    movements = inventory.complete_mobile_stocktake(result, staff_id=2)
    assert [movement.item_id for movement in movements] == [
        unchanged.id,
        increased.id,
        decreased.id,
    ]
    assert [movement.delta for movement in movements] == [0, 2, -2]
    assert {movement.moved_at for movement in movements} == {movements[0].moved_at}
    assert all(
        movement.reason is Reason.ADJUST and movement.staff_id == 2 for movement in movements
    )
    assert [movement.note for movement in movements] == [None, "追加分", "棚の下"]
    assert [movement.unit_price for movement in movements] == [None, 250, None]
    assert inventory.allocations.list_for_movement(movements[-1].id)[0].quantity == 2
    quantities = []
    for item in (unchanged, increased, decreased):
        current = inventory.get_item(item.id)
        assert current is not None
        quantities.append(current.quantity)
    assert quantities == [2, 3, 2]
    assert inventory.get_active_stocktake(1) is None


def test_mobile_stocktake_reexport_and_existing_path_preserve_old_session(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    item = _new_item(inventory, "対象", quantity=1)
    first_path = tmp_path / "first.html"
    first_sheet = inventory.export_mobile_stocktake_html(first_path, 1, 1)
    first_result = _payload(first_sheet, (MobileStocktakeEntry(item.id, 1),))
    original_html = first_path.read_text(encoding="utf-8")

    with pytest.raises(FileOutputError):
        inventory.export_mobile_stocktake_html(first_path, 1, 2)
    assert first_path.read_text(encoding="utf-8") == original_html
    assert inventory.get_active_stocktake(1).session_id == first_sheet.session_id  # type: ignore[union-attr]

    second_sheet = inventory.export_mobile_stocktake_html(tmp_path / "second.html", 1, 2)
    assert second_sheet.session_id != first_sheet.session_id
    with pytest.raises(ValidationError, match="この棚卸シートは無効です"):
        inventory.preview_mobile_stocktake(first_result)


@pytest.mark.parametrize("failure_stage", ["write", "close"])
def test_mobile_stocktake_file_failures_remove_new_file_and_rollback(
    inventory: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    _new_item(inventory, "書き込み失敗")
    output_path = tmp_path / "failed.html"
    original_open = Path.open

    class FailingOutput:
        def __enter__(self) -> FailingOutput:
            return self

        def __exit__(self, *_: object) -> bool:
            if failure_stage == "close":
                raise OSError("close failed")
            return False

        def write(self, _content: str) -> int:
            raise OSError("write failed")

    def failing_open(path: Path, mode: str = "r", *, encoding: str | None = None) -> object:
        if path == output_path:
            with original_open(path, mode, encoding=encoding):
                pass
            return FailingOutput()
        return original_open(path, mode, encoding=encoding)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(FileOutputError):
        inventory.export_mobile_stocktake_html(output_path, 1, 1)

    assert not output_path.exists()
    assert inventory.get_active_stocktake(1) is None


def test_mobile_stocktake_commit_failure_removes_new_file_and_keeps_old_session(
    inventory: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "COMMIT失敗")
    old_path = tmp_path / "old.html"
    old_sheet = inventory.export_mobile_stocktake_html(old_path, 1, 1)
    old_html = old_path.read_text(encoding="utf-8")
    new_path = tmp_path / "new.html"

    @contextmanager
    def failing_commit(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("ROLLBACK")
            raise RuntimeError("commit failed")

    monkeypatch.setattr(service_module, "transaction", failing_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        inventory.export_mobile_stocktake_html(new_path, 1, 2)

    assert not new_path.exists()
    assert old_path.read_text(encoding="utf-8") == old_html
    assert inventory.get_active_stocktake(1).session_id == old_sheet.session_id  # type: ignore[union-attr]


def test_mobile_stocktake_lock_blocks_inventory_operations_but_not_other_clients(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    locked_item = _new_item(inventory, "ロック対象", quantity=4)
    original = inventory.list_history(locked_item.id)[0]
    moved_item = _new_item(inventory, "移動済み", quantity=1)
    moved_original = inventory.list_history(moved_item.id)[0]
    inventory.update_item(_update_item(moved_item, client_id=2))
    other_item = _new_item(inventory, "別クライアント", client_id=2, quantity=2)
    inventory.export_mobile_stocktake_html(tmp_path / "locked.html", 1, 1)

    with pytest.raises(StocktakeInProgressError):
        _new_item(inventory, "追加禁止")
    with pytest.raises(StocktakeInProgressError):
        inventory.update_item(_update_item(locked_item, name="編集禁止"))
    with pytest.raises(StocktakeInProgressError):
        inventory.update_item(_update_item(other_item, client_id=1))
    with pytest.raises(StocktakeInProgressError):
        inventory.deactivate_item(locked_item.id)
    with pytest.raises(StocktakeInProgressError):
        inventory.reactivate_item(locked_item.id)
    for operation in (
        lambda: inventory.receive(locked_item.id, 1, 1),
        lambda: inventory.issue(locked_item.id, 1, 1, "使用先"),
        lambda: inventory.return_to_supplier(locked_item.id, 1, 1),
        lambda: inventory.dispose(locked_item.id, 1, 1),
        lambda: inventory.stocktake(locked_item.id, 1, 3),
        lambda: inventory.reverse(original.id, 2),
    ):
        with pytest.raises(StocktakeInProgressError):
            operation()
    assert inventory.reversal_block_reason(original.id) == (
        "対象クライアントがスマホ棚卸手続き中のため取り消せません"
    )

    assert inventory.receive(other_item.id, 1, 1).delta == 1
    reversal = inventory.reverse(moved_original.id, 2)
    assert reversal.delta == -1


def test_master_service_enforces_client_and_sheet_creator_locks(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "ロック対象")
    inventory.export_mobile_stocktake_html(tmp_path / "locked.html", 1, 1)

    assert master.stocktake_lock_reason("client", 1) is not None
    assert master.stocktake_lock_reason("staff", 1) is not None
    assert not master.can_delete_client(1)
    assert not master.can_delete_staff(1)
    with pytest.raises(StocktakeInProgressError, match="スマホ棚卸手続き中"):
        master.deactivate_client(1)
    with pytest.raises(StocktakeInProgressError, match="スマホ棚卸手続き中"):
        master.delete_client(1)
    with pytest.raises(StocktakeInProgressError, match="シート作成担当者"):
        master.delete_staff(1)

    assert master.rename_client(1, "名称変更可").name == "名称変更可"
    assert master.deactivate_staff(1).is_active is False
    inventory.cancel_mobile_stocktake(1)
    assert master.stocktake_lock_reason("client", 1) is None
    assert master.stocktake_lock_reason("staff", 1) is None
    assert master.deactivate_client(1).is_active is False


def test_mobile_stocktake_checks_all_history_timestamps_before_writing(
    inventory: InventoryService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    item = _new_item(inventory, "時刻検証", quantity=2)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = _payload(sheet, (MobileStocktakeEntry(item.id, 3),))
    fixed_clock.current = datetime(2026, 1, 15, 11, tzinfo=UTC)

    with pytest.raises(ValidationError, match="記録時刻が対象品目の最新履歴より前"):
        inventory.complete_mobile_stocktake(result, staff_id=2)
    assert inventory.get_item(item.id).quantity == 2  # type: ignore[union-attr]
    assert len(inventory.list_history(item.id)) == 1
    assert inventory.get_active_stocktake(1) is not None


def test_prepared_restore_reports_active_stocktake_clients(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "復元確認")
    inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    backup = BackupService(inventory.conn)
    backup_path = backup.create_backup(inventory.conn, tmp_path / "backups")

    with backup.prepare_restore(backup_path) as prepared:
        assert prepared.stocktake_client_names == ("総務",)
