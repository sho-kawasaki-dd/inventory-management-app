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
from inventory_manager_mini.db import integrity, migrations
from inventory_manager_mini.db.connection import connect, connect_memory
from inventory_manager_mini.db.integrity import find_fifo_violations
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
        assert migrated.execute("PRAGMA user_version").fetchone() == (4,)
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
        assert migrated.execute(
            "SELECT unit_price, cost_amount FROM stock_movements WHERE id = 12"
        ).fetchone() == (None, 200)
        assert migrated.execute(
            "SELECT lot_id, quantity FROM stock_allocations WHERE movement_id = 12"
        ).fetchall() == [(11, 1)]
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


def test_version_three_spec_uses_frozen_schema_file() -> None:
    schema_v3 = files("inventory_manager_mini.db").joinpath("schema_v3.sql")
    assert SCHEMA_SPECS[3] == migrations._schema_spec_from_ddl(
        schema_v3.read_text(encoding="utf-8")
    )
    assert SCHEMA_SPECS[3] != SCHEMA_SPECS[4]


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


_LEGACY_MASTERS = """
INSERT INTO clients (id, name) VALUES (1, '利用者');
INSERT INTO purchasers (id, name) VALUES (1, '発注主体');
INSERT INTO staff (id, name, is_active) VALUES (1, '担当者', 1), (2, '退職者', 0);
INSERT INTO categories (id, name, code_prefix) VALUES (1, '備品', 'EQ');
INSERT INTO locations (id, name) VALUES (1, '倉庫');
"""

_LEGACY_TABLES = (
    "clients",
    "purchasers",
    "staff",
    "categories",
    "locations",
    "items",
    "stock_movements",
    "settings",
)


def _create_legacy_database(path: Path, statements: str, *, version: int = 2) -> None:
    if version == 3:
        ddl = (
            files("inventory_manager_mini.db").joinpath("schema_v3.sql").read_text(encoding="utf-8")
        )
    else:
        ddl = migrations._SCHEMA_V1_DDL
    if version == 2:
        ddl += "\n" + migrations._MIGRATION_V2_SQL
    _create_database(path, schema_sql=ddl, version=version)
    conn = sqlite3.connect(path, autocommit=True)  # 外部キー無効のまま投入する
    try:
        conn.executescript(_LEGACY_MASTERS + statements)
    finally:
        conn.close()


def _table_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in _LEGACY_TABLES
    }


def _schema_object_names(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return {
        (str(kind), str(name), str(table))
        for kind, name, table in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        )
    }


_RICH_LEGACY_ROWS = """
INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, location_id, quantity,
    reorder_threshold, reorder_quantity, reference_price, is_active, created_at, updated_at)
VALUES
    (3, 1, 1, 'EQ-0003', '稼働品', 1, 1, 5, 2, 10, 500, 1,
     '2026-01-01 00:00:00', '2026-01-02 00:00:00'),
    (9, 1, 1, 'EQ-0009', '廃止品', 1, NULL, 0, 0, NULL, NULL, 0,
     '2026-02-01 00:00:00', '2026-02-01 00:00:00');
INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason, delta,
    unit_price, used_for, reversal_of, note, moved_at)
VALUES
    (20, 3, 1, 1, 1, 'in', 10, 500, NULL, NULL, '入庫', '2026-01-10 00:00:00'),
    (21, 3, 1, 1, 1, 'out', -4, 500, '用途', NULL, NULL, '2026-01-11 00:00:00'),
    (22, 3, 1, 1, 1, 'dispose', -1, 500, NULL, NULL, NULL, '2026-01-12 00:00:00'),
    (23, 3, 1, 1, 1, 'return', -1, NULL, NULL, NULL, NULL, '2026-01-13 00:00:00'),
    (24, 3, 1, 1, 2, 'out', 4, 500, '用途', 21, '取り消し', '2026-01-14 00:00:00'),
    (25, 3, 1, 1, 1, 'out', -3, 500, '別用途', NULL, NULL, '2026-01-15 00:00:00'),
    (30, 9, 1, 1, 1, 'in', 2, NULL, NULL, NULL, NULL, '2026-02-02 00:00:00'),
    (31, 9, 1, 1, 1, 'adjust', -2, NULL, NULL, NULL, NULL, '2026-02-03 00:00:00'),
    (32, 9, 1, 1, 1, 'adjust', 0, 300, NULL, NULL, NULL, '2026-02-04 00:00:00');
"""


