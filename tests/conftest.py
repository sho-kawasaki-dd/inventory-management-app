from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from inventory_manager_mini.config import AppPaths
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.core.services import (
    BackupService,
    InventoryService,
    MasterService,
    SettingsService,
)
from inventory_manager_mini.db.connection import connect_memory, transaction
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, create_schema
from inventory_manager_mini.ui import display_settings
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.signals import DataBus

# 1366×768 の画面からタスクバー分を除いた作業領域の目安
WORK_AREA_WIDTH = 1366
WORK_AREA_HEIGHT = 768 - 40


@pytest.fixture(scope="session")
def qapp_args(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        return []
    config_path = tmp_path_factory.mktemp("qt-offscreen") / "config.json"
    config_path.write_text(
        json.dumps({"screens": [{"width": WORK_AREA_WIDTH, "height": WORK_AREA_HEIGHT}]}),
        encoding="utf-8",
    )
    return ["pytest", "-platform", f"offscreen:configfile={config_path}"]


@pytest.fixture
def restore_app_font(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> Iterator[QFont]:
    original = QFont(qapp.font())
    monkeypatch.setattr(display_settings, "_base_font", None)
    yield original
    QApplication.setFont(original)


def assert_fits_work_area(widget: QWidget) -> None:
    """ウィンドウ枠を含めて画面の作業領域と 1366×768 の作業領域に収まることを検証する。"""
    frame = widget.frameGeometry()
    assert widget.screen().availableGeometry().contains(frame)
    assert frame.width() <= WORK_AREA_WIDTH
    assert frame.height() <= WORK_AREA_HEIGHT


def assert_buttons_reachable(widget: QWidget, *buttons: QWidget) -> None:
    """ボタンが表示され、スクロールせずに操作できる位置にあることを検証する。"""
    bounds = widget.rect()
    for button in buttons:
        assert button.isVisible()
        top_left = button.mapTo(widget, QPoint(0, 0))
        assert bounds.contains(QRect(top_left, button.size()))


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


def create_test_context(
    conn: sqlite3.Connection,
    *,
    inventory: InventoryService | None = None,
    master: MasterService | None = None,
    settings: SettingsService | None = None,
    backup: BackupService | None = None,
    db_path: Path | None = None,
    schema_version: int = SCHEMA_VERSION,
    app_version: str = "0.1.0",
) -> AppContext:
    context_db_path = db_path or Path(":memory:")
    data_dir = context_db_path.parent
    paths = AppPaths(
        data_dir=data_dir,
        db_path=context_db_path,
        backup_dir=data_dir / "backups",
        lock_path=data_dir / "inventory.lock",
        ui_settings_path=data_dir / "ui_settings.json",
        log_dir=data_dir / "logs",
        log_path=data_dir / "logs" / "app.log",
    )
    return AppContext(
        inventory=inventory or InventoryService(conn),
        master=master or MasterService(conn),
        settings=settings or SettingsService(conn),
        reports=ReportService(conn),
        backup=backup or BackupService(conn),
        paths=paths,
        data_bus=DataBus(),
        db_path=context_db_path,
        schema_version=schema_version,
        app_version=app_version,
    )


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


@pytest.fixture
def bulk_history() -> Callable[..., list[int]]:
    """入庫履歴を直接投入する(items.quantity・集計は更新しない)。表示・絞り込み検証用。"""

    def insert(
        conn: sqlite3.Connection,
        item_id: int,
        count: int,
        start: datetime = datetime(2025, 6, 1, tzinfo=UTC),
    ) -> list[int]:
        moved_ats = [
            (start + timedelta(seconds=index)).strftime("%Y-%m-%d %H:%M:%S")
            for index in range(count)
        ]
        with transaction(conn):
            conn.executemany(
                "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, "
                "reason, delta, unit_price, moved_at) "
                "SELECT id, client_id, purchaser_id, 1, 'in', 1, 1, ? FROM items WHERE id = ?",
                [(moved_at, item_id) for moved_at in moved_ats],
            )
        rows = conn.execute(
            "SELECT id FROM stock_movements WHERE item_id = ? ORDER BY id DESC LIMIT ?",
            (item_id, count),
        ).fetchall()
        return [row[0] for row in reversed(rows)]

    return insert
