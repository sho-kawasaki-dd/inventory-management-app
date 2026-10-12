import logging
from collections.abc import Sequence
from enum import Enum, auto
from importlib import import_module
from pathlib import Path

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QPageLayout
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.errors import InvalidBackupError, RestoreError
from inventory_manager_mini.core.models import REASON_LABELS, ItemFilter, Reason
from inventory_manager_mini.core.services import PreparedRestore, validate_purchase_url
from inventory_manager_mini.core.timeutil import format_local, utc_now_str
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.csv_export import export_csv_with_dialog
from inventory_manager_mini.ui.dialogs.settings_dialog import SettingsDialog
from inventory_manager_mini.ui.display_settings import load_display_settings
from inventory_manager_mini.ui.error_handling import run_guarded, show_unexpected_error
from inventory_manager_mini.ui.models.item_table_model import (
    ItemSortProxyModel,
    ItemTableModel,
)
from inventory_manager_mini.ui.printing.inventory_sheet_renderer import render_inventory_sheet
from inventory_manager_mini.ui.printing.label_print_dialog import LabelPrintDialog
from inventory_manager_mini.ui.printing.label_renderer import render_labels
from inventory_manager_mini.ui.printing.layout import (
    INVENTORY_SHEET_SPEC,
    LABEL_72212,
    assign_label_slots,
    paginate,
    summarize_item_filter,
)
from inventory_manager_mini.ui.printing.preview_dialog import PrintPreviewDialog
from inventory_manager_mini.ui.widgets.alert_panel import AlertPanel
from inventory_manager_mini.ui.widgets.category_picker import CategoryPicker
from inventory_manager_mini.ui.widgets.dashboard import DashboardWindow

logger = logging.getLogger(__name__)

MAX_REJECTION_REASONS = 20