def test_version_two_rebuild_preserves_every_row_reference_index_and_trigger(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rich-v2.db"
    _create_legacy_database(path, _RICH_LEGACY_ROWS)
    legacy = sqlite3.connect(path, autocommit=True)
    try:
        rows_before = _table_rows(legacy)
    finally:
        legacy.close()
    fresh_path = tmp_path / "fresh.db"
    fresh = open_database(fresh_path, tmp_path / "fresh-backups", backup_timestamp=lambda: "x")
    try:
        fresh_objects = _schema_object_names(fresh)
    finally:
        fresh.close()

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261009_130000")
    try:
        preserved_tables = (
            "clients",
            "purchasers",
            "staff",
            "categories",
            "locations",
            "items",
            "settings",
        )
        for table in preserved_tables:
            assert _table_rows(conn)[table] == rows_before[table]
        assert conn.execute(
            "SELECT id, unit_price, cost_amount FROM stock_movements ORDER BY id"
        ).fetchall() == [
            (20, 500, None),
            (21, None, 2000),
            (22, None, 500),
            (23, None, None),
            (24, None, -2000),
            (25, None, 1500),
            (30, None, None),
            (31, None, None),
            (32, None, None),
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert find_fifo_violations(conn) == []
        assert inspect_schema(conn, SCHEMA_VERSION) == []
        assert _schema_object_names(conn) == fresh_objects
        assert ("index", "idx_movements_item_purchase", "stock_movements") in fresh_objects
        assert conn.execute("SELECT * FROM total_aggregates").fetchall() == [
            (1, 12, 3, 1, 1500, 500)
        ]
        assert conn.execute(
            "SELECT movement_id, lot_id, quantity FROM stock_allocations ORDER BY id"
        ).fetchall() == [
            (21, 20, 4),
            (22, 20, 1),
            (23, 20, 1),
            (24, 20, -4),
            (25, 20, 3),
            (31, 30, 2),
        ]
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, "
                "reason, delta, unit_price, reversal_of) VALUES (3, 1, 1, 1, 'out', 4, 500, 21)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, "
                "reason, delta, reversal_of) VALUES (3, 1, 1, 1, 'out', 1, 9999)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE items SET quantity = 1000001 WHERE id = 3")
        conn.execute("UPDATE items SET name = '更新後' WHERE id = 3")
        assert conn.execute("SELECT updated_at FROM items WHERE id = 3").fetchone() != (
            "2026-01-02 00:00:00",
        )
    finally:
        conn.close()

    backup = sqlite3.connect(tmp_path / "backups" / "pre-migrate_v2_20261009_130000.db")
    try:
        assert backup.execute("PRAGMA user_version").fetchone() == (2,)
        assert _table_rows(backup) == rows_before
    finally:
        backup.close()


def test_v4_fifo_integrity_check_rejects_changed_allocations(tmp_path: Path) -> None:
    path = tmp_path / "v3-corrupt-allocation.db"
    _create_legacy_database(path, _RICH_LEGACY_ROWS, version=3)
    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261010_120100")
    try:
        conn.execute("UPDATE stock_allocations SET quantity = quantity + 1 WHERE movement_id = 21")
        violations = find_fifo_violations(conn)
        assert any("履歴 ID 21" in reason and "一致しません" in reason for reason in violations)
    finally:
        conn.close()


def test_failed_v4_rebuild_rolls_back_schema_data_version_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "v4-rebuild-failure.db"
    _create_legacy_database(path, _RICH_LEGACY_ROWS, version=3)
    conn = connect(path)
    try:
        rows_before = _table_rows(conn)

        def rebuild_then_fail(connection: sqlite3.Connection) -> None:
            migrations._migrate_v3_to_v4(connection)
            raise RuntimeError("after v4 rebuild")

        with pytest.raises(RuntimeError, match="after v4 rebuild"):
            migrations.migrate_schema(
                conn,
                3,
                4,
                {4: MigrationStep(rebuild_then_fail, rebuilds_tables=True)},
            )
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
        assert inspect_schema(conn, 3) == []
        assert _table_rows(conn) == rows_before
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'stock_allocations'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_version_one_database_with_history_migrates_through_every_step(tmp_path: Path) -> None:
    path = tmp_path / "v1-history.db"
    _create_legacy_database(path, _RICH_LEGACY_ROWS, version=1)

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261009_130100")
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert inspect_schema(conn, SCHEMA_VERSION) == []
        assert conn.execute("SELECT * FROM total_aggregates").fetchall() == [
            (1, 12, 3, 1, 1500, 500)
        ]
    finally:
        conn.close()
    assert (tmp_path / "backups" / "pre-migrate_v1_20261009_130100.db").exists()


def test_version_three_migrates_fifo_history_and_matches_new_schema(tmp_path: Path) -> None:
    path = tmp_path / "v3-history.db"
    _create_legacy_database(path, _RICH_LEGACY_ROWS, version=3)

    conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261010_120000")
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (4,)
        assert inspect_schema(conn, 4) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT id, unit_price, cost_amount FROM stock_movements WHERE id IN (21, 24, 25) "
            "ORDER BY id"
        ).fetchall() == [(21, None, 2000), (24, None, -2000), (25, None, 1500)]
        assert conn.execute(
            "SELECT movement_id, lot_id, quantity FROM stock_allocations ORDER BY id"
        ).fetchall() == [
            (21, 20, 4),
            (22, 20, 1),
            (23, 20, 1),
            (24, 20, -4),
            (25, 20, 3),
            (31, 30, 2),
        ]
    finally:
        conn.close()


