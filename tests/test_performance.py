from __future__ import annotations

import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from argparse import Namespace
from collections.abc import Callable, Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QPaintEvent
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from inventory_manager_mini.core.models import PeriodKind, Reason
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.core.timeutil import local_timestamp_for_filename, utc_now_str
from inventory_manager_mini.db import migrations
from inventory_manager_mini.db.connection import connect, transaction
from inventory_manager_mini.db.integrity import compute_aggregates
from inventory_manager_mini.db.migrations import SCHEMA_VERSION, open_database
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.history_dialog import HistoryDialog
from inventory_manager_mini.ui.dialogs.reversal_dialog import ReversalDialog
from inventory_manager_mini.ui.dialogs.stock_move_dialog import StockMoveDialog
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.ui.signals import DataBus
from scripts.generate_dummy_data import generate_database

pytestmark = pytest.mark.perf


@pytest.fixture(scope="module")
def performance_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data_dir = tmp_path_factory.mktemp("performance-data")
    db_path = data_dir / "inventory.db"
    generate_database(
        Namespace(
            db=db_path,
            items=5000,
            movements=100000,
            years=3,
            seed=0,
            force=False,
        )
    )
    return db_path


class PaintProbe(QObject):
    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._generation = 0
        self._scheduled = False
        self.completed_at: float | None = None
        self.required_region: QRect | None = None

    def arm(self) -> None:
        self._generation += 1
        self._scheduled = False
        self.completed_at = None

    def require_paint_in(self, region: QRect) -> None:
        self.required_region = region

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.Paint
            and isinstance(event, QPaintEvent)
            and self.completed_at is None
            and not self._scheduled
            and (self.required_region is None or event.region().intersects(self.required_region))
        ):
            self._scheduled = True
            generation = self._generation
            QTimer.singleShot(0, lambda: self._complete_paint(generation))
        return super().eventFilter(watched, event)

    def _complete_paint(self, generation: int) -> None:
        if generation == self._generation:
            self.completed_at = time.perf_counter()


@pytest.fixture
def performance_window(qtbot, performance_db: Path) -> Iterator[tuple[MainWindow, PaintProbe]]:
    conn = open_database(
        performance_db,
        performance_db.parent,
        backup_timestamp=local_timestamp_for_filename,
    )
    context = AppContext(
        inventory=InventoryService(conn),
        master=MasterService(conn),
        settings=SettingsService(conn),
        data_bus=DataBus(),
        db_path=performance_db,
        schema_version=SCHEMA_VERSION,
        app_version="0.1.0",
    )
    window = MainWindow(context)
    probe = PaintProbe(window)
    window.table.viewport().installEventFilter(probe)
    qtbot.addWidget(window)
    window.show()
    probe.arm()
    window.table.viewport().update()
    qtbot.waitUntil(lambda: probe.completed_at is not None, timeout=5000)
    yield window, probe
    conn.close()


def _wait_for_paint(qtbot, probe: PaintProbe) -> float:
    qtbot.waitUntil(lambda: probe.completed_at is not None, timeout=5000)
    assert probe.completed_at is not None
    return probe.completed_at


def _measure_action(qtbot, probe: PaintProbe, action: Callable[[], None]) -> float:
    probe.arm()
    started_at = time.perf_counter()
    action()
    completed_at = _wait_for_paint(qtbot, probe)
    return completed_at - started_at


