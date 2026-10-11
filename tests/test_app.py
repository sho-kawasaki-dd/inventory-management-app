from __future__ import annotations

import logging
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from inventory_manager_mini.app import main, setup_logging
from inventory_manager_mini.config import AppPaths, ensure_dirs
from inventory_manager_mini.core.errors import MigrationError
from inventory_manager_mini.core.models import ItemRow, NewItem
from inventory_manager_mini.core.services import InventoryService
from inventory_manager_mini.core.timeutil import local_timestamp_for_filename
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, open_database
from inventory_manager_mini.ui import display_settings, single_instance
from inventory_manager_mini.ui.dialogs.low_stock_notice_dialog import LowStockNoticeDialog
from inventory_manager_mini.ui.main_window import ExitRequest, MainWindow
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
    assert shown_windows[0].context.reports is not None
    assert shown_windows[0].context.paths == paths
    lock = single_instance.SingleInstanceLock(paths.lock_path)
    assert lock.try_acquire()
    lock.release()


@pytest.mark.parametrize(
    ("file_state", "contents"),
    [
        ("missing", None),
        ("invalid-json", b"{not json"),
        ("invalid-utf8", b"\xff\xfe"),
        ("permission-error", b'{"font_scale_percent": 150}'),
    ],
)
def test_main_uses_default_display_settings_when_settings_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    qtbot,
    file_state: str,
    contents: bytes | None,
) -> None:
    import inventory_manager_mini.app as app_module

    paths = _paths(tmp_path)
    if contents is not None:
        paths.data_dir.mkdir(parents=True)
        paths.ui_settings_path.write_bytes(contents)
    applied_settings: list[display_settings.DisplaySettings] = []
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    monkeypatch.setattr(app_module, "apply_display_settings", applied_settings.append)
    if file_state == "permission-error":
        original_read_text = Path.read_text

        def deny_settings_read(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == paths.ui_settings_path:
                raise PermissionError("denied")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_settings_read)

    assert main(paths) == 0

    assert applied_settings == [display_settings.default_display_settings()]


def test_main_applies_saved_display_scale_before_showing_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot
) -> None:
    paths = _paths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    display_settings.save_display_settings(
        paths.ui_settings_path, display_settings.DisplaySettings(None, 150)
    )
    original_font = QFont(QApplication.font())
    monkeypatch.setattr(display_settings, "_base_font", QFont(original_font))
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)

    try:
        assert main(paths) == 0
        applied_font = QApplication.font()
        if original_font.pointSizeF() > 0:
            assert applied_font.pointSizeF() == pytest.approx(original_font.pointSizeF() * 1.5)
        else:
            assert applied_font.pixelSize() == round(original_font.pixelSize() * 1.5)
    finally:
        QApplication.setFont(original_font)


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


def _seed_database(paths: AppPaths, *, low_stock: int, inactive_low_stock: int = 0) -> None:
    ensure_dirs(paths)
    conn = open_database(
        paths.db_path, paths.backup_dir, backup_timestamp=local_timestamp_for_filename
    )
    try:
        conn.execute("INSERT INTO clients (id, name) VALUES (1, '総務')")
        conn.execute("INSERT INTO purchasers (id, name) VALUES (1, '本部')")
        conn.execute(
            "INSERT INTO categories (id, parent_id, name, code_prefix, next_seq) "
            "VALUES (1, NULL, '文具', 'ST', 1)"
        )
        inventory = InventoryService(conn)
        for index in range(low_stock + inactive_low_stock):
            item = inventory.create_item(
                NewItem(client_id=1, purchaser_id=1, name=f"品目{index}", category_id=1)
            )
            if index >= low_stock:
                inventory.deactivate_item(item.id)
    finally:
        conn.close()


@pytest.fixture
def notice_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_exec(dialog: LowStockNoticeDialog) -> int:
        parent = dialog.parent()
        calls.append(
            {
                "count": dialog.model.rowCount(),
                "window_visible": isinstance(parent, MainWindow) and parent.isVisible(),
            }
        )
        return 0

    monkeypatch.setattr(LowStockNoticeDialog, "exec", fake_exec)
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)
    return calls


@pytest.mark.parametrize("inactive_only", [False, True])
def test_main_does_not_show_notice_without_low_stock_items(
    tmp_path: Path, qtbot, notice_calls: list[dict[str, object]], inactive_only: bool
) -> None:
    paths = _paths(tmp_path)
    _seed_database(paths, low_stock=0, inactive_low_stock=2 if inactive_only else 0)

    assert main(paths) == 0

    assert notice_calls == []