def test_v2_v3_legacy_aggregates_are_preserved_until_v4_conversion(tmp_path: Path) -> None:
    path = tmp_path / "legacy-aggregates.db"
    statements = """
    INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity)
    VALUES (1, 1, 1, 'EQ-0001', '品目', 1, 6);
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (10, 1, 1, 1, 1, 'in', 10, 100, '2026-01-01 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (11, 1, 1, 1, 1, 'out', -4, 500, '2026-01-02 00:00:00');
    """
    _create_legacy_database(path, statements)
    conn = connect(path)
    try:
        migrations.migrate_schema(conn, 2, 3)
        assert conn.execute("PRAGMA user_version").fetchone() == (3,)
        assert conn.execute(
            "SELECT inbound_quantity, outbound_quantity, expenditure FROM total_aggregates"
        ).fetchone() == (10, 4, 2000)
    finally:
        conn.close()

    migrated = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261010_120200")
    try:
        assert migrated.execute("PRAGMA user_version").fetchone() == (4,)
        assert migrated.execute(
            "SELECT inbound_quantity, outbound_quantity, expenditure FROM total_aggregates"
        ).fetchone() == (10, 4, 400)
        assert migrated.execute(
            "SELECT unit_price, cost_amount FROM stock_movements WHERE id = 11"
        ).fetchone() == (None, 400)
    finally:
        migrated.close()


def test_v3_fifo_cost_over_limit_is_rejected_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "v3-fifo-cost-too-high.db"
    statements = """
    INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity)
    VALUES (1, 1, 1, 'EQ-0001', '品目', 1, 100001);
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (10, 1, 1, 1, 1, 'in', 100001, 1000, '2026-01-01 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (11, 1, 1, 1, 1, 'out', -100001, 0, '2026-01-02 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, reversal_of, moved_at)
    VALUES (12, 1, 1, 1, 1, 'out', 100001, 0, 11, '2026-01-03 00:00:00');
    """
    _create_legacy_database(path, statements, version=3)

    with pytest.raises(MigrationError, match="cost_amount") as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")

    assert "ID 11" in str(error.value)
    assert "ID 12" in str(error.value)
    assert "100001000" in str(error.value)
    assert error.value.backup_path is None
    assert not (tmp_path / "backups").exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (3,)
        assert verify.execute(
            "SELECT unit_price FROM stock_movements WHERE id = 11"
        ).fetchone() == (0,)
    finally:
        verify.close()