def _run_startup_probe(data_dir: Path) -> float:
    probe_script = r"""
import sys
from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication
from inventory_manager_mini.ui.main_window import MainWindow
from inventory_manager_mini.app import main

class FirstPaint(QObject):
    sent = False
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Paint and not self.sent:
            self.sent = True
            QTimer.singleShot(0, self.report)
        return False
    def report(self):
        print("FIRST_PAINT", flush=True)
        QApplication.instance().quit()

original_show = MainWindow.show
def show_with_probe(window):
    window._first_paint_probe = FirstPaint(window)
    window.table.viewport().installEventFilter(window._first_paint_probe)
    original_show(window)
MainWindow.show = show_with_probe
raise SystemExit(main())
"""
    environment = os.environ.copy()
    environment["INVENTORY_MANAGER_MINI_DATA_DIR"] = str(data_dir)
    process = subprocess.Popen(
        [sys.executable, "-c", probe_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    stdout = process.stdout
    output_queue: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: output_queue.put(stdout.readline()),
        daemon=True,
    )
    started_at = time.perf_counter()
    reader.start()
    try:
        try:
            output = output_queue.get(timeout=15)
        except queue.Empty as error:
            process.terminate()
            process.wait(timeout=5)
            assert process.stderr is not None
            stderr = process.stderr.read()
            raise AssertionError(f"起動子プロセスが通知しませんでした: {stderr}") from error
        received_at = time.perf_counter()
        assert output.strip() == "FIRST_PAINT", "初回描画完了通知を受信できませんでした"
        assert process.wait(timeout=5) == 0
        return received_at - started_at
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_startup_to_first_table_paint(performance_db: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "startup-data"
    data_dir.mkdir()
    shutil.copy2(performance_db, data_dir / "inventory.db")

    elapsed = _run_startup_probe(data_dir)
    print(f"別プロセス起動から一覧初回描画: {elapsed:.3f} 秒")
    assert elapsed <= 3.0


def test_service_outflow_save_with_large_history(performance_db: Path) -> None:
    conn = connect(performance_db)
    try:
        service = InventoryService(conn)
        assert conn.execute("SELECT quantity FROM items WHERE id = 1").fetchone()[0] > 0
        started_at = time.perf_counter()
        movement = service.issue(1, 1, 1, "性能計測")
        elapsed = time.perf_counter() - started_at
        assert movement.cost_amount is not None
        print(f"品目 5,000・履歴 100,000 件で出庫保存: {elapsed:.3f} 秒")
        assert elapsed <= 0.5
    finally:
        conn.close()


def test_service_outflow_save_with_many_lots(performance_db: Path) -> None:
    conn = connect(performance_db)
    try:
        service = InventoryService(conn)
        item_id = 1
        item = conn.execute(
            "SELECT quantity, client_id, purchaser_id FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if item[0]:
            service.issue(item_id, 1, item[0], "ロット性能計測の初期在庫整理")

        moved_at = utc_now_str()
        with transaction(conn):
            conn.executemany(
                "INSERT INTO stock_movements (item_id, client_id, purchaser_id, staff_id, "
                "reason, delta, unit_price, moved_at) VALUES (?, ?, ?, 1, 'in', 1, 1, ?)",
                [(item_id, item[1], item[2], moved_at)] * 1000,
            )
            conn.execute("UPDATE items SET quantity = 1000 WHERE id = ?", (item_id,))
            conn.execute(
                "UPDATE total_aggregates SET inbound_quantity = inbound_quantity + 1000 "
                "WHERE id = 1"
            )

        started_at = time.perf_counter()
        movement = service.issue(item_id, 1, 1000, "1,000 ロット性能計測")
        elapsed = time.perf_counter() - started_at
        allocation_count = conn.execute(
            "SELECT COUNT(*) FROM stock_allocations WHERE movement_id = ?", (movement.id,)
        ).fetchone()[0]
        assert movement.cost_amount == 1000
        assert allocation_count == 1000
        print(f"1 品目・1,000 ロットの出庫保存: {elapsed:.3f} 秒")
        assert elapsed <= 0.5
    finally:
        conn.close()


def test_report_annual_and_monthly_aggregation_with_large_history(
    performance_db: Path,
) -> None:
    conn = connect(performance_db)
    try:
        reports = ReportService(conn)
        fiscal_year = reports.current_fiscal_year()
        results: list[tuple[str, float]] = []
        for label, kind in (
            ("年次", PeriodKind.ANNUAL),
            ("月次", PeriodKind.MONTHLY),
        ):
            started_at = time.perf_counter()
            reports.dashboard(fiscal_year, kind)
            elapsed = time.perf_counter() - started_at
            results.append((label, elapsed))
            assert elapsed <= 2.0, f"{label}集計に {elapsed:.3f} 秒かかりました"
        print(
            "大規模履歴のレポート: "
            + "、".join(f"{label} {elapsed:.3f} 秒" for label, elapsed in results)
        )
    finally:
        conn.close()


def test_v2_to_v4_migration_with_large_history(performance_db: Path, tmp_path: Path) -> None:
    source = sqlite3.connect(performance_db)
    legacy_path = tmp_path / "legacy-v2.db"
    legacy = sqlite3.connect(legacy_path, autocommit=True)
    try:
        legacy.executescript(migrations._SCHEMA_V1_DDL + "\n" + migrations._MIGRATION_V2_SQL)
        with transaction(legacy):
            for table in (
                "clients",
                "purchasers",
                "staff",
                "categories",
                "locations",
                "items",
                "stock_movements",
            ):
                if table == "stock_movements":
                    rows = source.execute(
                        "SELECT id, item_id, client_id, purchaser_id, staff_id, reason, delta, "
                        "unit_price, used_for, reversal_of, note, moved_at "
                        "FROM stock_movements"
                    ).fetchall()
                else:
                    rows = source.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    placeholders = ", ".join("?" for _ in rows[0])
                    legacy.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            legacy.execute("PRAGMA user_version = 2")
    finally:
        source.close()
        legacy.close()

    started_at = time.perf_counter()
    conn = open_database(
        legacy_path,
        tmp_path / "migration-backups",
        backup_timestamp=lambda: "20261009_120000",
    )
    elapsed = time.perf_counter() - started_at
    try:
        assert conn.execute("SELECT COUNT(*) FROM total_aggregates").fetchone() == (1,)
    finally:
        conn.close()
    print(f"履歴 100,000 件の v2→v4 移行(事前検査・バックアップ・FIFO 再生): {elapsed:.3f} 秒")


def test_v4_migration_with_large_history(performance_db: Path, tmp_path: Path) -> None:
    source = connect(performance_db)
    legacy_path = tmp_path / "legacy-v3.db"
    legacy = sqlite3.connect(legacy_path, autocommit=True)
    try:
        schema_v3 = files("inventory_manager_mini.db").joinpath("schema_v3.sql")
        legacy.executescript(schema_v3.read_text(encoding="utf-8"))
        with transaction(legacy):
            for table in (
                "clients",
                "purchasers",
                "staff",
                "categories",
                "locations",
                "items",
            ):
                rows = source.execute(f"SELECT * FROM {table}").fetchall()
                if rows:
                    placeholders = ", ".join("?" for _ in rows[0])
                    legacy.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            movement_rows = source.execute(
                "SELECT id, item_id, client_id, purchaser_id, staff_id, reason, delta, "
                "unit_price, used_for, reversal_of, note, moved_at FROM stock_movements"
            ).fetchall()
            legacy.executemany(
                "INSERT INTO stock_movements (id, item_id, client_id, purchaser_id, staff_id, "
                "reason, delta, unit_price, used_for, reversal_of, note, moved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                movement_rows,
            )
            aggregates = compute_aggregates(legacy, version=3)
            legacy.execute(
                "UPDATE total_aggregates SET inbound_quantity = ?, outbound_quantity = ?, "
                "disposed_quantity = ?, expenditure = ?, disposal_amount = ? WHERE id = 1",
                tuple(
                    aggregates[column]
                    for column in (
                        "inbound_quantity",
                        "outbound_quantity",
                        "disposed_quantity",
                        "expenditure",
                        "disposal_amount",
                    )
                ),
            )
            legacy.execute("PRAGMA user_version = 3")
    finally:
        source.close()
        legacy.close()

    started_at = time.perf_counter()
    conn = open_database(
        legacy_path,
        tmp_path / "v4-migration-backups",
        backup_timestamp=lambda: "20261010_120000",
    )
    elapsed = time.perf_counter() - started_at
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (4,)
        assert conn.execute("SELECT COUNT(*) FROM stock_allocations").fetchone()[0] > 0
    finally:
        conn.close()
    print(f"履歴 100,000 件の v3→v4 移行(事前検査・バックアップ・FIFO 再生): {elapsed:.3f} 秒")


def test_search_to_table_paint(qtbot, monkeypatch, performance_window) -> None:
    window, probe = performance_window
    original_list_items = window.context.inventory.list_items
    query_started: list[float] = []

    def track_search(item_filter):
        if item_filter.text == "備品000001":
            query_started.append(time.perf_counter())
        return original_list_items(item_filter)

    monkeypatch.setattr(window.context.inventory, "list_items", track_search)
    window.item_model.modelReset.connect(probe.arm)
    probe.arm()
    input_started = time.perf_counter()
    window.search_edit.setText("備品000001")
    qtbot.waitUntil(lambda: len(query_started) == 1, timeout=2000)
    painted_at = _wait_for_paint(qtbot, probe)

    assert len(query_started) == 1
    assert window.item_model.rowCount() == 1
    processing_elapsed = painted_at - query_started[0]
    input_elapsed = painted_at - input_started
    print(
        "検索: 処理開始から描画 "
        f"{processing_elapsed:.3f} 秒、最終入力から描画 {input_elapsed:.3f} 秒"
    )
    assert processing_elapsed <= 0.3
    assert input_elapsed <= 0.6


def test_filter_changes_to_table_paint(qtbot, performance_window) -> None:
    window, probe = performance_window

    def select_category() -> None:
        window.category_picker.set_current_category_id(1)
        window.category_picker.category_changed.emit(1)

    actions: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "クライアント",
            lambda: window.client_combo.setCurrentIndex(window.client_combo.findData(1)),
        ),
        (
            "発注主体",
            lambda: window.purchaser_combo.setCurrentIndex(window.purchaser_combo.findData(1)),
        ),
        (
            "カテゴリ(子孫含む)",
            select_category,
        ),
        (
            "保管場所",
            lambda: window.location_combo.setCurrentIndex(window.location_combo.findData(1)),
        ),
        ("低在庫のみ", lambda: window.low_stock_checkbox.setChecked(True)),
        ("廃止品目を含む", lambda: window.inactive_checkbox.setChecked(True)),
    )
    results: list[tuple[str, float]] = []
    for label, action in actions:
        elapsed = _measure_action(qtbot, probe, action)
        results.append((label, elapsed))
        assert elapsed <= 0.3, f"{label} の変更から描画まで {elapsed:.3f} 秒"
    print("絞り込み: " + "、".join(f"{label} {elapsed:.3f} 秒" for label, elapsed in results))


def test_sort_changes_to_table_paint(qtbot, performance_window) -> None:
    window, probe = performance_window
    results: list[tuple[str, float]] = []
    for label, column in (("数量", 7), ("品名", 1)):
        probe.arm()
        started_at = time.perf_counter()
        window.table.sortByColumn(column, window.sort_model.sortOrder())
        sort_returned_at = time.perf_counter()
        painted_at = _wait_for_paint(qtbot, probe)
        elapsed = painted_at - started_at
        print(
            f"{label} ソート呼び出し {sort_returned_at - started_at:.3f} 秒、"
            f"描画待ち {painted_at - sort_returned_at:.3f} 秒"
        )
        results.append((label, elapsed))
        assert elapsed <= 0.3, f"{label} のソートから描画まで {elapsed:.3f} 秒"
    print("ソート: " + "、".join(f"{label} {elapsed:.3f} 秒" for label, elapsed in results))


def test_inbound_save_to_table_paint(qtbot, performance_window) -> None:
    window, probe = performance_window
    item_id = 1
    source_row = window.item_model.row_of(item_id)
    assert source_row is not None
    item = window.item_model.row_at(source_row)
    assert item.reference_price is not None
    proxy_index = window.sort_model.mapFromSource(window.item_model.index(source_row, 0))
    window.table.selectRow(proxy_index.row())

    dialog = StockMoveDialog(window.context, Reason.IN, item_id=item_id, parent=window)
    qtbot.addWidget(dialog)
    dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(1))
    dialog.quantity_spin.setValue(1)
    dialog.unit_price_edit.setText(str(item.reference_price))
    dialog.show()
    qtbot.waitUntil(dialog.isVisible, timeout=2000)
    ok_button = dialog.button_box.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_button.isEnabled()

    window.item_model.modelReset.connect(probe.arm)
    probe.arm()
    started_at = time.perf_counter()
    qtbot.mouseClick(ok_button, Qt.MouseButton.LeftButton)
    painted_at = _wait_for_paint(qtbot, probe)
    elapsed = painted_at - started_at

    refreshed_row = window.item_model.row_of(item_id)
    assert refreshed_row is not None
    assert window.item_model.row_at(refreshed_row).quantity == item.quantity + 1
    assert window._selected_item_id() == item_id
    print(f"入庫の保存から一覧再描画: {elapsed:.3f} 秒")
    assert elapsed <= 0.5, f"入庫の保存から一覧再描画まで {elapsed:.3f} 秒かかりました"


