from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest

from inventory_manager_mini.core import services
from inventory_manager_mini.core.errors import InvalidBackupError, RestoreError, ValidationError
from inventory_manager_mini.core.services import BackupService
from inventory_manager_mini.db import backup, migrations
from inventory_manager_mini.db.backup import check_sqlite_integrity, copy_database
from inventory_manager_mini.db.connection import connect, connect_readonly
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, create_schema
from tests.conftest import unchecked_constraints


def _create_inventory_database(
    path: Path,
    item_name: str,
    *,
    delete_journal: bool = False,
    schema_version: int = SCHEMA_VERSION,
) -> None:
    conn = sqlite3.connect(path, autocommit=True) if delete_journal else connect(path)
    try:
        if schema_version == 1:
            create_schema(conn, migrations._SCHEMA_V1_DDL, version=1)
        elif schema_version == 2:
            create_schema(
                conn,
                migrations._SCHEMA_V1_DDL + "\n" + migrations._MIGRATION_V2_SQL,
                version=2,
            )
        elif schema_version == 3:
            schema_v3 = files("inventory_manager_mini.db").joinpath("schema_v3.sql")
            create_schema(conn, schema_v3.read_text(encoding="utf-8"), version=3)
        else:
            create_schema(conn, version=schema_version)
        conn.executemany("INSERT INTO clients (id, name) VALUES (?, ?)", [(1, "クライアント")])
        conn.executemany("INSERT INTO purchasers (id, name) VALUES (?, ?)", [(1, "発注主体")])
        conn.executemany("INSERT INTO staff (id, name) VALUES (?, ?)", [(1, "担当者")])
        conn.executemany(
            "INSERT INTO categories (id, name, code_prefix) VALUES (?, ?, ?)",
            [(1, "備品", "EQ")],
        )
        conn.execute(
            "INSERT INTO items (client_id, purchaser_id, code, name, category_id) "
            "VALUES (1, 1, 'EQ-0001', ?, 1)",
            (item_name,),
        )
    finally:
        conn.close()


class FailingBackupConnection(sqlite3.Connection):
    def backup(
        self,
        target: sqlite3.Connection,
        *,
        pages: int = -1,
        progress: Callable[[int, int, int], object] | None = None,
        name: str = "main",
        sleep: float = 0.250,
    ) -> None:
        target_path = Path(target.execute("PRAGMA database_list").fetchone()[2])
        Path(f"{target_path}-wal").write_bytes(b"incomplete wal")
        Path(f"{target_path}-shm").write_bytes(b"incomplete shm")
        raise sqlite3.OperationalError("injected backup failure")


class FakeResult:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[str]]:
        return self._rows


class FailingIntegrityConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if sql == "PRAGMA integrity_check":
            return FakeResult([("page 1: malformed",)])  # type: ignore[return-value]
        return super().execute(sql, parameters)  # type: ignore[arg-type]


def test_check_sqlite_integrity_accepts_valid_database() -> None:
    conn = sqlite3.connect(":memory:", autocommit=True)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        assert check_sqlite_integrity(conn) == []
    finally:
        conn.close()


def test_check_sqlite_integrity_reports_integrity_and_foreign_key_failures() -> None:
    conn = sqlite3.connect(":memory:", autocommit=True, factory=FailingIntegrityConnection)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
        conn.execute("INSERT INTO child VALUES (42)")

        assert check_sqlite_integrity(conn) == [
            "SQLite 整合性検査に失敗しました: page 1: malformed",
            "外部キー制約に違反しています: テーブル child、行 1、参照先 parent、制約 0",
        ]
    finally:
        conn.close()