class ExitRequest(Enum):
    NORMAL = auto()
    RESTART = auto()
    ABNORMAL = auto()


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self._displayed_item_filter: ItemFilter | None = None
        self._displayed_filter_names: (
            tuple[str | None, str | None, str | None, str | None] | None
        ) = None
        self.exit_request = ExitRequest.NORMAL
        self._dashboard_window: DashboardWindow | None = None
        self.setWindowTitle("Inventory Manager mini")
        self.resize(1000, 650)
        self.setMinimumSize(800, 600)
        self._build_ui()
        self.context.data_bus.data_changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        filters = QGridLayout()

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("品名・管理番号・メーカー型番")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_edit.textChanged.connect(lambda _text: self.search_timer.start())
        self.search_timer.timeout.connect(self.refresh_items)
        filters.addWidget(self.search_edit, 0, 0, 1, 4)

        self.client_combo = self._master_combo()
        self.purchaser_combo = self._master_combo()
        self.category_picker = CategoryPicker(self)
        self.category_picker.category_changed.connect(self.refresh_items)
        self.location_combo = self._master_combo()
        for combo in (self.client_combo, self.purchaser_combo, self.location_combo):
            combo.currentIndexChanged.connect(self.refresh_items)
        for index, (label, widget) in enumerate(
            (
                ("クライアント", self.client_combo),
                ("発注主体", self.purchaser_combo),
                ("カテゴリ", self.category_picker),
                ("保管場所", self.location_combo),
            )
        ):
            widget.setMinimumWidth(130)
            row = 1 + index // 2
            column = (index % 2) * 2
            filters.addWidget(QLabel(label, self), row, column)
            filters.addWidget(widget, row, column + 1)

        self.low_stock_checkbox = QCheckBox("低在庫のみ", self)
        self.low_stock_checkbox.toggled.connect(self.refresh_items)
        filters.addWidget(self.low_stock_checkbox, 3, 0)
        self.inactive_checkbox = QCheckBox("廃止品目を含む", self)
        self.inactive_checkbox.toggled.connect(self.refresh_items)
        filters.addWidget(self.inactive_checkbox, 3, 1)
        layout.addLayout(filters)

        self.item_model = ItemTableModel(self)
        self.sort_model = ItemSortProxyModel(self)
        self.sort_model.setSourceModel(self.item_model)
        self.table = QTableView(self)
        self.table.setModel(self.sort_model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.sort_model.set_item_selection_model(self.table.selectionModel())
        self.table.selectionModel().selectionChanged.connect(self._update_selection_actions)
        self.table.doubleClicked.connect(self._open_history_for_index)
        layout.addWidget(self.table)
        self.setCentralWidget(central)

        self.alert_panel = AlertPanel(self.context, self)
        self.alert_panel.item_activated.connect(self._show_item_from_alert)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.alert_panel)

        self._build_menus()
        self._build_toolbar()
        self._build_table_context_menu()
        self.statusBar()

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar("品目操作", self)
        self.toolbar.setMovable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(self.toolbar)
        self.toolbar.addAction(self.new_action)
        self.toolbar.addAction(self.edit_action)
        self.toolbar.addAction(self.toggle_active_action)
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.stock_actions[Reason.IN])
        self.toolbar.addAction(self.stock_actions[Reason.OUT])
        self.return_dispose_button = QToolButton(self.toolbar)
        self.return_dispose_button.setText("返品・廃棄")
        self.return_dispose_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.return_dispose_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.return_dispose_menu = QMenu(self.return_dispose_button)
        self.return_dispose_menu.addAction(self.stock_actions[Reason.RETURN])
        self.return_dispose_menu.addAction(self.stock_actions[Reason.DISPOSE])
        self.return_dispose_button.setMenu(self.return_dispose_menu)
        self.toolbar.addWidget(self.return_dispose_button)
        self.toolbar.addAction(self.stock_actions[Reason.ADJUST])
        self.toolbar.addSeparator()
        self.toolbar.addAction(self.history_action)
        self.toolbar.addAction(self.purchase_page_action)
        self.toolbar.addSeparator()
        self.csv_export_button = QToolButton(self.toolbar)
        self.csv_export_button.setText("CSV 出力")
        self.csv_export_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.csv_export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.csv_export_menu = QMenu(self.csv_export_button)
        self.csv_export_menu.addAction(self.export_items_action)
        self.csv_export_menu.addAction(self.export_history_action)
        self.csv_export_button.setMenu(self.csv_export_menu)
        self.toolbar.addWidget(self.csv_export_button)
        self.print_button = QToolButton(self.toolbar)
        self.print_button.setText("印刷")
        self.print_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.print_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.print_toolbar_menu = QMenu(self.print_button)
        self.print_toolbar_menu.addAction(self.print_labels_action)
        self.print_toolbar_menu.addAction(self.print_inventory_sheet_action)
        self.print_button.setMenu(self.print_toolbar_menu)
        self.toolbar.addWidget(self.print_button)

    def _build_table_context_menu(self) -> None:
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.table.addAction(self.new_action)
        self.table.addAction(self.edit_action)
        self.table.addAction(self.toggle_active_action)
        separator = QAction(self.table)
        separator.setSeparator(True)
        self.table.addAction(separator)
        for action in self.stock_actions.values():
            self.table.addAction(action)
        separator = QAction(self.table)
        separator.setSeparator(True)
        self.table.addAction(separator)
        self.table.addAction(self.history_action)
        self.table.addAction(self.purchase_page_action)
        separator = QAction(self.table)
        separator.setSeparator(True)
        self.table.addAction(separator)
        self.table.addAction(self.print_labels_action)

    @staticmethod
    def _master_combo() -> QComboBox:
        combo = QComboBox()
        combo.addItem("すべて", None)
        return combo

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル")
        self.export_items_action = QAction("在庫一覧", self)
        self.export_items_action.triggered.connect(self._export_items_csv)
        self.export_history_action = QAction("履歴(全品目)", self)
        self.export_history_action.triggered.connect(self._export_all_history_csv)
        self.csv_menu = file_menu.addMenu("CSV 出力")
        self.csv_menu.addAction(self.export_items_action)
        self.csv_menu.addAction(self.export_history_action)
        self.print_menu = file_menu.addMenu("印刷")
        self.print_labels_action = QAction("棚ラベル", self)
        self.print_labels_action.triggered.connect(self._print_labels)
        self.print_menu.addAction(self.print_labels_action)
        self.print_inventory_sheet_action = QAction("在庫表", self)
        self.print_inventory_sheet_action.triggered.connect(self._print_inventory_sheet)
        self.print_menu.addAction(self.print_inventory_sheet_action)
        file_menu.addSeparator()
        self.backup_action = QAction("バックアップ", self)
        self.backup_action.triggered.connect(self._create_backup)
        file_menu.addAction(self.backup_action)
        self.restore_action = QAction("復元", self)
        self.restore_action.triggered.connect(self._restore_backup)
        file_menu.addAction(self.restore_action)
        file_menu.addSeparator()
        file_menu.addAction("終了", self.close)

        item_menu = self.menuBar().addMenu("品目")
        self.new_action = QAction("新規", self)
        self.new_action.triggered.connect(self._create_item)
        item_menu.addAction(self.new_action)
        self.edit_action = QAction("編集", self)
        self.edit_action.triggered.connect(self._edit_item)
        item_menu.addAction(self.edit_action)
        self.toggle_active_action = QAction("廃止", self)
        self.toggle_active_action.triggered.connect(self._toggle_item_active)
        item_menu.addAction(self.toggle_active_action)
        item_menu.addSeparator()
        self.purchase_page_action = QAction("販売ページを開く", self)
        self.purchase_page_action.triggered.connect(self._open_purchase_page)
        item_menu.addAction(self.purchase_page_action)
        self.history_action = QAction("履歴", self)
        self.history_action.triggered.connect(self._open_history)
        item_menu.addAction(self.history_action)

        self.stock_menu = self.menuBar().addMenu("在庫")
        self.stock_actions: dict[Reason, QAction] = {}
        for reason, label in REASON_LABELS.items():
            action = QAction(label, self)
            action.triggered.connect(
                lambda _checked=False, value=reason: self._open_stock_move(value)
            )
            self.stock_actions[reason] = action
            self.stock_menu.addAction(action)

        master_menu = self.menuBar().addMenu("マスタ")
        master_tabs = (
            ("クライアント", "CLIENT"),
            ("発注主体", "PURCHASER"),
            ("担当者", "STAFF"),
            ("カテゴリ", "CATEGORY"),
            ("保管場所", "LOCATION"),
        )
        for label, tab_name in master_tabs:
            action = master_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=tab_name: self._open_master_dialog(value)
            )

        view_menu = self.menuBar().addMenu("表示")
        self.include_inactive_action = QAction("廃止品目を含む", self)
        self.include_inactive_action.setCheckable(True)
        self.include_inactive_action.toggled.connect(self.inactive_checkbox.setChecked)
        self.inactive_checkbox.toggled.connect(self.include_inactive_action.setChecked)
        view_menu.addAction(self.include_inactive_action)
        view_menu.addAction(self.alert_panel.toggleViewAction())
        self.dashboard_action = QAction("ダッシュボード", self)
        self.dashboard_action.triggered.connect(self._open_dashboard)
        view_menu.addAction(self.dashboard_action)

        tools_menu = self.menuBar().addMenu("ツール")
        self.settings_action = QAction("設定", self)
        self.settings_action.triggered.connect(self._open_settings_dialog)
        tools_menu.addAction(self.settings_action)

        help_menu = self.menuBar().addMenu("ヘルプ")
        help_menu.addAction("バージョン情報", self._show_about)
        self._update_selection_actions()

    def refresh(self) -> None:
        selected_item_ids = self._selected_item_ids()
        self._refresh_master_choices()
        self.refresh_items(selected_item_ids=selected_item_ids)
        self.alert_panel.refresh()
        self._update_selection_actions()

    def _refresh_master_choices(self) -> None:
        succeeded, choices = run_guarded(
            self,
            lambda: (
                self.context.master.list_clients(include_inactive=True),
                self.context.master.list_purchasers(include_inactive=True),
                self.context.master.list_categories(),
                self.context.master.list_locations(),
            ),
        )
        if not succeeded or choices is None:
            return
        clients, purchasers, categories, locations = choices
        self._fill_master_combo(
            self.client_combo,
            [(client.id, client.name, client.is_active) for client in clients],
        )
        self._fill_master_combo(
            self.purchaser_combo,
            [(purchaser.id, purchaser.name, purchaser.is_active) for purchaser in purchasers],
        )
        self.category_picker.set_categories(categories, leading_label="すべて")
        self._fill_master_combo(
            self.location_combo,
            [(location.id, location.name, True) for location in locations],
        )

    @staticmethod
    def _fill_master_combo(combo: QComboBox, values: list[tuple[int, str, bool]]) -> None:
        selected_id = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("すべて", None)
        for entity_id, name, is_active in values:
            label = name if is_active else f"{name} (無効)"
            combo.addItem(label, entity_id)
        index = combo.findData(selected_id)
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _current_item_filter(self) -> ItemFilter:
        return ItemFilter(
            text=self.search_edit.text().strip() or None,
            client_id=self.client_combo.currentData(),
            purchaser_id=self.purchaser_combo.currentData(),
            category_id=self.category_picker.current_category_id(),
            location_id=self.location_combo.currentData(),
            low_stock_only=self.low_stock_checkbox.isChecked(),
            include_inactive=self.inactive_checkbox.isChecked(),
        )

    def refresh_items(self, *_args, selected_item_ids: list[int] | None = None) -> None:
        item_filter = self._current_item_filter()
        filter_names = (
            self._combo_display_name(self.client_combo, item_filter.client_id),
            self._combo_display_name(self.purchaser_combo, item_filter.purchaser_id),
            self.category_picker.display.text() if item_filter.category_id is not None else None,
            self._combo_display_name(self.location_combo, item_filter.location_id),
        )
        if selected_item_ids is None:
            selected_item_ids = self._selected_item_ids()
        succeeded, rows = run_guarded(self, lambda: self.context.inventory.list_items(item_filter))
        if not succeeded or rows is None:
            return
        self.item_model.set_rows(rows)
        self._displayed_item_filter = item_filter
        self._displayed_filter_names = filter_names
        self._restore_item_selection(selected_item_ids)
        self.statusBar().showMessage(f"{len(rows):,} 件")
        self._update_selection_actions()

    @staticmethod
    def _combo_display_name(combo: QComboBox, entity_id: int | None) -> str | None:
        if entity_id is None:
            return None
        index = combo.findData(entity_id)
        return combo.itemText(index) if index >= 0 else None

    def _restore_item_selection(self, item_ids: list[int]) -> None:
        selection_model = self.table.selectionModel()
        proxy_indexes = []
        for item_id in item_ids:
            source_row = self.item_model.row_of(item_id)
            if source_row is None:
                continue
            proxy_index = self.sort_model.mapFromSource(self.item_model.index(source_row, 0))
            if proxy_index.isValid():
                proxy_indexes.append(proxy_index)
        if not proxy_indexes:
            selection_model.clearSelection()
            return
        selection = QItemSelection()
        for proxy_index in proxy_indexes:
            selection.select(proxy_index, proxy_index)
        selection_model.select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(
            proxy_indexes[-1], QItemSelectionModel.SelectionFlag.NoUpdate
        )

    def _selected_item_ids(self) -> list[int]:
        selected = sorted(self.table.selectionModel().selectedRows(), key=lambda index: index.row())
        item_ids = []
        for proxy_index in selected:
            source_index = self.sort_model.mapToSource(proxy_index)
            if source_index.isValid():
                item_ids.append(self.item_model.row_at(source_index.row()).id)
        return item_ids

    def _selected_item_id(self) -> int | None:
        selected_item_ids = self._selected_item_ids()
        if len(selected_item_ids) != 1:
            return None
        return selected_item_ids[0]

    def _update_selection_actions(self, *_args) -> None:
        selected_item_ids = self._selected_item_ids() if hasattr(self, "table") else []
        selected_id = selected_item_ids[0] if len(selected_item_ids) == 1 else None
        row = None if selected_id is None else self.item_model.row_of(selected_id)
        item = None if row is None else self.item_model.row_at(row)
        has_selection = item is not None
        self.edit_action.setEnabled(has_selection)
        self.toggle_active_action.setEnabled(has_selection)
        self.history_action.setEnabled(has_selection)
        self.purchase_page_action.setEnabled(
            item is not None and bool(item.purchase_url and item.purchase_url.strip())
        )
        can_move_stock = item is not None and item.is_active
        for action in self.stock_actions.values():
            action.setEnabled(can_move_stock)
        print_labels_action = getattr(self, "print_labels_action", None)
        if print_labels_action is not None:
            print_labels_action.setEnabled(bool(selected_item_ids))
        if item is not None:
            self.toggle_active_action.setText("廃止" if item.is_active else "再有効化")

    def _create_item(self) -> None:
        self._open_item_dialog()

    def _edit_item(self) -> None:
        item_id = self._selected_item_id()
        if item_id is not None:
            self._open_item_dialog(item_id)

    def _open_stock_move(self, reason: Reason) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        StockMoveDialog = import_module(
            "inventory_manager_mini.ui.dialogs.stock_move_dialog"
        ).StockMoveDialog
        dialog = StockMoveDialog(self.context, reason, item_id=item_id, parent=self)
        dialog.exec()

    def _open_history(self) -> None:
        item_id = self._selected_item_id()
        if item_id is not None:
            self._show_history(item_id)

    def _open_purchase_page(self) -> None:
        selected_id = self._selected_item_id()
        if selected_id is None:
            return
        row = self.item_model.row_of(selected_id)
        if row is None:
            return
        item = self.item_model.row_at(row)
        succeeded, url = run_guarded(self, lambda: validate_purchase_url(item.purchase_url))
        if succeeded and url is not None:
            QDesktopServices.openUrl(QUrl(url))

    def _print_labels(self) -> None:
        run_guarded(self, self._run_print_labels)

    def _print_inventory_sheet(self) -> None:
        run_guarded(self, self._run_print_inventory_sheet)

    def _run_print_inventory_sheet(self) -> None:
        if self._displayed_item_filter is None or self._displayed_filter_names is None:
            QMessageBox.information(self, "在庫表", "印刷する品目がありません")
            return

        rows = []
        for proxy_row in range(self.sort_model.rowCount()):
            source_index = self.sort_model.mapToSource(self.sort_model.index(proxy_row, 0))
            if source_index.isValid():
                rows.append(self.item_model.row_at(source_index.row()))
        if not rows:
            QMessageBox.information(self, "在庫表", "印刷する品目がありません")
            return

        filter_summary = summarize_item_filter(
            self._displayed_item_filter,
            client_name=self._displayed_filter_names[0],
            purchaser_name=self._displayed_filter_names[1],
            category_path=self._displayed_filter_names[2],
            location_name=self._displayed_filter_names[3],
        )
        printed_at_local = format_local(utc_now_str())
        total_pages = len(
            paginate(
                len(rows),
                INVENTORY_SHEET_SPEC.first_page_rows,
                INVENTORY_SHEET_SPEC.other_page_rows,
            )
        )
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)

        def render(device: QPrinter, page_numbers: Sequence[int] | None) -> None:
            render_inventory_sheet(
                device,
                rows,
                filter_summary,
                printed_at_local,
                INVENTORY_SHEET_SPEC,
                page_numbers=page_numbers,
            )

        preview = PrintPreviewDialog(
            printer,
            QPageLayout.Orientation.Landscape,
            total_pages,
            render,
            parent=self,
        )
        preview.exec()

    def _run_print_labels(self) -> None:
        settings = load_display_settings(self.context.paths.ui_settings_path)
        items = []
        for item_id in self._selected_item_ids():
            source_row = self.item_model.row_of(item_id)
            if source_row is not None:
                items.append(self.item_model.row_at(source_row))
        if not items:
            return

        dialog = LabelPrintDialog(items, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        placements = assign_label_slots(dialog.entries, dialog.first_page_slots)
        items_by_id = {item.id: item for item in items}
        total_pages = max(placement.page_number for placement in placements)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)

        def render(device: QPrinter, page_numbers: Sequence[int] | None) -> None:
            render_labels(
                device,
                placements,
                items_by_id,
                LABEL_72212,
                settings.label_offset_left_mm,
                settings.label_offset_top_mm,
                page_numbers=page_numbers,
            )

        preview = PrintPreviewDialog(
            printer,
            QPageLayout.Orientation.Portrait,
            total_pages,
            render,
            parent=self,
        )
        preview.exec()

    def _export_items_csv(self) -> None:
        item_filter = self._current_item_filter()
        path = export_csv_with_dialog(
            self,
            "在庫一覧",
            "在庫一覧",
            lambda destination: self.context.reports.export_items_csv(destination, item_filter),
        )
        if path is not None:
            self.statusBar().showMessage(f"在庫一覧を出力しました: {path}")

    def _export_all_history_csv(self) -> None:
        path = export_csv_with_dialog(
            self,
            "履歴(全品目)",
            "履歴",
            lambda destination: self.context.reports.export_history_csv(destination),
        )
        if path is not None:
            self.statusBar().showMessage(f"履歴を出力しました: {path}")

    def _create_backup(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "バックアップの保存先を選択", str(self.context.paths.backup_dir)
        )
        if not selected:
            return
        dest_dir = Path(selected)

        def run_backup() -> Path:
            logger.info("バックアップを開始します: 保存先=%s", dest_dir)
            try:
                path = self.context.backup.create_current_backup(dest_dir)
            except Exception:
                logger.error("バックアップに失敗しました: 保存先=%s", dest_dir, exc_info=True)
                raise
            logger.info("バックアップが完了しました: ファイル=%s", path)
            return path

        self.statusBar().showMessage("バックアップを作成しています...")
        self.statusBar().repaint()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            succeeded, path = run_guarded(self, run_backup)
        finally:
            QApplication.restoreOverrideCursor()
        if succeeded and path is not None:
            message = f"バックアップを作成しました: {path}"
            self.statusBar().showMessage(message)
            QMessageBox.information(self, "バックアップ", message)
        else:
            self.statusBar().clearMessage()

    def _restore_backup(self) -> None:
        selected, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "復元するバックアップを選択",
            str(self.context.paths.backup_dir),
            "データベース (*.db)",
        )
        if not selected:
            return
        source = Path(selected)
        prepared = self._prepare_restore(source)
        if prepared is None:
            return
        try:
            if self._confirm_restore(source, prepared):
                self._apply_restore(prepared)
        finally:
            prepared.close()

    def _prepare_restore(self, source: Path) -> PreparedRestore | None:
        rejection: InvalidBackupError | None = None

        def run_prepare() -> PreparedRestore | None:
            nonlocal rejection
            logger.info("復元の検査・準備を開始します: 復元元=%s", source)
            try:
                prepared = self.context.backup.prepare_restore(source)
            except InvalidBackupError as error:
                rejection = error
                logger.error("復元元を利用できません: 復元元=%s", source, exc_info=True)
                return None
            except Exception:
                logger.error("復元の検査・準備に失敗しました: 復元元=%s", source, exc_info=True)
                raise
            logger.info("復元の検査・準備が完了しました: 復元元=%s", source)
            return prepared

        self.statusBar().showMessage("復元元を検査しています...")
        self.statusBar().repaint()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            _succeeded, prepared = run_guarded(self, run_prepare)
        finally:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()
        if rejection is not None:
            reasons = list(rejection.reasons)
            message = "復元元を利用できないため、復元を中止しました。\n" + "\n".join(
                f"・{reason}" for reason in reasons[:MAX_REJECTION_REASONS]
            )
            if len(reasons) > MAX_REJECTION_REASONS:
                message += f"\n他 {len(reasons) - MAX_REJECTION_REASONS} 件"
            QMessageBox.warning(self, "復元できません", message)
        return prepared

    def _confirm_restore(self, source: Path, prepared: PreparedRestore) -> bool:
        version_text = f"{prepared.source_version}"
        if prepared.source_version < self.context.schema_version:
            version_text += "(最新版へ移行済み)"
        last_moved_at = (
            "なし" if prepared.last_moved_at is None else format_local(prepared.last_moved_at)
        )
        message = (
            f"復元元: {source}\n"
            f"元の版数: {version_text}\n"
            f"品目数: {prepared.item_count:,} 件\n"
            f"履歴数: {prepared.movement_count:,} 件\n"
            f"最終履歴日時: {last_moved_at}\n\n"
            "現在のデータは復元元の内容で置き換えられます。\n"
            "復元前に現在のデータを自動バックアップします"
            f"(保存先: {self.context.paths.backup_dir})。\n"
            "完了後にアプリを再起動します。\n\n"
            "復元しますか？"
        )
        answer = QMessageBox.question(
            self,
            "復元の確認",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _apply_restore(self, prepared: PreparedRestore) -> None:
        self._shutdown_dashboard()
        paths = self.context.paths
        restore_error: RestoreError | None = None
        unexpected_error: Exception | None = None
        pre_restore_path: Path | None = None

        self.setEnabled(False)
        self.statusBar().showMessage("復元しています...")
        self.repaint()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        logger.info("復元の上書きを開始します: 復元元=%s 復元先=%s", prepared.path, paths.db_path)
        try:
            pre_restore_path = self.context.backup.apply_restore(
                prepared, paths.db_path, paths.backup_dir
            )
        except RestoreError as error:
            restore_error = error
            logger.error(
                "復元に失敗しました: 段階=%s 復旧=%s 自動バックアップ=%s",
                error.stage,
                error.recovered,
                error.backup_path,
                exc_info=True,
            )
        except Exception as error:
            unexpected_error = error
        finally:
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()

        if unexpected_error is not None:
            # 現行 DB の状態が不明なため、DB 操作を再開しない
            show_unexpected_error(None, unexpected_error)
            self._request_exit(ExitRequest.ABNORMAL)
        elif restore_error is not None:
            self._handle_restore_error(restore_error)
        else:
            logger.info("復元が完了しました: 復元前バックアップ=%s", pre_restore_path)
            self.setEnabled(True)
            QMessageBox.information(
                self,
                "復元",
                f"復元が完了しました。再起動します。\n復元前のバックアップ: {pre_restore_path}",
            )
            self._request_exit(ExitRequest.RESTART)

    def _handle_restore_error(self, error: RestoreError) -> None:
        if error.stage == "recovery":
            # 無効化したウィンドウを親にするとダイアログを操作できないため親なしで表示する
            QMessageBox.critical(
                None,
                "復元に失敗しました",
                f"{error.message}\n\n"
                f"自動バックアップ: {error.backup_path}\n"
                f"ログ: {self.context.paths.log_path}\n\n"
                "アプリを終了します。アプリの全プロセスが終了したことを確認し、"
                "現行のデータベースと付随する -wal・-shm ファイルを別の場所へ退避してから、"
                "自動バックアップを現行のデータベースのパスへ配置して再起動してください。"
                "退避したファイルと自動バックアップは、復旧を確認するまで削除しないでください。",
            )
            self._request_exit(ExitRequest.ABNORMAL)
            return

        self.setEnabled(True)
        if error.stage == "pre_backup":
            QMessageBox.warning(
                self,
                "復元に失敗しました",
                f"{error.message}\n保存先: {self.context.paths.backup_dir}\n"
                "現在のデータは変更されていません。",
            )
            return
        logger.info("現行データベースを自動復旧しました: 自動バックアップ=%s", error.backup_path)
        QMessageBox.warning(
            self,
            "復元に失敗しました",
            f"{error.message}\n自動バックアップ: {error.backup_path}\n復元前の状態に戻しました。",
        )
        self.refresh()

    def _request_exit(self, request: ExitRequest) -> None:
        self.exit_request = request
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._shutdown_dashboard()
        super().closeEvent(event)

    def _open_dashboard(self) -> None:
        if self._dashboard_window is None:
            self._dashboard_window = DashboardWindow(self.context, self)
        self._dashboard_window.show()
        self._dashboard_window.raise_()
        self._dashboard_window.activateWindow()

    def _shutdown_dashboard(self) -> None:
        if self._dashboard_window is None:
            return
        dashboard = self._dashboard_window
        self._dashboard_window = None
        dashboard.shutdown()

    def _open_history_for_index(self, index) -> None:
        if not index.isValid():
            return
        selection_model = self.table.selectionModel()
        selection_model.select(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
        source_index = self.sort_model.mapToSource(index)
        self._show_history(self.item_model.row_at(source_index.row()).id)

    def _show_history(self, item_id: int) -> None:
        HistoryDialog = import_module(
            "inventory_manager_mini.ui.dialogs.history_dialog"
        ).HistoryDialog
        dialog = HistoryDialog(self.context, item_id, parent=self)
        dialog.exec()

    def _open_item_dialog(self, item_id: int | None = None) -> None:
        ItemDialog = import_module("inventory_manager_mini.ui.dialogs.item_dialog").ItemDialog
        dialog = ItemDialog(self.context, item_id=item_id, parent=self)
        dialog.exec()

    def _show_item_from_alert(self, item_id: int) -> None:
        self.search_timer.stop()
        widgets = (
            self.search_edit,
            self.client_combo,
            self.purchaser_combo,
            self.category_picker,
            self.location_combo,
            self.low_stock_checkbox,
            self.inactive_checkbox,
            self.include_inactive_action,
        )
        blockers = [QSignalBlocker(widget) for widget in widgets]
        self.search_edit.clear()
        self.client_combo.setCurrentIndex(0)
        self.purchaser_combo.setCurrentIndex(0)
        self.category_picker.set_current_category_id(None)
        self.location_combo.setCurrentIndex(0)
        self.low_stock_checkbox.setChecked(False)
        self.inactive_checkbox.setChecked(False)
        self.include_inactive_action.setChecked(False)
        for blocker in blockers:
            blocker.unblock()
        self.refresh_items()
        source_row = self.item_model.row_of(item_id)
        if source_row is None:
            return
        proxy_index = self.sort_model.mapFromSource(self.item_model.index(source_row, 0))
        selection_model = self.table.selectionModel()
        selection_model.select(
            proxy_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection_model.setCurrentIndex(proxy_index, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.table.scrollTo(proxy_index)

    def _open_master_dialog(self, tab_name: str) -> None:
        master_dialog_module = import_module("inventory_manager_mini.ui.dialogs.master_dialog")
        dialog = master_dialog_module.MasterDialog(
            self.context,
            initial_tab=master_dialog_module.MasterTab(tab_name.lower()),
            parent=self,
        )
        dialog.exec()

    def _open_settings_dialog(self) -> None:
        current = load_display_settings(self.context.paths.ui_settings_path)
        dialog = SettingsDialog(self.context, current, parent=self)
        dialog.exec()

    def _toggle_item_active(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        source_row = self.item_model.row_of(item_id)
        if source_row is None:
            return
        item = self.item_model.row_at(source_row)
        if item.is_active:
            detail = f"品目「{item.name}」を廃止しますか？"
            if item.quantity > 0:
                detail += f"\n現在の在庫数量: {item.quantity:,} 個"
            answer = QMessageBox.question(self, "品目の廃止", detail)
            if answer != QMessageBox.StandardButton.Yes:
                return
            succeeded, _result = run_guarded(
                self, lambda: self.context.inventory.deactivate_item(item_id)
            )
        else:
            succeeded, _result = run_guarded(
                self, lambda: self.context.inventory.reactivate_item(item_id)
            )
        if succeeded:
            self.context.data_bus.data_changed.emit()

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "バージョン情報",
            f"Inventory Manager mini {self.context.app_version}\n"
            f"スキーマ版: {self.context.schema_version}\nDB: {self.context.db_path}",
        )