def test_main_shows_notice_after_main_window_is_visible(
    tmp_path: Path, qtbot, notice_calls: list[dict[str, object]]
) -> None:
    paths = _paths(tmp_path)
    _seed_database(paths, low_stock=2, inactive_low_stock=1)

    assert main(paths) == 0

    assert notice_calls == [{"count": 2, "window_visible": True}]


def test_main_continues_when_low_stock_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    qtbot,
    notice_calls: list[dict[str, object]],
) -> None:
    paths = _paths(tmp_path)
    _seed_database(paths, low_stock=1)
    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    def fail(self: InventoryService) -> list[ItemRow]:
        raise RuntimeError("取得失敗")

    monkeypatch.setattr(InventoryService, "list_low_stock", fail)

    assert main(paths) == 0

    assert notice_calls == []
    assert len(messages) >= 1
    assert messages[0][1] == "エラー"


def test_build_restart_command_for_python_and_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    from inventory_manager_mini.app import build_restart_command

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert build_restart_command() == [sys.executable, "-m", "inventory_manager_mini"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert build_restart_command() == [sys.executable]


class _ExitRecorder:
    """exec を差し替えて終了要求をセットし、startDetached 呼び出し時の状態を記録する。"""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        paths: AppPaths,
        request: ExitRequest,
        start_result: tuple[bool, int],
    ) -> None:
        import inventory_manager_mini.app as app_module

        self.paths = paths
        self.events: list[str] = []
        self.calls: list[tuple[str, list[str]]] = []
        self.lock_acquirable = False
        self.conn_closed = False
        self.start_result = start_result
        self.conn: sqlite3.Connection | None = None
        windows: list[MainWindow] = []
        original_show = MainWindow.show
        original_open = app_module.open_database

        def open_and_capture(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            self.conn = original_open(*args, **kwargs)
            return self.conn

        def fake_exec(application: QApplication) -> int:
            windows[0].exit_request = request
            self.events.append("exec")
            return 0

        monkeypatch.setattr(app_module, "open_database", open_and_capture)
        monkeypatch.setattr(
            MainWindow, "show", lambda window: (windows.append(window), original_show(window))
        )
        monkeypatch.setattr(QApplication, "exec", fake_exec)
        monkeypatch.setattr(app_module.QProcess, "startDetached", self.start_detached)
        monkeypatch.setattr(sys, "excepthook", sys.excepthook)

    def start_detached(self, program: str, arguments: list[str]) -> tuple[bool, int]:
        self.events.append("startDetached")
        self.calls.append((program, arguments))
        assert self.conn is not None
        try:
            self.conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            self.conn_closed = True
        lock = single_instance.SingleInstanceLock(self.paths.lock_path)
        self.lock_acquirable = lock.try_acquire()
        if self.lock_acquirable:
            lock.release()
        return self.start_result


def test_main_restarts_after_closing_connection_and_releasing_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot
) -> None:
    paths = _paths(tmp_path)
    recorder = _ExitRecorder(monkeypatch, paths, ExitRequest.RESTART, (True, 12345))

    assert main(paths) == 0

    assert recorder.events == ["exec", "startDetached"]
    assert recorder.conn_closed
    assert recorder.lock_acquirable
    assert len(recorder.calls) == 1
    assert recorder.calls[0][0] == sys.executable


def test_main_reports_restart_failure_even_if_result_tuple_is_truthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot
) -> None:
    paths = _paths(tmp_path)
    recorder = _ExitRecorder(monkeypatch, paths, ExitRequest.RESTART, (False, 0))
    messages: list[tuple[object, ...]] = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: messages.append(args))

    assert main(paths) == 1

    assert recorder.events == ["exec", "startDetached"]
    assert len(messages) == 1
    assert "復元済み" in str(messages[0][2])
    assert "手動" in str(messages[0][2])


def test_main_does_not_restart_on_abnormal_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qtbot
) -> None:
    paths = _paths(tmp_path)
    recorder = _ExitRecorder(monkeypatch, paths, ExitRequest.ABNORMAL, (True, 1))

    assert main(paths) == 1

    assert recorder.events == ["exec"]
    lock = single_instance.SingleInstanceLock(paths.lock_path)
    assert lock.try_acquire()
    lock.release()