def test_copy_database_includes_committed_data_in_wal(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    source = connect(source_path)
    source.execute("PRAGMA wal_autocheckpoint = 0")
    source.execute("CREATE TABLE sample (value TEXT)")
    source.execute("INSERT INTO sample VALUES ('committed')")
    assert Path(f"{source_path}-wal").stat().st_size > 0

    destination_path = tmp_path / "backup.db"
    try:
        copy_database(source, destination_path)
    finally:
        source.close()

    destination = sqlite3.connect(destination_path)
    try:
        assert destination.execute("SELECT value FROM sample").fetchall() == [("committed",)]
    finally:
        destination.close()


def test_copy_database_refuses_existing_destination(tmp_path: Path) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination_path = tmp_path / "existing.db"
    destination_path.write_bytes(b"keep this file")
    original_hash = hashlib.sha256(destination_path.read_bytes()).digest()
    try:
        with pytest.raises(FileExistsError):
            copy_database(source, destination_path)
    finally:
        source.close()

    assert hashlib.sha256(destination_path.read_bytes()).digest() == original_hash


def test_copy_database_reports_destination_allocation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination_path = tmp_path / "unwritable" / "backup.db"

    def reject_destination(*args: object, **kwargs: object) -> int:
        raise PermissionError("injected destination permission failure")

    monkeypatch.setattr(backup.os, "open", reject_destination)
    try:
        with pytest.raises(PermissionError, match="injected destination permission failure"):
            copy_database(source, destination_path)
    finally:
        source.close()

    assert not destination_path.exists()


def test_copy_database_rejects_source_transaction_before_creating_destination(
    tmp_path: Path,
) -> None:
    source = sqlite3.connect(":memory:", autocommit=True)
    destination_path = tmp_path / "backup.db"
    source.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError):
            copy_database(source, destination_path)
        assert not destination_path.exists()
    finally:
        source.execute("ROLLBACK")
        source.close()


