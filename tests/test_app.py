from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from inventory_manager_mini.app import main, setup_logging
from inventory_manager_mini.config import AppPaths
from inventory_manager_mini.core.errors import MigrationError
from inventory_manager_mini.db.migrations import SCHEMA_VERSION
from inventory_manager_mini.ui import single_instance
from inventory_manager_mini.ui.main_window import MainWindow
from tests.test_single_instance import start_lock_holder, stop_lock_holder


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root_logger = logging.getLogger()
    original_handlers = set(root_logger.handlers)
    original_level = root_logger.level
    yield
    root_logger.setLevel(original_level)
    for handler in tuple(root_logger.handlers):
        if handler not in original_handlers and isinstance(handler, RotatingFileHandler):
            root_logger.removeHandler(handler)
            handler.close()


def test_main_creates_database_and_log_shows_window_and_releases_lock(
    monkeypatch, tmp_path: Path, qtbot
) -> None:
    paths = _paths(tmp_path)
    shown_windows: list[MainWindow] = []
    original_show = MainWindow.show
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(
        MainWindow,
        "show",
        lambda window: (shown_windows.append(window), original_show(window)),
    )
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    assert main(paths) == 0

    assert paths.db_path.is_file()
    assert paths.log_path.is_file()
    assert len(shown_windows) == 1
    assert shown_windows[0].windowTitle() == "Inventory Manager mini"
    lock = single_instance.SingleInstanceLock(paths.lock_path)
    assert lock.try_acquire()
    lock.release()


def test_main_rejects_second_instance_without_opening_database_or_log(
    monkeypatch, tmp_path: Path, qtbot
) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    process = start_lock_holder(paths.lock_path)
    messages: list[tuple[object, str, str]] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args))

    try:
        assert main(paths) == 0
        assert messages == [(None, "inventory-manager-mini", "既に起動しています")]
        assert not paths.db_path.exists()
        assert not paths.log_path.exists()
    finally:
        stop_lock_holder(process)


def test_main_reports_newer_schema_and_releases_lock(monkeypatch, tmp_path: Path, qtbot) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    messages: list[tuple[object, str, str]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    assert main(paths) == 1

    assert messages[0][1] == "データベースを開けません"
    assert "より新しい" in messages[0][2]
    lock = single_instance.SingleInstanceLock(paths.lock_path)
    assert lock.try_acquire()
    lock.release()


def test_main_reports_unsupported_schema(monkeypatch, tmp_path: Path, qtbot) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    with closing(sqlite3.connect(paths.db_path)) as conn:
        conn.execute("PRAGMA user_version = 0")
    messages: list[tuple[object, str, str]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    assert main(paths) == 1

    assert messages[0][1] == "データベースを開けません"
    assert "サポートされていません" in messages[0][2]


def test_main_shows_migration_backup_path(monkeypatch, tmp_path: Path, qtbot) -> None:
    import inventory_manager_mini.app as app_module

    paths = _paths(tmp_path)
    backup_path = tmp_path / "backup.db"
    messages: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        app_module,
        "open_database",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            MigrationError("移行に失敗しました", backup_path=backup_path)
        ),
    )
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    assert main(paths) == 1

    assert str(backup_path) in messages[0][2]
    lock = single_instance.SingleInstanceLock(paths.lock_path)
    assert lock.try_acquire()
    lock.release()


def test_setup_logging_does_not_register_duplicate_handlers(tmp_path: Path) -> None:
    root_logger = logging.getLogger()
    log_path = tmp_path / "app.log"
    handlers_before = set(root_logger.handlers)
    try:
        setup_logging(log_path)
        setup_logging(log_path)
        matching_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == log_path.resolve()
        ]
        assert len(matching_handlers) == 1
    finally:
        for handler in tuple(root_logger.handlers):
            if handler not in handlers_before and isinstance(handler, RotatingFileHandler):
                root_logger.removeHandler(handler)
                handler.close()
