from importlib import import_module

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import ItemFilter
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.error_handling import run_guarded
from inventory_manager_mini.ui.models.item_table_model import (
    ItemSortProxyModel,
    ItemTableModel,
)
from inventory_manager_mini.ui.widgets.category_picker import CategoryPicker


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
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
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.sort_model.set_item_selection_model(self.table.selectionModel())
        self.table.selectionModel().selectionChanged.connect(self._update_selection_actions)
        layout.addWidget(self.table)
        self.setCentralWidget(central)

        self._build_menus()
        toolbar = QToolBar("品目", self)
        self.addToolBar(toolbar)
        toolbar.addAction(self.new_action)
        self.statusBar()

    @staticmethod
    def _master_combo() -> QComboBox:
        combo = QComboBox()
        combo.addItem("すべて", None)
        return combo

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル")
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

        help_menu = self.menuBar().addMenu("ヘルプ")
        help_menu.addAction("バージョン情報", self._show_about)
        self._update_selection_actions()

    def refresh(self) -> None:
        selected_id = self._selected_item_id()
        self._refresh_master_choices()
        self.refresh_items()
        if selected_id is not None:
            source_row = self.item_model.row_of(selected_id)
            if source_row is not None:
                proxy_index = self.sort_model.mapFromSource(self.item_model.index(source_row, 0))
                self.table.selectRow(proxy_index.row())
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

    def refresh_items(self, *_args) -> None:
        item_filter = ItemFilter(
            text=self.search_edit.text().strip() or None,
            client_id=self.client_combo.currentData(),
            purchaser_id=self.purchaser_combo.currentData(),
            category_id=self.category_picker.current_category_id(),
            location_id=self.location_combo.currentData(),
            low_stock_only=self.low_stock_checkbox.isChecked(),
            include_inactive=self.inactive_checkbox.isChecked(),
        )
        selected_id = self._selected_item_id()
        succeeded, rows = run_guarded(self, lambda: self.context.inventory.list_items(item_filter))
        if not succeeded or rows is None:
            return
        self.item_model.set_rows(rows)
        if selected_id is not None:
            source_row = self.item_model.row_of(selected_id)
            if source_row is not None:
                proxy_index = self.sort_model.mapFromSource(self.item_model.index(source_row, 0))
                self.table.selectRow(proxy_index.row())
        self.statusBar().showMessage(f"{len(rows):,} 件")
        self._update_selection_actions()

    def _selected_item_id(self) -> int | None:
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        source_index = self.sort_model.mapToSource(selected[0])
        return self.item_model.row_at(source_index.row()).id

    def _update_selection_actions(self, *_args) -> None:
        selected_id = self._selected_item_id() if hasattr(self, "table") else None
        row = None if selected_id is None else self.item_model.row_of(selected_id)
        item = None if row is None else self.item_model.row_at(row)
        has_selection = item is not None
        self.edit_action.setEnabled(has_selection)
        self.toggle_active_action.setEnabled(has_selection)
        if item is not None:
            self.toggle_active_action.setText("廃止" if item.is_active else "再有効化")

    def _create_item(self) -> None:
        self._open_item_dialog()

    def _edit_item(self) -> None:
        item_id = self._selected_item_id()
        if item_id is not None:
            self._open_item_dialog(item_id)

    def _open_item_dialog(self, item_id: int | None = None) -> None:
        ItemDialog = import_module("inventory_manager_mini.ui.dialogs.item_dialog").ItemDialog
        dialog = ItemDialog(self.context, item_id=item_id, parent=self)
        if dialog.exec():
            self.refresh()

    def _open_master_dialog(self, tab_name: str) -> None:
        master_dialog_module = import_module("inventory_manager_mini.ui.dialogs.master_dialog")
        dialog = master_dialog_module.MasterDialog(
            self.context,
            initial_tab=master_dialog_module.MasterTab(tab_name.lower()),
            parent=self,
        )
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