def test_v3_consumed_lot_reversal_is_rejected_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "v3-consumed-lot.db"
    statements = """
    INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity)
    VALUES (1, 1, 1, 'EQ-0001', '品目', 1, 0);
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (10, 1, 1, 1, 1, 'in', 5, 100, '2026-01-01 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (11, 1, 1, 1, 1, 'out', -3, 100, '2026-01-02 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (12, 1, 1, 1, 1, 'in', 3, 100, '2026-01-03 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, reversal_of, moved_at)
    VALUES (13, 1, 1, 1, 1, 'in', -5, 100, 10, '2026-01-04 00:00:00');
    """
    _create_legacy_database(path, statements, version=3)

    with pytest.raises(MigrationError, match="消費済み") as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")

    assert error.value.backup_path is None
    assert not (tmp_path / "backups").exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (3,)
        assert verify.execute("SELECT COUNT(*) FROM stock_movements").fetchone() == (4,)
    finally:
        verify.close()


@pytest.mark.parametrize(("aggregate_limit", "rejected"), [(10, False), (9, True)])
def test_v3_fifo_aggregate_limit_boundary_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    aggregate_limit: int,
    rejected: bool,
) -> None:
    monkeypatch.setattr(integrity, "MAX_AGGREGATE_VALUE", aggregate_limit)
    path = tmp_path / f"v3-fifo-aggregate-{aggregate_limit}.db"
    statements = """
    INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity)
    VALUES (1, 1, 1, 'EQ-0001', '品目', 1, 0);
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (10, 1, 1, 1, 1, 'in', 1, 10, '2026-01-01 00:00:00');
    INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, reason,
        delta, unit_price, moved_at)
    VALUES (11, 1, 1, 1, 1, 'out', -1, 0, '2026-01-02 00:00:00');
    """
    _create_legacy_database(path, statements, version=3)

    if rejected:
        with pytest.raises(MigrationError, match="expenditure") as error:
            open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")
        assert error.value.backup_path is None
        assert not (tmp_path / "backups").exists()
        verify = sqlite3.connect(path)
        try:
            assert verify.execute("PRAGMA user_version").fetchone() == (3,)
        finally:
            verify.close()
    else:
        conn = open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261010_120300")
        try:
            assert conn.execute("PRAGMA user_version").fetchone() == (4,)
            assert conn.execute("SELECT expenditure FROM total_aggregates").fetchone() == (10,)
        finally:
            conn.close()


_ITEM = (
    "INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity, "
    "reference_price) VALUES (1, 1, 1, 'EQ-0001', '品目', 1, {quantity}, {price});"
)
_MOVEMENT = (
    "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, reason, delta, "
    "unit_price) VALUES (1, 1, 1, 1, '{reason}', {delta}, {price});"
)


@pytest.mark.parametrize(
    ("statements", "expected", "aggregate_limit"),
    [
        (_ITEM.format(quantity=0, price="1.5"), "reference_price", None),
        (_ITEM.format(quantity="'abc'", price="NULL"), "quantity", None),
        (
            _ITEM.format(quantity=1_000_001, price="NULL")
            + _MOVEMENT.format(reason="in", delta=1_000_001, price="NULL"),
            "delta",
            None,
        ),
        (
            _ITEM.format(quantity=1, price="NULL")
            + _MOVEMENT.format(reason="in", delta=1, price=10_000_001),
            "unit_price",
            None,
        ),
        (
            _ITEM.format(quantity=0, price="NULL")
            + _MOVEMENT.format(reason="in", delta=10_001, price="NULL")
            + _MOVEMENT.format(reason="out", delta=-10_001, price=10_000),
            "amount",
            None,
        ),
        (
            _ITEM.format(quantity=0, price="NULL")
            + _MOVEMENT.format(reason="in", delta=10_001, price="NULL")
            + _MOVEMENT.format(reason="dispose", delta=-10_001, price=10_000),
            "amount",
            None,
        ),
        (_ITEM.format(quantity=5, price="NULL"), "履歴合計", None),
        (
            _ITEM.format(quantity=10, price="NULL")
            + _MOVEMENT.format(reason="in", delta=10, price="NULL"),
            "inbound_quantity",
            5,
        ),
    ],
    ids=[
        "real-price",
        "text-quantity",
        "delta-over-limit",
        "unit-price-over-limit",
        "issue-amount-over-limit",
        "dispose-amount-over-limit",
        "quantity-history-mismatch",
        "aggregate-over-limit",
    ],
)
def test_migration_precheck_rejects_each_legacy_violation_before_any_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statements: str,
    expected: str,
    aggregate_limit: int | None,
) -> None:
    if aggregate_limit is not None:
        monkeypatch.setattr(integrity, "MAX_AGGREGATE_VALUE", aggregate_limit)
    path = tmp_path / "legacy-violation.db"
    _create_legacy_database(path, statements)

    with pytest.raises(MigrationError, match="データベースは変更されていません") as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")

    assert expected in str(error.value)
    assert error.value.backup_path is None
    assert not (tmp_path / "backups").exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (2,)
        assert inspect_schema(verify, 2) == []
    finally:
        verify.close()