def test_reversal_save_to_table_paint(qtbot, performance_window, monkeypatch) -> None:
    window, probe = performance_window
    item_id = 1
    original_row = window.item_model.row_of(item_id)
    assert original_row is not None
    original_item = window.item_model.row_at(original_row)
    movement = window.context.inventory.receive(item_id, 1, 1)
    window.refresh()
    source_row = window.item_model.row_of(item_id)
    assert source_row is not None
    proxy_index = window.sort_model.mapFromSource(window.item_model.index(source_row, 0))
    window.table.selectRow(proxy_index.row())

    history = HistoryDialog(window.context, item_id, window)
    qtbot.addWidget(history)
    available = window.screen().availableGeometry()
    history.resize(min(600, available.width() - 32), min(460, available.height() - 64))
    history.move(
        window.frameGeometry().right() - history.width() + 1,
        window.frameGeometry().bottom() - history.height() + 1,
    )
    history.setModal(True)
    history.show()
    qtbot.waitUntil(history.isVisible, timeout=2000)
    movement_row = history.model.row_of(movement.id)
    assert movement_row is not None
    history.table.selectRow(movement_row)
    assert history.reverse_button.isEnabled()
    history_row_count = history.model.rowCount()

    started_at: list[float] = []

    def execute_reversal(dialog: ReversalDialog) -> int:
        dialog.staff_combo.setCurrentIndex(dialog.staff_combo.findData(1))
        dialog.resize(min(520, available.width() - 32), min(420, available.height() - 64))
        dialog.move(
            window.frameGeometry().right() - dialog.width() + 1,
            window.frameGeometry().bottom() - dialog.height() + 1,
        )

        def submit() -> None:
            viewport = window.table.viewport()
            viewport_global = QRect(viewport.mapToGlobal(QPoint(0, 0)), viewport.size())
            left_edge = viewport_global.right() + 1
            for overlay in (history, dialog):
                overlay_rect = overlay.frameGeometry()
                if viewport_global.intersects(overlay_rect):
                    left_edge = min(left_edge, overlay_rect.left())
            exposed_width = left_edge - viewport_global.left()
            assert exposed_width >= 100, "一覧 viewport に非被覆領域がありません"
            probe.require_paint_in(QRect(0, 0, exposed_width, viewport.height()))
            probe.arm()
            started_at.append(time.perf_counter())
            qtbot.mouseClick(
                dialog.button_box.button(QDialogButtonBox.StandardButton.Ok),
                Qt.MouseButton.LeftButton,
            )

        QTimer.singleShot(0, submit)
        return QDialog.exec(dialog)

    monkeypatch.setattr(ReversalDialog, "exec", execute_reversal)
    window.item_model.modelReset.connect(probe.arm)
    history._open_reversal()
    assert started_at
    painted_at = _wait_for_paint(qtbot, probe)
    elapsed = painted_at - started_at[0]

    refreshed_row = window.item_model.row_of(item_id)
    assert refreshed_row is not None
    assert window.item_model.row_at(refreshed_row).quantity == original_item.quantity
    assert window._selected_item_id() == item_id
    assert history.isVisible()
    assert history.model.rowCount() == history_row_count + 1
    selected_row = history.table.currentIndex().row()
    assert history.model.row_at(selected_row).id == movement.id
    assert history.model.row_at(selected_row).is_reversed
    print(f"取り消しの保存から一覧再描画: {elapsed:.3f} 秒")
    assert elapsed <= 0.5, f"取り消しの保存から一覧再描画まで {elapsed:.3f} 秒かかりました"
