from __future__ import annotations

import sqlite3
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from inventory_manager_mini.core.errors import (
    MigrationError,
    SchemaTooNewError,
    UnsupportedSchemaError,
)
from inventory_manager_mini.db import migrations
from inventory_manager_mini.db.connection import connect, connect_memory
from inventory_manager_mini.db.migrations import (
    SCHEMA_SPECS,
    SCHEMA_VERSION,
    MigrationStep,
    check_migration_path,
    create_schema,
    inspect_schema,
    normalize_sql,
    open_database,
)


def _schema_sql() -> str:
    return files("inventory_manager_mini.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _create_database(
    path: Path, *, schema_sql: str | None = None, version: int = SCHEMA_VERSION
) -> None:
    conn = sqlite3.connect(path, autocommit=True)
    try:
        conn.executescript(_schema_sql() if schema_sql is None else schema_sql)
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


def _make_version_three_spec() -> migrations.SchemaSpec:
    conn = connect_memory()
    try:
        conn.executescript(migrations._SCHEMA_V1_DDL + "\n" + migrations._MIGRATION_V2_SQL)
        conn.execute("ALTER TABLE settings ADD COLUMN extra TEXT")
        return migrations._read_schema_spec(conn)
    finally:
        conn.close()


def _create_version_two_database(path: Path) -> None:
    _create_database(
        path,
        schema_sql=migrations._SCHEMA_V1_DDL + "\n" + migrations._MIGRATION_V2_SQL,
        version=2,
    )


def test_new_database_is_created_and_persists_schema(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261003_120000")
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert inspect_schema(conn, SCHEMA_VERSION) == []
        assert conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("fiscal_year_start_month",)
        ).fetchone() == ("4",)
    finally:
        conn.close()

    reopened = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    reopened.close()


def test_version_one_database_migrates_latest_purchase_index(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _create_database(path, schema_sql=migrations._SCHEMA_V1_DDL, version=1)

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261004_120000")
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert inspect_schema(conn, SCHEMA_VERSION) == []
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("idx_movements_item_purchase",),
        ).fetchone() == ("idx_movements_item_purchase",)
    finally:
        conn.close()


def test_version_two_rebuild_preserves_rows_and_initializes_aggregates(tmp_path: Path) -> None:
    path = tmp_path / "version-two.db"
    _create_version_two_database(path)
    conn = connect(path)
    conn.executescript(
        """INSERT INTO clients (id, name) VALUES (1, '利用者');
        INSERT INTO purchasers (id, name) VALUES (1, '発注主体');
        INSERT INTO staff (id, name) VALUES (1, '担当者');
        INSERT INTO categories (id, name, code_prefix) VALUES (1, '備品', 'EQ');
        INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity,
            reference_price) VALUES (7, 1, 1, 'EQ-0001', '品目', 1, 4, 200);
        INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id,
            reason, delta, unit_price) VALUES (11, 7, 1, 1, 1, 'in', 5, 200);
        INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id,
            reason, delta, unit_price, used_for) VALUES (12, 7, 1, 1, 1, 'out', -1, 200, '用途');"""
    )
    conn.close()

    migrated = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261009_120000")
    try:
        assert migrated.execute("PRAGMA user_version").fetchone() == (3,)
        assert migrated.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert inspect_schema(migrated, SCHEMA_VERSION) == []
        assert migrated.execute("SELECT id, quantity FROM items").fetchone() == (7, 4)
        assert migrated.execute("SELECT id FROM stock_movements ORDER BY id").fetchall() == [
            (11,),
            (12,),
        ]
        assert migrated.execute(
            "SELECT inbound_quantity, outbound_quantity, expenditure FROM total_aggregates"
        ).fetchone() == (5, 1, 200)
    finally:
        migrated.close()