def test_copy_database_cleans_created_files_after_copy_failure(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    source = sqlite3.connect(source_path, autocommit=True, factory=FailingBackupConnection)
    destination_path = tmp_path / "failed.db"
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected backup failure"):
            copy_database(source, destination_path)
    finally:
        source.close()

    assert not Path(f"{destination_path}").exists()
    assert not Path(f"{destination_path}-wal").exists()
    assert not Path(f"{destination_path}-shm").exists()


def test_backup_service_creates_timestamped_backup_and_rejects_existing_file(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.db"
    _create_inventory_database(source_path, "元の品目")
    conn = connect(source_path)
    service = BackupService(clock=lambda: datetime(2026, 10, 3, 3, 4, 5, tzinfo=UTC), tz=UTC)
    try:
        backup_path = service.create_backup(conn, tmp_path / "backups")
        assert backup_path.name == "inventory_20261003_030405.db"
        with pytest.raises(ValidationError):
            service.create_backup(conn, tmp_path / "backups")
    finally:
        conn.close()

    backup_conn = connect_readonly(backup_path)
    try:
        assert backup_conn.execute("SELECT name FROM items").fetchone() == ("元の品目",)
    finally:
        backup_conn.close()


def test_backup_service_inspects_schema_and_business_integrity(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    _create_inventory_database(path, "品目")
    conn = connect(path)
    try:
        service = BackupService()
        assert service.inspect_database(conn, SCHEMA_VERSION) == []
        conn.execute("UPDATE items SET quantity = 1 WHERE id = 1")
        reasons = service.inspect_database(conn, SCHEMA_VERSION)
        assert any("在庫数が履歴合計と一致しません" in reason for reason in reasons)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_prepare_restore_migrates_v3_and_rechecks_fifo_integrity(tmp_path: Path) -> None:
    source_path = tmp_path / "v3-backup.db"
    _create_inventory_database(source_path, "復元品", schema_version=3)
    source_conn = connect(source_path)
    try:
        source_conn.execute("UPDATE items SET quantity = 1 WHERE id = 1")
        source_conn.executemany(
            "INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, "
            "reason, delta, unit_price, moved_at) VALUES (?, 1, 1, 1, 1, ?, ?, ?, ?)",
            [
                (10, "in", 2, 100, "2026-01-01 00:00:00"),
                (11, "out", -1, 50, "2026-01-02 00:00:00"),
            ],
        )
        source_conn.execute(
            "UPDATE total_aggregates SET inbound_quantity = 2, outbound_quantity = 1, "
            "expenditure = 50 WHERE id = 1"
        )
    finally:
        source_conn.close()
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()

    with BackupService().prepare_restore(source_path) as prepared:
        restored = connect_readonly(prepared.path)
        try:
            assert restored.execute("PRAGMA user_version").fetchone() == (4,)
            assert restored.execute(
                "SELECT unit_price, cost_amount FROM stock_movements WHERE id = 11"
            ).fetchone() == (None, 100)
            assert restored.execute(
                "SELECT movement_id, lot_id, quantity FROM stock_allocations"
            ).fetchall() == [(11, 10, 1)]
        finally:
            restored.close()

        writable = connect(prepared.path)
        try:
            writable.execute("UPDATE stock_allocations SET quantity = 2 WHERE movement_id = 11")
            reasons = BackupService().inspect_database(writable, 4)
            assert any("引当明細" in reason and "一致しません" in reason for reason in reasons)
        finally:
            writable.close()

    assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash


def test_prepare_restore_migrates_v2_database_through_v4(tmp_path: Path) -> None:
    source_path = tmp_path / "v2-backup.db"
    _create_inventory_database(source_path, "v2復元品", schema_version=2)
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()

    with BackupService().prepare_restore(source_path) as prepared:
        restored = connect_readonly(prepared.path)
        try:
            assert restored.execute("PRAGMA user_version").fetchone() == (4,)
            assert migrations.inspect_schema(restored, 4) == []
            assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
            assert restored.execute("SELECT name FROM items").fetchone() == ("v2復元品",)
        finally:
            restored.close()

    assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash


def test_backup_service_stops_before_business_checks_when_schema_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inventory.db"
    _create_inventory_database(path, "品目")
    conn = connect(path)
    conn.execute("DROP TRIGGER trg_items_updated_at")

    def fail_if_business_check_runs(self: object) -> list[tuple[int, int, int]]:
        raise AssertionError("業務検査へ進んではいけません")

    monkeypatch.setattr(
        services.ItemRepository,
        "find_quantity_mismatches",
        fail_if_business_check_runs,
    )
    try:
        reasons = BackupService().inspect_database(conn, SCHEMA_VERSION)
        assert any("スキーマ" in reason or "トリガー" in reason for reason in reasons)
    finally:
        conn.close()


def test_backup_service_rejects_invalid_settings_and_reversal_data(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.db"
    _create_inventory_database(settings_path, "設定検査品目")
    settings_conn = connect(settings_path)
    settings_conn.execute(
        "UPDATE settings SET value = ? WHERE key = ?", ("04", "fiscal_year_start_month")
    )
    try:
        settings_reasons = BackupService().inspect_database(settings_conn, SCHEMA_VERSION)
        assert any("年度開始月" in reason for reason in settings_reasons)
    finally:
        settings_conn.close()

    reversal_path = tmp_path / "reversal.db"
    _create_inventory_database(reversal_path, "取り消し検査品目")
    reversal_conn = connect(reversal_path)
    reversal_conn.execute("UPDATE items SET quantity = 2 WHERE id = 1")
    reversal_conn.executemany(
        "INSERT INTO stock_movements "
        "(item_id, client_id, purchaser_id, staff_id, reason, delta, reversal_of) "
        "VALUES (1, 1, 1, 1, 'in', 1, ?)",
        [(None,), (1,)],
    )
    try:
        reversal_reasons = BackupService().inspect_database(reversal_conn, SCHEMA_VERSION)
        assert any("取り消し履歴が不正" in reason for reason in reversal_reasons)
    finally:
        reversal_conn.close()


def test_prepare_restore_preserves_source_and_cleans_temporary_database(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.db"
    _create_inventory_database(source_path, "復元品目")
    original_hash = hashlib.sha256(source_path.read_bytes()).digest()
    service = BackupService()

    with service.prepare_restore(source_path) as prepared:
        assert prepared.source_version == SCHEMA_VERSION
        assert prepared.item_count == 1
        assert prepared.movement_count == 0
        assert prepared.last_moved_at is None
        assert prepared.path.exists()
        temp_path = prepared.path
    assert not temp_path.exists()
    assert hashlib.sha256(source_path.read_bytes()).digest() == original_hash


def test_prepare_restore_accepts_delete_journal_without_changing_source(tmp_path: Path) -> None:
    source_path = tmp_path / "source-delete.db"
    _create_inventory_database(source_path, "復元品目", delete_journal=True)
    original_hash = hashlib.sha256(source_path.read_bytes()).digest()
    source_conn = sqlite3.connect(source_path, autocommit=True)
    try:
        assert source_conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        source_conn.close()

    with BackupService().prepare_restore(source_path) as prepared:
        assert prepared.item_count == 1

    source_conn = sqlite3.connect(source_path, autocommit=True)
    try:
        assert source_conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        source_conn.close()
    assert hashlib.sha256(source_path.read_bytes()).digest() == original_hash


def test_prepare_restore_wraps_non_database_input_and_preserves_cause(tmp_path: Path) -> None:
    source_path = tmp_path / "not-a-database.db"
    source_path.write_bytes(b"not a sqlite database")

    with pytest.raises(InvalidBackupError) as error:
        BackupService().prepare_restore(source_path)

    assert error.value.reasons
    assert error.value.__cause__ is not None
    assert source_path.read_bytes() == b"not a sqlite database"


def test_prepare_restore_wraps_missing_source_and_preserves_cause(tmp_path: Path) -> None:
    with pytest.raises(InvalidBackupError) as error:
        BackupService().prepare_restore(tmp_path / "missing.db")

    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_prepare_restore_wraps_access_denied_and_preserves_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "denied.db"

    def deny_source(path: Path) -> sqlite3.Connection:
        raise PermissionError("injected access denial")

    monkeypatch.setattr(services, "connect_readonly", deny_source)
    with pytest.raises(InvalidBackupError) as error:
        BackupService().prepare_restore(source_path)

    assert isinstance(error.value.__cause__, PermissionError)


def test_prepare_restore_does_not_wrap_programming_sql_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.db"
    _create_inventory_database(source_path, "品目")

    def fail_schema_query(*args: object, **kwargs: object) -> list[str]:
        raise sqlite3.OperationalError("injected application SQL error")

    monkeypatch.setattr(services, "inspect_schema", fail_schema_query)
    with pytest.raises(sqlite3.OperationalError, match="injected application SQL error"):
        BackupService().prepare_restore(source_path)


def test_apply_restore_replaces_database_and_keeps_pre_restore_backup(tmp_path: Path) -> None:
    current_path = tmp_path / "current.db"
    source_path = tmp_path / "source.db"
    _create_inventory_database(current_path, "現行品目")
    _create_inventory_database(source_path, "復元品目")
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()
    service = BackupService(clock=lambda: datetime(2026, 10, 3, 3, 4, 5, tzinfo=UTC), tz=UTC)

    with service.prepare_restore(source_path) as prepared:
        backup_path = service.apply_restore(prepared, current_path, tmp_path / "backups")

    restored = connect_readonly(current_path)
    backup_conn = connect_readonly(backup_path)
    try:
        assert restored.execute("SELECT name FROM items").fetchone() == ("復元品目",)
        assert backup_conn.execute("SELECT name FROM items").fetchone() == ("現行品目",)
    finally:
        restored.close()
        backup_conn.close()
    assert backup_path.name == "pre-restore_20261003_030405.db"
    assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash


def test_apply_restore_refuses_existing_pre_restore_backup_without_changing_current(
    tmp_path: Path,
) -> None:
    current_path = tmp_path / "current.db"
    source_path = tmp_path / "source.db"
    _create_inventory_database(current_path, "現行品目")
    _create_inventory_database(source_path, "復元品目")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    existing_path = backup_dir / "pre-restore_20261003_030405.db"
    existing_path.write_bytes(b"keep")
    existing_hash = hashlib.sha256(existing_path.read_bytes()).digest()
    service = BackupService(clock=lambda: datetime(2026, 10, 3, 3, 4, 5, tzinfo=UTC), tz=UTC)

    with (
        service.prepare_restore(source_path) as prepared,
        pytest.raises(RestoreError) as error,
    ):
        service.apply_restore(prepared, current_path, backup_dir)

    assert error.value.stage == "pre_backup"
    assert error.value.recovered
    assert error.value.backup_path is None
    assert hashlib.sha256(existing_path.read_bytes()).digest() == existing_hash
    current = connect_readonly(current_path)
    try:
        assert current.execute("SELECT name FROM items").fetchone() == ("現行品目",)
    finally:
        current.close()


def test_prepare_restore_migrates_older_backup_in_temporary_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source-v1.db"
    current_path = tmp_path / "current-v2.db"
    _create_inventory_database(source_path, "旧版品目", schema_version=1)
    _create_inventory_database(current_path, "現行品目")
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()

    with BackupService().prepare_restore(source_path) as prepared:
        BackupService().apply_restore(prepared, current_path, tmp_path / "backups")
        prepared_conn = connect_readonly(prepared.path)
        try:
            assert prepared_conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
            assert prepared_conn.execute("SELECT name FROM items").fetchone() == ("旧版品目",)
        finally:
            prepared_conn.close()
    restored_conn = connect_readonly(current_path)
    try:
        assert restored_conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert restored_conn.execute("SELECT name FROM items").fetchone() == ("旧版品目",)
    finally:
        restored_conn.close()
    assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash


@pytest.mark.parametrize("case", ("zero", "unsupported_old", "too_new", "schema", "integrity"))
def test_prepare_restore_rejects_invalid_version_schema_and_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    source_path = tmp_path / f"{case}.db"
    _create_inventory_database(
        source_path,
        "品目",
        schema_version=1 if case == "unsupported_old" else SCHEMA_VERSION,
    )
    if case == "zero":
        source_conn = sqlite3.connect(source_path, autocommit=True)
        source_conn.execute("PRAGMA user_version = 0")
        source_conn.close()
    elif case == "unsupported_old":
        monkeypatch.setattr(services, "MIN_SUPPORTED_SCHEMA_VERSION", 2)
    elif case == "too_new":
        monkeypatch.setattr(services, "SCHEMA_VERSION", 0)
    elif case == "schema":
        source_conn = connect(source_path)
        source_conn.execute("DROP TRIGGER trg_items_updated_at")
        source_conn.close()
    elif case == "integrity":
        monkeypatch.setattr(
            services,
            "check_sqlite_integrity",
            lambda conn: ["注入した SQLite 整合性エラー"],
        )

    with pytest.raises(InvalidBackupError) as error:
        BackupService().prepare_restore(source_path)
    assert error.value.reasons


def test_prepare_restore_rejects_failed_temporary_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source-v1.db"
    _create_inventory_database(source_path, "旧版品目", schema_version=1)
    monkeypatch.setattr(
        services,
        "MIGRATIONS",
        {2: "CREATE INDEX failed_migration ON missing_table(id);"},
    )
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()

    with pytest.raises(InvalidBackupError, match="移行に失敗"):
        BackupService().prepare_restore(source_path)

    assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash


def test_backup_and_restore_reject_numeric_corruption_without_changing_source(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "corrupt.db"
    _create_inventory_database(source_path, "異常品目")
    conn = connect(source_path)
    try:
        with unchecked_constraints(conn):
            conn.execute("UPDATE items SET quantity = 1.5 WHERE id = 1")
    finally:
        conn.close()
    source_hash = hashlib.sha256(source_path.read_bytes()).digest()

    readonly_conn = connect_readonly(source_path)
    try:
        reasons = BackupService().inspect_database(readonly_conn, SCHEMA_VERSION)
        assert any(
            "quantity" in reason and "整数" in reason or "quantity" in reason for reason in reasons
        )
        with pytest.raises(ValidationError, match="バックアップを作成できません"):
            BackupService().create_backup(readonly_conn, tmp_path / "backups")
    finally:
        readonly_conn.close()

    with pytest.raises(InvalidBackupError):
        BackupService().prepare_restore(source_path)
    assert hashlib.sha256(source_path.read_bytes()).digest() == source_hash
    assert not (tmp_path / "backups").exists()


def test_backup_rejects_aggregate_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "aggregate-mismatch.db"
    _create_inventory_database(path, "品目")
    conn = connect(path)
    conn.execute("UPDATE total_aggregates SET expenditure = 1 WHERE id = 1")
    try:
        reasons = BackupService().inspect_database(conn, SCHEMA_VERSION)
        assert any("expenditure" in reason and "一致しません" in reason for reason in reasons)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("delta", "unit_price"),
    [(1.5, 100), (1, 1.5), ("abc", 100), (1, "abc")],
    ids=["real-delta", "real-price", "text-delta", "text-price"],
)
def test_inspect_database_reports_non_integer_movements_as_reasons(
    tmp_path: Path, delta: object, unit_price: object
) -> None:
    path = tmp_path / "non-integer-movement.db"
    _create_inventory_database(path, "品目")
    conn = connect(path)
    try:
        with unchecked_constraints(conn):
            conn.execute(
                "INSERT INTO stock_movements "
                "(item_id, client_id, purchaser_id, staff_id, reason, delta, unit_price) "
                "VALUES (1, 1, 1, 1, 'in', ?, ?)",
                (delta, unit_price),
            )
        reasons = BackupService().inspect_database(conn, SCHEMA_VERSION)
        assert any("stock_movements" in reason for reason in reasons)
    finally:
        conn.close()

    with pytest.raises(InvalidBackupError):
        BackupService().prepare_restore(path)


def test_cancel_removes_prepared_restore_directory(tmp_path: Path) -> None:
    source_path = tmp_path / "source.db"
    _create_inventory_database(source_path, "復元品目")
    service = BackupService()
    prepared = service.prepare_restore(source_path)
    prepared_path = prepared.path

    service.cancel(prepared)

    assert not prepared_path.exists()


def test_apply_restore_recovers_current_database_after_failed_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_path = tmp_path / "current.db"
    source_path = tmp_path / "source.db"
    _create_inventory_database(current_path, "現行品目")
    _create_inventory_database(source_path, "復元品目")
    service = BackupService()
    real_inspect = service.inspect_database
    with service.prepare_restore(source_path) as prepared:
        inspections = 0

        def fail_first_inspection(conn: sqlite3.Connection, version: int) -> list[str]:
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                return ["検査失敗"]
            return real_inspect(conn, version)

        monkeypatch.setattr(service, "inspect_database", fail_first_inspection)
        with pytest.raises(RestoreError) as error:
            service.apply_restore(prepared, current_path, tmp_path / "backups")

    assert error.value.stage == "overwrite"
    assert error.value.recovered
    assert error.value.backup_path is not None
    current = connect_readonly(current_path)
    try:
        assert current.execute("SELECT name FROM items").fetchone() == ("現行品目",)
    finally:
        current.close()


def test_apply_restore_reports_failed_recovery_and_preserves_automatic_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_path = tmp_path / "current.db"
    source_path = tmp_path / "source.db"
    _create_inventory_database(current_path, "現行品目")
    _create_inventory_database(source_path, "復元品目")
    service = BackupService()

    with service.prepare_restore(source_path) as prepared:
        monkeypatch.setattr(service, "inspect_database", lambda conn, version: ["検査失敗"])
        with pytest.raises(RestoreError) as error:
            service.apply_restore(prepared, current_path, tmp_path / "backups")

    assert error.value.stage == "recovery"
    assert not error.value.recovered
    assert error.value.backup_path is not None
    assert error.value.backup_path.exists()
