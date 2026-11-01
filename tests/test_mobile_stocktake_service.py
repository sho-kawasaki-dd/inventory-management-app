from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from inventory_manager_mini.core import services as service_module
from inventory_manager_mini.core.errors import (
    FileOutputError,
    InactiveMasterError,
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
from inventory_manager_mini.db.integrity import compute_aggregates


def _new_item(
    inventory: InventoryService,
    name: str,
    *,
    client_id: int = 1,
    quantity: int = 0,
    reference_price: int | None = 100,
    category_id: int = 2,
):
    return inventory.create_item(
        NewItem(
            client_id=client_id,
            purchaser_id=1,
            name=name,
            category_id=category_id,
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


class _FailingOutput:
    def __init__(self, stage: str) -> None:
        self.stage = stage

    def __enter__(self) -> _FailingOutput:
        return self

    def __exit__(self, *_: object) -> bool:
        if self.stage == "close":
            raise OSError("close failed")
        return False

    def write(self, _content: str) -> int:
        if self.stage == "write":
            raise OSError("write failed")
        return 0


def _fail_output_after_creation(
    monkeypatch: pytest.MonkeyPatch, output_path: Path, stage: str
) -> None:
    original_open = Path.open

    def failing_open(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if path == output_path:
            # 排他的な新規作成を実際に行ってから失敗する出力に差し替える
            with original_open(path, mode, *args, **kwargs):  # type: ignore[arg-type]
                pass
            return _FailingOutput(stage)
        return original_open(path, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", failing_open)


@pytest.mark.parametrize("has_old_session", [False, True])
@pytest.mark.parametrize("failure_stage", ["write", "close"])
def test_mobile_stocktake_file_failures_remove_new_file_and_rollback(
    inventory: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
    has_old_session: bool,
) -> None:
    item = _new_item(inventory, "書き込み失敗", quantity=1)
    old_path = tmp_path / "old.html"
    old_sheet = inventory.export_mobile_stocktake_html(old_path, 1, 1) if has_old_session else None
    old_html = old_path.read_text(encoding="utf-8") if has_old_session else None
    output_path = tmp_path / "failed.html"
    _fail_output_after_creation(monkeypatch, output_path, failure_stage)

    with pytest.raises(FileOutputError):
        inventory.export_mobile_stocktake_html(output_path, 1, 2)

    assert not output_path.exists()
    active = inventory.get_active_stocktake(1)
    if old_sheet is None:
        assert active is None
    else:
        assert active is not None
        assert active.session_id == old_sheet.session_id
        assert active.staff_id == 1
        assert old_path.read_text(encoding="utf-8") == old_html
        old_result = _payload(old_sheet, (MobileStocktakeEntry(item.id, 1),))
        assert inventory.preview_mobile_stocktake(old_result).rows[0].item_id == item.id


def test_mobile_stocktake_cleanup_failure_reports_remaining_file(
    inventory: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _new_item(inventory, "後始末失敗")
    output_path = tmp_path / "residual.html"
    _fail_output_after_creation(monkeypatch, output_path, "write")
    original_unlink = Path.unlink

    def failing_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == output_path:
            raise PermissionError("locked")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(FileOutputError) as raised:
        inventory.export_mobile_stocktake_html(output_path, 1, 1)

    message = str(raised.value)
    assert "write failed" in message
    assert str(output_path) in message
    assert "後始末に失敗しました" in message
    assert "使用できません" in message
    assert str(output_path) in caplog.text
    assert output_path.exists()
    assert inventory.get_active_stocktake(1) is None


def test_mobile_stocktake_existing_path_on_new_start_keeps_file_and_leaves_no_lock(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "既存パス")
    existing = tmp_path / "existing.html"
    existing.write_text("既存の内容", encoding="utf-8")

    with pytest.raises(FileOutputError, match="同名の棚卸シート"):
        inventory.export_mobile_stocktake_html(existing, 1, 1)

    assert existing.read_text(encoding="utf-8") == "既存の内容"
    assert inventory.get_active_stocktake(1) is None


def test_mobile_stocktake_missing_parent_directory_leaves_no_lock(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "保存先なし")
    output_path = tmp_path / "missing" / "sheet.html"

    with pytest.raises(FileOutputError, match="書き込めません"):
        inventory.export_mobile_stocktake_html(output_path, 1, 1)

    assert not output_path.parent.exists()
    assert inventory.get_active_stocktake(1) is None


@pytest.mark.parametrize("has_old_session", [False, True])
def test_mobile_stocktake_commit_failure_removes_new_file_and_keeps_old_session(
    inventory: InventoryService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_old_session: bool,
) -> None:
    _new_item(inventory, "COMMIT失敗")
    old_path = tmp_path / "old.html"
    old_sheet = inventory.export_mobile_stocktake_html(old_path, 1, 1) if has_old_session else None
    old_html = old_path.read_text(encoding="utf-8") if has_old_session else None
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
    active = inventory.get_active_stocktake(1)
    if old_sheet is None:
        assert active is None
    else:
        assert old_path.read_text(encoding="utf-8") == old_html
        assert active is not None
        assert active.session_id == old_sheet.session_id


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


def test_prepared_restore_has_no_stocktake_clients_without_active_stocktake(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "棚卸なし")
    backup = BackupService(inventory.conn)
    backup_path = backup.create_backup(inventory.conn, tmp_path / "backups")

    with backup.prepare_restore(backup_path) as prepared:
        assert prepared.stocktake_client_names == ()


def _two_items(inventory: InventoryService) -> tuple[Any, Any]:
    return _new_item(inventory, "品目A", quantity=2), _new_item(inventory, "品目B", quantity=5)


def _row_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    movements = conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
    allocations = conn.execute("SELECT COUNT(*) FROM stock_allocations").fetchone()[0]
    return movements, allocations


@pytest.mark.parametrize(
    ("client_id", "staff_id", "error", "message"),
    [
        (99, 1, ValidationError, "クライアントが見つかりません"),
        (3, 1, InactiveMasterError, "クライアント"),
        (2, 1, ValidationError, "有効な品目がありません"),
        (1, 3, InactiveMasterError, "担当者"),
        (1, 99, ValidationError, "担当者が見つかりません"),
    ],
)
def test_mobile_stocktake_rejects_invalid_start_conditions(
    inventory: InventoryService,
    tmp_path: Path,
    client_id: int,
    staff_id: int,
    error: type[Exception],
    message: str,
) -> None:
    _new_item(inventory, "有効品目")
    output_path = tmp_path / "sheet.html"

    with pytest.raises(error, match=message):
        inventory.export_mobile_stocktake_html(output_path, client_id, staff_id)

    assert not output_path.exists()
    assert inventory.list_active_stocktakes() == []


def test_mobile_stocktake_sheet_contains_session_and_only_active_items(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    active = _new_item(inventory, "有効品目ABC", quantity=1)
    retired = _new_item(inventory, "廃止品目XYZ", quantity=1)
    inventory.deactivate_item(retired.id)
    output_path = tmp_path / "sheet.html"

    sheet = inventory.export_mobile_stocktake_html(output_path, 1, 1)

    html = output_path.read_text(encoding="utf-8")
    assert [item.item_id for item in sheet.items] == [active.id]
    assert sheet.session_id in html
    assert "有効品目ABC" in html
    assert "廃止品目XYZ" not in html
    stocktake = inventory.get_active_stocktake(1)
    assert stocktake is not None
    assert stocktake.item_count == 1
    preview = inventory.preview_mobile_stocktake(
        _payload(sheet, (MobileStocktakeEntry(active.id, 1),))
    )
    assert [row.item_id for row in preview.rows] == [active.id]


def test_cancel_mobile_stocktake_unlocks_and_rejects_clients_without_stocktake(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    item = _new_item(inventory, "中止対象", quantity=1)
    with pytest.raises(ValidationError, match="棚卸手続き中ではありません"):
        inventory.cancel_mobile_stocktake(1)
    with pytest.raises(ValidationError):
        inventory.cancel_mobile_stocktake(0)

    inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    inventory.cancel_mobile_stocktake(1)

    assert inventory.get_active_stocktake(1) is None
    assert inventory.receive(item.id, 1, 1).delta == 1
    assert inventory.update_item(_update_item(item, name="中止後の編集")).name == "中止後の編集"
    _new_item(inventory, "中止後の追加")
    with pytest.raises(ValidationError, match="棚卸手続き中ではありません"):
        inventory.cancel_mobile_stocktake(1)


def test_mobile_stocktakes_for_different_clients_are_independent(
    inventory: InventoryService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    first = _new_item(inventory, "総務品目", quantity=1)
    second = _new_item(inventory, "経理品目", client_id=2, quantity=2)
    first_sheet = inventory.export_mobile_stocktake_html(tmp_path / "first.html", 1, 1)
    fixed_clock.advance(minutes=1)
    second_sheet = inventory.export_mobile_stocktake_html(tmp_path / "second.html", 2, 2)

    active = inventory.list_active_stocktakes()
    assert [(value.client_id, value.item_count) for value in active] == [(1, 1), (2, 1)]

    fixed_clock.advance(minutes=1)
    first_result = _payload(first_sheet, (MobileStocktakeEntry(first.id, 1),))
    inventory.complete_mobile_stocktake(first_result, staff_id=2)

    assert [value.client_id for value in inventory.list_active_stocktakes()] == [2]
    assert inventory.receive(first.id, 1, 1).delta == 1
    with pytest.raises(StocktakeInProgressError):
        inventory.receive(second.id, 1, 1)
    with pytest.raises(ValidationError, match="この棚卸シートは無効です"):
        inventory.preview_mobile_stocktake(first_result)
    second_result = _payload(second_sheet, (MobileStocktakeEntry(second.id, 2),))
    assert inventory.preview_mobile_stocktake(second_result).client_id == 2

    inventory.cancel_mobile_stocktake(2)
    assert inventory.list_active_stocktakes() == []
    assert inventory.receive(second.id, 1, 1).delta == 1


def test_mobile_stocktake_rejects_results_that_do_not_match_the_sheet(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    first, second = _two_items(inventory)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    both = (MobileStocktakeEntry(first.id, 2), MobileStocktakeEntry(second.id, 5))
    wrong_client = encode_stocktake_payload(
        MobileStocktakePayload(session_id=sheet.session_id, client_id=2, entries=both)
    )
    unknown_session = encode_stocktake_payload(
        MobileStocktakePayload(session_id="0" * 32, client_id=1, entries=both)
    )
    rejected = (
        (wrong_client, "棚卸結果のクライアントがシートと一致しません"),
        (_payload(sheet, both[:1]), "品目一覧がシートの対象品目と一致しません"),
        (
            _payload(sheet, (*both, MobileStocktakeEntry(9999, 1))),
            "品目一覧がシートの対象品目と一致しません",
        ),
        (unknown_session, "この棚卸シートは無効です"),
    )
    before = _row_counts(inventory.conn)

    for raw_text, message in rejected:
        with pytest.raises(ValidationError, match=message):
            inventory.preview_mobile_stocktake(raw_text)
        with pytest.raises(ValidationError, match=message):
            inventory.complete_mobile_stocktake(raw_text, staff_id=1)

    assert _row_counts(inventory.conn) == before
    assert inventory.get_active_stocktake(1) is not None

    inventory.cancel_mobile_stocktake(1)
    with pytest.raises(ValidationError, match="この棚卸シートは無効です"):
        inventory.preview_mobile_stocktake(_payload(sheet, both))


@pytest.mark.parametrize(
    ("staff_id", "error"),
    [(3, InactiveMasterError), (99, ValidationError)],
)
def test_complete_mobile_stocktake_rejects_invalid_confirming_staff(
    inventory: InventoryService,
    tmp_path: Path,
    staff_id: int,
    error: type[Exception],
) -> None:
    first, second = _two_items(inventory)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = _payload(
        sheet, (MobileStocktakeEntry(first.id, 1), MobileStocktakeEntry(second.id, 9))
    )
    before = _row_counts(inventory.conn)

    with pytest.raises(error):
        inventory.complete_mobile_stocktake(result, staff_id=staff_id)

    assert _row_counts(inventory.conn) == before
    assert inventory.get_item(first.id).quantity == 2  # type: ignore[union-attr]
    assert inventory.get_active_stocktake(1) is not None


def test_complete_mobile_stocktake_result_cannot_be_reused(
    inventory: InventoryService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    first, second = _two_items(inventory)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = _payload(
        sheet, (MobileStocktakeEntry(first.id, 3), MobileStocktakeEntry(second.id, 4))
    )
    fixed_clock.advance(seconds=1)
    inventory.complete_mobile_stocktake(result, staff_id=1)
    after_first = _row_counts(inventory.conn)

    with pytest.raises(ValidationError, match="この棚卸シートは無効です"):
        inventory.complete_mobile_stocktake(result, staff_id=1)

    assert _row_counts(inventory.conn) == after_first
    assert inventory.get_item(first.id).quantity == 3  # type: ignore[union-attr]


def test_complete_mobile_stocktake_rolls_back_every_item_on_midway_failure(
    inventory: InventoryService,
    fixed_clock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first, second = _two_items(inventory)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = _payload(
        sheet, (MobileStocktakeEntry(first.id, 1), MobileStocktakeEntry(second.id, 9))
    )
    before = _row_counts(inventory.conn)
    original = InventoryService._record_movement
    calls: list[int] = []

    def failing_second_record(self: InventoryService, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("record failed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InventoryService, "_record_movement", failing_second_record)
    fixed_clock.advance(seconds=1)
    with pytest.raises(RuntimeError, match="record failed"):
        inventory.complete_mobile_stocktake(result, staff_id=1)

    assert len(calls) == 2
    assert _row_counts(inventory.conn) == before
    assert inventory.get_item(first.id).quantity == 2  # type: ignore[union-attr]
    assert inventory.get_item(second.id).quantity == 5  # type: ignore[union-attr]
    assert inventory.get_active_stocktake(1) is not None


def test_mobile_stocktake_clock_rollback_on_any_item_leaves_all_items_unchanged(
    inventory: InventoryService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    first = _new_item(inventory, "先頭品目", quantity=2)
    fixed_clock.advance(hours=1)
    second = _new_item(inventory, "後続品目", quantity=3)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result = _payload(
        sheet, (MobileStocktakeEntry(first.id, 0), MobileStocktakeEntry(second.id, 1))
    )
    before = _row_counts(inventory.conn)
    # first の最新履歴より後で、second の最新履歴より前
    fixed_clock.current = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)

    with pytest.raises(ValidationError, match="記録時刻が対象品目の最新履歴より前"):
        inventory.complete_mobile_stocktake(result, staff_id=1)

    assert _row_counts(inventory.conn) == before
    assert inventory.get_item(first.id).quantity == 2  # type: ignore[union-attr]
    assert inventory.get_item(second.id).quantity == 3  # type: ignore[union-attr]
    assert inventory.get_active_stocktake(1) is not None


def test_mobile_stocktake_decrease_spans_multiple_lots_and_keeps_lot_total(
    inventory: InventoryService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    item = _new_item(inventory, "複数ロット", quantity=3)
    fixed_clock.advance(seconds=1)
    inventory.receive(item.id, 1, 2, unit_price=300)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    fixed_clock.advance(seconds=1)

    movement = inventory.complete_mobile_stocktake(
        _payload(sheet, (MobileStocktakeEntry(item.id, 1),)), staff_id=2
    )[0]

    assert (movement.delta, movement.unit_price, movement.cost_amount) == (-4, None, None)
    allocations = inventory.allocations.list_for_movement(movement.id)
    assert [allocation.quantity for allocation in allocations] == [3, 1]
    lots = inventory.allocations.list_available_lots(item.id)
    assert [(lot.remaining_quantity, lot.unit_price) for lot in lots] == [(1, 300)]
    assert inventory.get_item(item.id).quantity == 1  # type: ignore[union-attr]
    assert inventory.aggregates.get() == compute_aggregates(inventory.conn)


def test_mobile_stocktake_increase_without_reference_price_creates_unpriced_lot(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    item = _new_item(inventory, "単価なし", quantity=0, reference_price=None)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)

    movement = inventory.complete_mobile_stocktake(
        _payload(sheet, (MobileStocktakeEntry(item.id, 3),)), staff_id=1
    )[0]

    assert (movement.delta, movement.unit_price) == (3, None)
    lots = inventory.allocations.list_available_lots(item.id)
    assert [(lot.remaining_quantity, lot.unit_price) for lot in lots] == [(3, None)]
    assert inventory.aggregates.get() == compute_aggregates(inventory.conn)


def test_mobile_stocktake_lock_leaves_state_unchanged_and_blocks_moving_item_out(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    locked_item = _new_item(inventory, "ロック対象", quantity=4)
    moved_item = _new_item(inventory, "移動済み", quantity=1)
    moved_original = inventory.list_history(moved_item.id)[0]
    inventory.update_item(_update_item(moved_item, client_id=2))
    inventory.export_mobile_stocktake_html(tmp_path / "locked.html", 1, 1)
    before = _row_counts(inventory.conn)

    with pytest.raises(StocktakeInProgressError):
        inventory.update_item(_update_item(locked_item, client_id=2))
    with pytest.raises(StocktakeInProgressError):
        inventory.receive(locked_item.id, 1, 1)
    with pytest.raises(StocktakeInProgressError):
        inventory.stocktake(locked_item.id, 1, 0)

    assert inventory.get_item(locked_item.id).client_id == 1  # type: ignore[union-attr]
    assert inventory.get_item(locked_item.id).quantity == 4  # type: ignore[union-attr]
    assert _row_counts(inventory.conn) == before
    assert inventory.reversal_block_reason(moved_original.id) is None


def test_master_lock_does_not_block_unrelated_master_operations(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "ロック対象")
    inventory.export_mobile_stocktake_html(tmp_path / "locked.html", 1, 1)

    assert master.reactivate_client(1).is_active is True
    assert master.stocktake_lock_reason("client", 2) is None
    assert master.stocktake_lock_reason("staff", 2) is None
    assert master.can_delete_client(2)
    assert master.can_delete_staff(2)
    with pytest.raises(ValidationError, match="対象種別"):
        master.stocktake_lock_reason("purchaser", 1)

    assert master.deactivate_client(2).is_active is False
    master.delete_client(2)
    master.delete_staff(2)
    assert inventory.get_active_stocktake(1) is not None


def _category_tree(master: MasterService) -> tuple[int, int, int]:
    # 備品(1) > 文具(2) > ペン、備品(1) > 工具 の ID を返す
    pen = master.add_category("ペン", "PN", 2)
    tool = master.add_category("工具", "TL", 1)
    return pen.id, tool.id, 2


def _snapshot_ids(conn: sqlite3.Connection, client_id: int = 1) -> set[int]:
    rows = conn.execute(
        "SELECT item_id FROM active_stocktake_items WHERE client_id = ?", (client_id,)
    ).fetchall()
    return {int(row[0]) for row in rows}


def test_mobile_stocktake_category_limits_snapshot_to_descendant_items(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    pen_id, tool_id, stationery_id = _category_tree(master)
    stationery = _new_item(inventory, "文具品目", quantity=1, category_id=stationery_id)
    pen = _new_item(inventory, "ペン品目", quantity=2, category_id=pen_id)
    tool = _new_item(inventory, "工具品目", quantity=3, category_id=tool_id)

    sheet = inventory.export_mobile_stocktake_html(
        tmp_path / "sheet.html", 1, 1, category_id=stationery_id
    )

    assert {item.item_id for item in sheet.items} == {stationery.id, pen.id}
    assert (sheet.category_id, sheet.category_path) == (stationery_id, "備品 > 文具")
    assert _snapshot_ids(inventory.conn) == {stationery.id, pen.id}
    active = inventory.get_active_stocktake(1)
    assert active is not None
    assert (active.category_id, active.category_path, active.item_count) == (
        stationery_id,
        "備品 > 文具",
        2,
    )

    all_sheet = inventory.export_mobile_stocktake_html(tmp_path / "all.html", 1, 1)
    assert {item.item_id for item in all_sheet.items} == {stationery.id, pen.id, tool.id}
    assert (all_sheet.category_id, all_sheet.category_path) == (None, None)
    assert _snapshot_ids(inventory.conn) == {stationery.id, pen.id, tool.id}


def test_mobile_stocktake_category_completion_leaves_other_items_untouched(
    inventory: InventoryService,
    master: MasterService,
    fixed_clock,
    tmp_path: Path,
) -> None:
    _, tool_id, stationery_id = _category_tree(master)
    target = _new_item(inventory, "対象", quantity=3, category_id=stationery_id)
    other = _new_item(inventory, "対象外", quantity=4, category_id=tool_id)
    sheet = inventory.export_mobile_stocktake_html(
        tmp_path / "sheet.html", 1, 1, category_id=stationery_id
    )
    before_other = inventory.movements.latest_moved_at(other.id)
    result = _payload(sheet, (MobileStocktakeEntry(target.id, 1),))

    preview = inventory.preview_mobile_stocktake(result)
    assert [row.item_id for row in preview.rows] == [target.id]
    assert (preview.category_id, preview.category_path) == (stationery_id, "備品 > 文具")
    with pytest.raises(ValidationError, match="対象品目と一致しません"):
        inventory.preview_mobile_stocktake(
            _payload(sheet, (MobileStocktakeEntry(target.id, 1), MobileStocktakeEntry(other.id, 4)))
        )

    fixed_clock.advance(seconds=1)
    movements = inventory.complete_mobile_stocktake(result, staff_id=1)

    assert [movement.item_id for movement in movements] == [target.id]
    refreshed_other = inventory.get_item(other.id)
    assert refreshed_other is not None and refreshed_other.quantity == 4
    assert inventory.movements.latest_moved_at(other.id) == before_other
    assert inventory.get_active_stocktake(1) is None
    assert _snapshot_ids(inventory.conn) == set()


def test_mobile_stocktake_snapshot_survives_category_hierarchy_changes(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    pen_id, tool_id, stationery_id = _category_tree(master)
    pen = _new_item(inventory, "ペン品目", quantity=2, category_id=pen_id)
    sheet = inventory.export_mobile_stocktake_html(
        tmp_path / "sheet.html", 1, 1, category_id=stationery_id
    )

    master.move_category(pen_id, tool_id)
    master.rename_category(stationery_id, "事務用品")

    result = _payload(sheet, (MobileStocktakeEntry(pen.id, 2),))
    preview = inventory.preview_mobile_stocktake(result)
    assert [row.item_id for row in preview.rows] == [pen.id]
    assert preview.category_path == "備品 > 事務用品"
    assert len(inventory.complete_mobile_stocktake(result, staff_id=1)) == 1


def test_mobile_stocktake_lock_covers_items_outside_category_but_not_other_clients(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    _, tool_id, stationery_id = _category_tree(master)
    target = _new_item(inventory, "対象", quantity=3, category_id=stationery_id)
    outside = _new_item(inventory, "対象外", quantity=4, category_id=tool_id)
    other_client = _new_item(inventory, "別クライアント", client_id=2, quantity=2)
    inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1, category_id=stationery_id)

    assert _snapshot_ids(inventory.conn) == {target.id}
    with pytest.raises(StocktakeInProgressError):
        inventory.receive(outside.id, 1, 1)
    with pytest.raises(StocktakeInProgressError):
        inventory.issue(outside.id, 1, 1, "使用先")
    with pytest.raises(StocktakeInProgressError):
        inventory.stocktake(outside.id, 1, 3)
    with pytest.raises(StocktakeInProgressError):
        inventory.update_item(_update_item(outside, name="編集禁止"))
    with pytest.raises(StocktakeInProgressError):
        inventory.deactivate_item(outside.id)
    with pytest.raises(StocktakeInProgressError):
        _new_item(inventory, "追加禁止", category_id=tool_id)

    assert inventory.receive(other_client.id, 1, 1).delta == 1
    refreshed = inventory.get_item(outside.id)
    assert refreshed is not None and refreshed.quantity == 4

    inventory.cancel_mobile_stocktake(1)
    assert _snapshot_ids(inventory.conn) == set()
    assert inventory.receive(outside.id, 1, 1).delta == 1


def test_mobile_stocktake_rejects_snapshot_item_that_is_no_longer_valid(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    first, second = _two_items(inventory)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    inventory.conn.execute("UPDATE items SET is_active = 0 WHERE id = ?", (second.id,))
    result = _payload(
        sheet, (MobileStocktakeEntry(first.id, 2), MobileStocktakeEntry(second.id, 5))
    )

    with pytest.raises(ValidationError, match="対象品目と一致しません"):
        inventory.preview_mobile_stocktake(result)
    with pytest.raises(ValidationError, match="対象品目と一致しません"):
        inventory.complete_mobile_stocktake(result, staff_id=1)


def test_mobile_stocktake_rejects_session_without_snapshot(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    item = _new_item(inventory, "対象", quantity=1)
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    inventory.conn.execute("DELETE FROM active_stocktake_items")

    with pytest.raises(ValidationError, match="対象品目と一致しません"):
        inventory.preview_mobile_stocktake(_payload(sheet, (MobileStocktakeEntry(item.id, 1),)))


def test_mobile_stocktake_rejects_empty_or_invalid_category_without_changes(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    _, tool_id, _ = _category_tree(master)
    _new_item(inventory, "文具品目", quantity=1)
    output_path = tmp_path / "sheet.html"

    with pytest.raises(ValidationError, match="対象クライアント・カテゴリに有効な品目がありません"):
        inventory.export_mobile_stocktake_html(output_path, 1, 1, category_id=tool_id)
    with pytest.raises(ValidationError, match="カテゴリが見つかりません"):
        inventory.export_mobile_stocktake_html(output_path, 1, 1, category_id=9999)
    with pytest.raises(ValidationError, match="カテゴリ ID"):
        inventory.export_mobile_stocktake_html(output_path, 1, 1, category_id=0)

    assert not output_path.exists()
    assert inventory.list_active_stocktakes() == []
    assert _snapshot_ids(inventory.conn) == set()


def test_mobile_stocktake_reexport_replaces_snapshot_and_failure_keeps_old_one(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    _, tool_id, stationery_id = _category_tree(master)
    stationery = _new_item(inventory, "文具品目", quantity=1, category_id=stationery_id)
    tool = _new_item(inventory, "工具品目", quantity=1, category_id=tool_id)
    first = inventory.export_mobile_stocktake_html(
        tmp_path / "first.html", 1, 1, category_id=stationery_id
    )

    with pytest.raises(FileOutputError):
        inventory.export_mobile_stocktake_html(tmp_path / "first.html", 1, 1, category_id=tool_id)
    active = inventory.get_active_stocktake(1)
    assert active is not None and active.session_id == first.session_id
    assert active.category_id == stationery_id
    assert _snapshot_ids(inventory.conn) == {stationery.id}

    inventory.export_mobile_stocktake_html(tmp_path / "second.html", 1, 1, category_id=tool_id)
    active = inventory.get_active_stocktake(1)
    assert active is not None and active.category_id == tool_id
    assert _snapshot_ids(inventory.conn) == {tool.id}


def test_cancel_mobile_stocktake_removes_snapshot(
    inventory: InventoryService,
    tmp_path: Path,
) -> None:
    _new_item(inventory, "対象", quantity=1)
    inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    assert _snapshot_ids(inventory.conn)

    inventory.cancel_mobile_stocktake(1)

    assert _snapshot_ids(inventory.conn) == set()


def test_master_service_blocks_deleting_category_referenced_by_stocktake(
    inventory: InventoryService,
    master: MasterService,
    tmp_path: Path,
) -> None:
    pen_id, _, stationery_id = _category_tree(master)
    _new_item(inventory, "ペン品目", quantity=1, category_id=pen_id)
    inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1, category_id=pen_id)

    assert master.stocktake_lock_reason("category", pen_id) == (
        "スマホ棚卸の対象カテゴリとして登録中のため削除できません"
    )
    assert master.stocktake_lock_reason("category", stationery_id) is None
    assert not master.can_delete_category(pen_id)
    with pytest.raises(StocktakeInProgressError, match="対象カテゴリ"):
        master.delete_category(pen_id)

    inventory.cancel_mobile_stocktake(1)
    assert master.stocktake_lock_reason("category", pen_id) is None
