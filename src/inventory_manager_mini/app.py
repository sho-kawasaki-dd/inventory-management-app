from __future__ import annotations

import logging
import sqlite3
from importlib.metadata import PackageNotFoundError, version
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from inventory_manager_mini import __version__
from inventory_manager_mini.config import APP_NAME, AppPaths, ensure_dirs, resolve_paths
from inventory_manager_mini.core.errors import (
    MigrationError,
    SchemaTooNewError,
    UnsupportedSchemaError,
)
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.core.timeutil import local_timestamp_for_filename
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, open_database
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.low_stock_notice_dialog import LowStockNoticeDialog
from inventory_manager_mini.ui.error_handling import (
    install_excepthook,
    run_guarded,
    show_unexpected_error,
)
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.ui.signals import DataBus
from inventory_manager_mini.ui.single_instance import SingleInstanceLock

logger = logging.getLogger(__name__)


def setup_logging(log_path: Path) -> None:
    root_logger = logging.getLogger()
    target_path = str(log_path.resolve())
    if not any(
        isinstance(handler, RotatingFileHandler)
        and str(Path(handler.baseFilename).resolve()) == target_path
        for handler in root_logger.handlers
    ):
        root_logger.addHandler(
            RotatingFileHandler(
                log_path,
                maxBytes=1_000_000,
                backupCount=5,
                encoding="utf-8",
            )
        )
    root_logger.setLevel(logging.INFO)


def build_context(conn: sqlite3.Connection, paths: AppPaths) -> AppContext:
    try:
        app_version = version("inventory-manager-mini")
    except PackageNotFoundError:
        app_version = __version__
    return AppContext(
        inventory=InventoryService(conn),
        master=MasterService(conn),
        settings=SettingsService(conn),
        data_bus=DataBus(),
        db_path=paths.db_path,
        schema_version=SCHEMA_VERSION,
        app_version=app_version,
    )


def show_startup_notifications(context: AppContext, window: MainWindow) -> None:
    succeeded, rows = run_guarded(window, context.inventory.list_low_stock)
    if succeeded and rows:
        LowStockNoticeDialog(rows, window).exec()


def _show_database_error(
    error: SchemaTooNewError | UnsupportedSchemaError | MigrationError,
) -> None:
    message = error.message
    if isinstance(error, MigrationError) and error.backup_path is not None:
        message += f"\nバックアップ: {error.backup_path}"
    QMessageBox.critical(None, "データベースを開けません", message)


def main(paths: AppPaths | None = None) -> int:
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    application.setApplicationName(APP_NAME)

    resolved_paths = resolve_paths() if paths is None else paths
    ensure_dirs(resolved_paths)
    lock = SingleInstanceLock(resolved_paths.lock_path)
    if not lock.try_acquire():
        QMessageBox.information(None, APP_NAME, "既に起動しています")
        return 0

    conn: sqlite3.Connection | None = None
    try:
        setup_logging(resolved_paths.log_path)
        install_excepthook(resolved_paths.log_path)
        logger.info("アプリを起動します: version=%s db=%s", __version__, resolved_paths.db_path)
        try:
            conn = open_database(
                resolved_paths.db_path,
                resolved_paths.backup_dir,
                backup_timestamp=local_timestamp_for_filename,
            )
        except (SchemaTooNewError, UnsupportedSchemaError, MigrationError) as error:
            _show_database_error(error)
            return 1
        except Exception as error:
            show_unexpected_error(None, error)
            return 1

        context = build_context(conn, resolved_paths)
        window = MainWindow(context)
        window.show()
        show_startup_notifications(context, window)
        return application.exec()
    except Exception as error:
        show_unexpected_error(None, error)
        return 1
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            logger.exception("データベース接続を閉じられませんでした")
        finally:
            lock.release()