def test_migration_precheck_truncates_reported_violations_to_twenty(tmp_path: Path) -> None:
    path = tmp_path / "many-violations.db"
    items = "".join(
        "INSERT INTO items (id, client_id, purchaser_id, code, name, category_id, quantity) "
        f"VALUES ({index}, 1, 1, 'EQ-{index:04d}', '品目{index}', 1, 1);"
        for index in range(1, 26)
    )
    _create_legacy_database(path, items)

    with pytest.raises(MigrationError) as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")

    assert str(error.value).count("在庫数が履歴合計と一致しません") == 20
    assert "ほか 5 件" in str(error.value)


def test_migration_precheck_covers_every_pending_version(tmp_path: Path) -> None:
    path = tmp_path / "v1-violation.db"
    _create_legacy_database(path, _ITEM.format(quantity=5, price="NULL"), version=1)

    with pytest.raises(MigrationError, match="データベースは変更されていません") as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "unused")

    assert error.value.backup_path is None
    assert not (tmp_path / "backups").exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (1,)
        assert inspect_schema(verify, 1) == []
    finally:
        verify.close()


def test_rebuild_failure_after_real_rebuild_restores_schema_data_version_and_foreign_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rebuild-failure.db"
    _create_legacy_database(path, _RICH_LEGACY_ROWS)
    legacy = sqlite3.connect(path, autocommit=True)
    try:
        rows_before = _table_rows(legacy)
    finally:
        legacy.close()

    def rebuild_then_fail(connection: sqlite3.Connection) -> None:
        migrations._migrate_v2_to_v3(connection)
        raise RuntimeError("after rebuild")

    conn = connect(path)
    try:
        with pytest.raises(RuntimeError, match="after rebuild"):
            migrations.migrate_schema(
                conn, 2, 3, {3: MigrationStep(rebuild_then_fail, rebuilds_tables=True)}
            )
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
        assert inspect_schema(conn, 2) == []
        assert _table_rows(conn) == rows_before
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'total_aggregates'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_migration_rolls_back_when_foreign_key_check_finds_orphans(tmp_path: Path) -> None:
    path = tmp_path / "orphan.db"
    _create_legacy_database(
        path,
        "INSERT INTO items (id, client_id, purchaser_id, code, name, category_id) "
        "VALUES (1, 1, 1, 'EQ-0001', '孤児', 999);",
    )

    with pytest.raises(MigrationError, match="外部キー検査に失敗") as error:
        open_database(path, tmp_path / "backups", backup_timestamp=lambda: "20261009_130200")

    assert error.value.backup_path is not None and error.value.backup_path.exists()
    verify = sqlite3.connect(path)
    try:
        assert verify.execute("PRAGMA user_version").fetchone() == (2,)
        assert inspect_schema(verify, 2) == []
        assert verify.execute("SELECT id, category_id FROM items").fetchall() == [(1, 999)]
    finally:
        verify.close()


def test_check_migration_path_accepts_migration_steps() -> None:
    steps = {2: MigrationStep("SELECT 1;"), 3: MigrationStep(lambda conn: None)}
    check_migration_path(1, 3, steps)
    with pytest.raises(UnsupportedSchemaError, match="経路"):
        check_migration_path(1, 3, {2: steps[2]})


def test_normalize_sql_unquotes_only_simple_identifiers() -> None:
    assert normalize_sql('CREATE TABLE "items" (id INTEGER)') == normalize_sql(
        "CREATE TABLE items (id INTEGER)"
    )
    assert normalize_sql('SELECT "my col" FROM t') != normalize_sql("SELECT my col FROM t")
    assert normalize_sql('SELECT "1x" FROM t') != normalize_sql("SELECT 1x FROM t")
    assert normalize_sql('SELECT "items"') != normalize_sql("SELECT 'items'")