def test_migration_precheck_rejects_legacy_limit_violation_before_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-invalid.db"
    _create_version_two_database(path)
    conn = connect(path)
    conn.executescript(
        """INSERT INTO clients (id, name) VALUES (1, '利用者');
        INSERT INTO purchasers (id, name) VALUES (1, '発注主体');
        INSERT INTO categories (id, name, code_prefix) VALUES (1, '備品', 'EQ');
        INSERT INTO items (id, client_id, purchaser_id, code, name, category_id,
            reference_price) VALUES (1, 1, 1, 'EQ-0001', '品目', 1, 10000001);"""
    )
    conn.close()

    with pytest.raises(MigrationError, match="データベースは変更されていません") as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    assert error.value.backup_path is None
    assert list((tmp_path / "backups").glob("pre-migrate_*.db")) == []
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (2,)
        assert verify.execute("SELECT reference_price FROM items").fetchone() == (10_000_001,)
    finally:
        verify.close()


def test_rebuild_migration_restores_foreign_keys_after_failure() -> None:
    conn = connect_memory()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(migrations._SCHEMA_V1_DDL + "\n" + migrations._MIGRATION_V2_SQL)

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE partial_rebuild (id INTEGER)")
        raise RuntimeError("rebuild failure")

    try:
        with pytest.raises(RuntimeError, match="rebuild failure"):
            migrations.migrate_schema(
                conn,
                2,
                3,
                {3: MigrationStep(fail_after_ddl, rebuilds_tables=True)},
                {**SCHEMA_SPECS, 3: SCHEMA_SPECS[2]},
            )
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'partial_rebuild'").fetchone()
            is None
        )
    finally:
        conn.close()


def test_create_schema_rolls_back_partial_ddl() -> None:
    conn = connect_memory()
    try:
        with pytest.raises(sqlite3.OperationalError):
            create_schema(conn, "CREATE TABLE partial (id INTEGER); INVALID DDL;")
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'partial'").fetchone() is None
        )
        assert conn.execute("PRAGMA user_version").fetchone() == (0,)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_new_database_failure_removes_database_and_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed.db"

    def fail_create(
        conn: sqlite3.Connection,
        *,
        version: int,
        specs: dict[int, migrations.SchemaSpec],
    ) -> None:
        create_schema(
            conn, "CREATE TABLE partial (id INTEGER); INVALID DDL;", version=version, specs=specs
        )

    monkeypatch.setattr(migrations, "create_schema", fail_create)
    with pytest.raises(sqlite3.OperationalError):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    assert not path.exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_existing_version_zero_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    with pytest.raises(UnsupportedSchemaError, match="版 0"):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
    assert path.exists()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "newer.db"
    _create_database(path, version=SCHEMA_VERSION + 1)
    with pytest.raises(SchemaTooNewError):
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")


@pytest.mark.parametrize(
    ("schema_version", "min_supported"),
    [(True, 1), (1, True), (0, 1), (1, 0), (1, 2)],
)
def test_invalid_version_configuration_is_rejected(
    tmp_path: Path, schema_version: int, min_supported: int
) -> None:
    with pytest.raises(ValueError):
        open_database(
            tmp_path / "inventory.db",
            tmp_path / "backups",
            backup_timestamp=lambda: "unused",
            schema_version=schema_version,
            min_supported=min_supported,
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("  note TEXT,\n", ""),
        (
            "code_prefix NOT GLOB '*[^A-Z0-9]*'",
            "code_prefix NOT GLOB '*[^a-z0-9]*'",
        ),
        (
            "code_prefix NOT GLOB '*[^A-Z0-9]*'",
            "1 /* code_prefix NOT GLOB '*[^A-Z0-9]*' */",
        ),
        (
            "length(code_prefix) BETWEEN 2 AND 5 AND code_prefix NOT GLOB '*[^A-Z0-9]*'",
            "length(code_prefix) BETWEEN 2 AND 5 OR 1",
        ),
        (
            "name TEXT NOT NULL UNIQUE,",
            "name TEXT NOT NULL,",
        ),
        (
            "client_id INTEGER NOT NULL REFERENCES clients(id),",
            "client_id INTEGER NOT NULL,",
        ),
        (
            "CREATE INDEX idx_movements_client_date ON stock_movements(client_id, moved_at);\n",
            "",
        ),
        (
            "CREATE TRIGGER trg_items_updated_at AFTER UPDATE ON items\n"
            "WHEN NEW.updated_at = OLD.updated_at\n"
            "BEGIN\n"
            "  UPDATE items SET updated_at = datetime('now') WHERE id = NEW.id;\n"
            "END;\n",
            "",
        ),
    ],
    ids=[
        "missing-column",
        "changed-glob",
        "commented-check",
        "or-one",
        "missing-unique",
        "missing-fk",
        "missing-index",
        "missing-trigger",
    ],
)
def test_inspect_schema_rejects_schema_changes(old: str, new: str) -> None:
    modified_sql = _schema_sql().replace(old, new, 1)
    assert modified_sql != _schema_sql()
    conn = connect_memory()
    try:
        conn.executescript(modified_sql)
        reasons = inspect_schema(conn, SCHEMA_VERSION)
        assert reasons
    finally:
        conn.close()


