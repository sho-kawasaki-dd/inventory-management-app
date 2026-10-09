from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from inventory_manager_mini.core.services import (
    InventoryService,
    MasterService,
    SettingsService,
)
from inventory_manager_mini.db.connection import connect_memory
from inventory_manager_mini.db.migrations import create_schema


@contextmanager
def unchecked_constraints(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("PRAGMA ignore_check_constraints = ON")
    try:
        yield
    finally:
        conn.execute("PRAGMA ignore_check_constraints = OFF")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-perf",
        action="store_true",
        default=False,
        help="性能計測テストを実行する",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-perf"):
        return
    skip_perf = pytest.mark.skip(reason="--run-perf が指定されていません")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip_perf)


class FixedClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> datetime:
        self.current += timedelta(**kwargs)
        return self.current


@pytest.fixture
def memory_conn() -> Iterator[sqlite3.Connection]:
    conn = connect_memory()
    create_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def seeded_conn(memory_conn: sqlite3.Connection) -> sqlite3.Connection:
    memory_conn.executemany(
        "INSERT INTO clients (id, name, is_active) VALUES (?, ?, ?)",
        [(1, "総務", 1), (2, "経理", 1), (3, "廃止クライアント", 0)],
    )
    memory_conn.executemany(
        "INSERT INTO purchasers (id, name, is_active) VALUES (?, ?, ?)",
        [(1, "本部", 1), (2, "部門", 1), (3, "廃止発注主体", 0)],
    )
    memory_conn.executemany(
        "INSERT INTO staff (id, name, is_active) VALUES (?, ?, ?)",
        [(1, "担当A", 1), (2, "担当B", 1), (3, "廃止担当", 0)],
    )
    memory_conn.executemany(
        "INSERT INTO categories (id, parent_id, name, code_prefix, next_seq) "
        "VALUES (?, ?, ?, ?, ?)",
        [(1, None, "備品", "TO", 1), (2, 1, "文具", "ST", 1)],
    )
    memory_conn.executemany(
        "INSERT INTO locations (id, name) VALUES (?, ?)",
        [(1, "倉庫"), (2, "事務室")],
    )
    return memory_conn


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def inventory(seeded_conn: sqlite3.Connection, fixed_clock: FixedClock) -> InventoryService:
    return InventoryService(seeded_conn, clock=fixed_clock)


@pytest.fixture
def master(seeded_conn: sqlite3.Connection) -> MasterService:
    return MasterService(seeded_conn)


@pytest.fixture
def settings(seeded_conn: sqlite3.Connection) -> SettingsService:
    return SettingsService(seeded_conn)
