from __future__ import annotations

import argparse
import sqlite3

import pytest

from inventory_manager_mini.core.services import BackupService
from inventory_manager_mini.db.migrations import SCHEMA_VERSION
from scripts.generate_dummy_data import generate_database, main


def _args(path, *, force: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        db=path,
        items=50,
        movements=500,
        years=3,
        seed=7,
        force=force,
    )


def test_generates_inspectable_database(tmp_path) -> None:
    path = tmp_path / "dummy.db"

    items, movements, elapsed = generate_database(_args(path))

    assert (items, movements) == (50, 500)
    assert elapsed >= 0
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 50
        assert conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 500
        assert {row[0] for row in conn.execute("SELECT DISTINCT reason FROM stock_movements")} == {
            "in",
            "out",
            "return",
            "dispose",
            "adjust",
        }
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM stock_movements WHERE reversal_of IS NOT NULL"
            ).fetchone()[0]
            >= 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM stock_movements m JOIN items i ON i.id = m.item_id "
                "WHERE m.client_id != i.client_id OR m.purchaser_id != i.purchaser_id"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM staff WHERE is_active = 0").fetchone()[0] >= 1
        conn.close()
        readonly_conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            assert BackupService().inspect_database(readonly_conn, SCHEMA_VERSION) == []
        finally:
            readonly_conn.close()
    finally:
        if conn:
            conn.close()


def test_existing_database_is_refused_without_force(tmp_path) -> None:
    path = tmp_path / "existing.db"
    path.write_bytes(b"keep")

    assert main(["--db", str(path), "--items", "50", "--movements", "500"]) == 1
    assert path.read_bytes() == b"keep"


def test_force_replaces_database_and_sqlite_sidecars(tmp_path) -> None:
    path = tmp_path / "existing.db"
    path.write_bytes(b"old")
    (tmp_path / "existing.db-wal").write_bytes(b"wal")
    (tmp_path / "existing.db-shm").write_bytes(b"shm")

    assert main(["--db", str(path), "--items", "50", "--movements", "500", "--force"]) == 0
    assert path.is_file()
    wal_path = tmp_path / "existing.db-wal"
    shm_path = tmp_path / "existing.db-shm"
    assert not wal_path.exists() or wal_path.read_bytes() != b"wal"
    assert not shm_path.exists() or shm_path.read_bytes() != b"shm"


@pytest.mark.parametrize(
    ("option", "value"),
    [("--items", "0"), ("--movements", "5"), ("--years", "0")],
)
def test_invalid_counts_are_rejected(tmp_path, option: str, value: str) -> None:
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "invalid.db"), option, value])