def test_inspect_schema_only_excludes_sqlite_underscore_prefix() -> None:
    conn = connect_memory()
    try:
        conn.executescript(_schema_sql())
        conn.execute('CREATE TABLE "sqliteX_user_table" (id INTEGER)')
        assert any(
            "sqliteX_user_table" in reason for reason in inspect_schema(conn, SCHEMA_VERSION)
        )
    finally:
        conn.close()


def test_normalize_sql_preserves_literals_and_quoted_identifiers() -> None:
    original = "CREATE TABLE \"MiXeD\" (value TEXT DEFAULT 'A  -- /* ''x'' */'); -- comment\n"
    formatted = (
        " create\n table \"MiXeD\"( value text default 'A  -- /* ''x'' */' ) ; /* comment */"
    )
    assert normalize_sql(original) == normalize_sql(formatted)
    assert normalize_sql(original) != normalize_sql(original.replace("'A  --", "'a  --"))
    assert normalize_sql(original) == normalize_sql(original.replace('"MiXeD"', '"mixed"'))


def test_normalize_sql_removes_comments_without_rewriting_literals() -> None:
    assert normalize_sql("SELECT '/* retained */' -- removed\nFROM sample") == normalize_sql(
        "select '/* retained */' from sample"
    )


def test_check_migration_path_requires_every_intermediate_version() -> None:
    check_migration_path(1, 3, {2: "SELECT 1;", 3: "SELECT 2;"})
    with pytest.raises(UnsupportedSchemaError, match="経路"):
        check_migration_path(1, 3, {3: "SELECT 2;"})


def test_migrates_one_version_and_creates_timestamped_backup(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    _create_version_two_database(path)
    source = connect(path)
    source.execute("UPDATE settings SET value = '9' WHERE key = 'fiscal_year_start_month'")
    source.close()

    version_three = _make_version_three_spec()
    specs = {**SCHEMA_SPECS, 3: version_three}
    backup_dir = tmp_path / "backups"
    conn = open_database(
        path,
        backup_dir,
        backup_timestamp=lambda: "20261003_123456",
        schema_version=3,
        min_supported=1,
        migrations={3: "ALTER TABLE settings ADD COLUMN extra TEXT;"},
        specs=specs,
    )
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
        assert inspect_schema(conn, 3, specs) == []
        assert conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("fiscal_year_start_month",)
        ).fetchone() == ("9",)
    finally:
        conn.close()

    backup = backup_dir / "pre-migrate_v2_20261003_123456.db"
    assert backup.exists()
    backup_conn = sqlite3.connect(backup)
    try:
        assert backup_conn.execute("PRAGMA user_version").fetchone() == (2,)
        assert backup_conn.execute(
            "SELECT value FROM settings WHERE key = ?", ("fiscal_year_start_month",)
        ).fetchone() == ("9",)
    finally:
        backup_conn.close()


def test_migration_backup_collision_preserves_existing_files(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    _create_version_two_database(path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    backup = backup_dir / "pre-migrate_v2_fixed.db"
    backup.write_bytes(b"keep existing backup")

    with pytest.raises(MigrationError) as error:
        open_database(
            path,
            backup_dir,
            backup_timestamp=lambda: "fixed",
            schema_version=3,
            migrations={3: "ALTER TABLE settings ADD COLUMN extra TEXT;"},
            specs={**SCHEMA_SPECS, 3: _make_version_three_spec()},
        )
    assert error.value.backup_path is None
    assert backup.read_bytes() == b"keep existing backup"
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (2,)
        assert "extra" not in {row[1] for row in verify.execute("PRAGMA table_info(settings)")}
    finally:
        verify.close()


def test_migration_sql_failure_rolls_back_and_keeps_backup(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    _create_version_two_database(path)
    source = connect(path)
    source.execute("UPDATE settings SET value = '9' WHERE key = 'fiscal_year_start_month'")
    source.close()
    with pytest.raises(MigrationError) as error:
        open_database(
            path,
            tmp_path / "backups",
            backup_timestamp=lambda: "20261003_130000",
            schema_version=3,
            migrations={3: "CREATE TABLE partial_migration (id INTEGER); INVALID DDL;"},
            specs={**SCHEMA_SPECS, 3: SCHEMA_SPECS[2]},
        )
    backup_path = error.value.backup_path
    assert backup_path is not None and backup_path.exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (2,)
        assert (
            verify.execute(
                "SELECT name FROM sqlite_master WHERE name = 'partial_migration'"
            ).fetchone()
            is None
        )
        assert verify.execute(
            "SELECT value FROM settings WHERE key = ?", ("fiscal_year_start_month",)
        ).fetchone() == ("9",)
    finally:
        verify.close()


def test_migration_schema_failure_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "inventory.db"
    _create_version_two_database(path)
    with pytest.raises(MigrationError, match="検査に失敗") as error:
        open_database(
            path,
            tmp_path / "backups",
            backup_timestamp=lambda: "20261003_130001",
            schema_version=3,
            migrations={3: "ALTER TABLE settings ADD COLUMN extra TEXT;"},
            specs={**SCHEMA_SPECS, 3: SCHEMA_SPECS[2]},
        )
    assert error.value.backup_path is not None and error.value.backup_path.exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (2,)
        assert "extra" not in {row[1] for row in verify.execute("PRAGMA table_info(settings)")}
    finally:
        verify.close()


def test_unsupported_old_version_and_missing_migration_path_are_rejected(
    tmp_path: Path,
) -> None:
    old_path = tmp_path / "old.db"
    _create_version_two_database(old_path)
    with pytest.raises(UnsupportedSchemaError, match="サポート"):
        open_database(
            old_path,
            tmp_path / "backups-old",
            backup_timestamp=lambda: "unused",
            min_supported=3,
            schema_version=3,
        )

    gap_path = tmp_path / "gap.db"
    _create_database(gap_path, schema_sql=migrations._SCHEMA_V1_DDL, version=1)
    with pytest.raises(UnsupportedSchemaError, match="経路"):
        open_database(
            gap_path,
            tmp_path / "backups-gap",
            backup_timestamp=lambda: "unused",
            schema_version=3,
            migrations={3: "SELECT 1;"},
            specs={**SCHEMA_SPECS, 3: SCHEMA_SPECS[2]},
        )


def test_version_three_spec_is_based_on_its_own_ddl_snapshot() -> None:
    version_three = _make_version_three_spec()
    settings = next(table for table in version_three.tables if table.name == "settings")
    assert (
        settings.sql
        != next(table for table in SCHEMA_SPECS[2].tables if table.name == "settings").sql
    )
    assert any(column.name == "extra" for column in settings.columns)


def test_spec_records_index_expression_and_trigger_definition() -> None:
    category_index = next(
        index for index in SCHEMA_SPECS[1].indexes if index.name == "uq_categories_parent_name"
    )
    trigger = SCHEMA_SPECS[1].triggers[0]
    assert category_index.columns_or_expressions == (None, "name")
    assert "coalesce" in normalize_sql(category_index.sql)
    assert "update items" in normalize_sql(trigger.sql)


def test_spec_is_immutable() -> None:
    table = SCHEMA_SPECS[1].tables[0]
    with pytest.raises(AttributeError):
        replace(table, name="changed").name = "other"  # type: ignore[misc]
