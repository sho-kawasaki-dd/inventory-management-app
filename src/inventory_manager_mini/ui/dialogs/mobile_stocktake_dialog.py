from __future__ import annotations

import re
import secrets
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.models import ItemFilter, MobileStocktakePreview
from inventory_manager_mini.core.timeutil import format_local, utc_now_str
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.error_handling import run_guarded


class MobileStocktakeDialog(QDialog):
    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._active_stocktakes = []
        self._preview: MobileStocktakePreview | None = None
        self._preview_raw_text: str | None = None
        self.setWindowTitle("スマホ棚卸")
        self._build_ui()
        self._load_sources()
        self._update_confirm_button()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self._build_export_tab()
        self._build_import_tab()

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)
        available = self.screen().availableGeometry()
        self.resize(min(1000, available.width() - 32), min(650, available.height() - 48))

    def _build_export_tab(self) -> None:
        tab = QWidget(self.tabs)
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.client_combo = QComboBox(tab)
        form.addRow("クライアント", self.client_combo)
        self.export_staff_combo = QComboBox(tab)
        form.addRow("シート作成担当者", self.export_staff_combo)
        layout.addLayout(form)

        self.export_button = QPushButton("棚卸シート(.html)を作成して保存…", tab)
        self.export_button.clicked.connect(self._export_sheet)
        layout.addWidget(self.export_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.active_table = QTableWidget(0, 4, tab)
        self.active_table.setHorizontalHeaderLabels(
            ("クライアント", "担当者", "開始日時", "品目数")
        )
        self.active_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.active_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.active_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.active_table.verticalHeader().setVisible(False)
        self.active_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.active_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.active_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.active_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.active_table.currentCellChanged.connect(self._update_cancel_button)
        layout.addWidget(QLabel("現在棚卸手続き中のクライアント", tab))
        layout.addWidget(self.active_table, 1)

        controls = QHBoxLayout()
        self.cancel_stocktake_button = QPushButton("選択クライアントの棚卸を中止", tab)
        self.cancel_stocktake_button.clicked.connect(self._cancel_stocktake)
        self.cancel_stocktake_button.setEnabled(False)
        controls.addWidget(self.cancel_stocktake_button)
        self.go_to_import_button = QPushButton("結果取り込みタブへ進む", tab)
        self.go_to_import_button.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        controls.addWidget(self.go_to_import_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.tabs.addTab(tab, "① 出発前：棚卸シート出力・管理")

    def _build_import_tab(self) -> None:
        tab = QWidget(self.tabs)
        layout = QVBoxLayout(tab)
        source_buttons = QHBoxLayout()
        self.paste_button = QPushButton("クリップボードから結果テキストを貼り付け", tab)
        self.paste_button.clicked.connect(self._paste_clipboard)
        source_buttons.addWidget(self.paste_button)
        self.load_file_button = QPushButton("結果ファイル(.json)を選択…", tab)
        self.load_file_button.clicked.connect(self._load_result_file)
        source_buttons.addWidget(self.load_file_button)
        source_buttons.addStretch(1)
        layout.addLayout(source_buttons)

        self.result_edit = QPlainTextEdit(tab)
        self.result_edit.setPlaceholderText("IMM1:...:END または結果JSONを貼り付け")
        self.result_edit.setMaximumHeight(95)
        self.result_edit.textChanged.connect(self._invalidate_preview)
        layout.addWidget(self.result_edit)

        preview_controls = QHBoxLayout()
        self.preview_button = QPushButton("プレビューを更新", tab)
        self.preview_button.clicked.connect(self._preview_result)
        preview_controls.addWidget(self.preview_button)
        self.preview_status_label = QLabel("結果を読み込んでプレビューしてください", tab)
        self.preview_status_label.setWordWrap(True)
        preview_controls.addWidget(self.preview_status_label, 1)
        layout.addLayout(preview_controls)

        self.preview_summary_label = QLabel(tab)
        self.preview_summary_label.setWordWrap(True)
        layout.addWidget(self.preview_summary_label)
        self.differences_only_checkbox = QCheckBox("差異のある品目のみ表示", tab)
        self.differences_only_checkbox.toggled.connect(self._render_preview_rows)
        layout.addWidget(self.differences_only_checkbox)

        self.preview_table = QTableWidget(0, 7, tab)
        self.preview_table.setHorizontalHeaderLabels(
            ("管理番号", "品名", "保管場所", "帳簿数量", "棚卸実数", "差分", "メモ")
        )
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview_table.verticalHeader().setVisible(False)
        header = self.preview_table.horizontalHeader()
        for column in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.preview_table, 1)

        bottom = QHBoxLayout()
        self.confirm_staff_combo = QComboBox(tab)
        self.confirm_staff_combo.currentIndexChanged.connect(self._update_confirm_button)
        bottom.addWidget(QLabel("確定担当者", tab))
        bottom.addWidget(self.confirm_staff_combo)
        self.confirm_button = QPushButton("この内容で棚卸を一括確定する", tab)
        self.confirm_button.clicked.connect(self._complete_stocktake)
        bottom.addWidget(self.confirm_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        self.tabs.addTab(tab, "② 帰社後：結果取り込み・確定")

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)

    def _load_sources(self) -> None:
        succeeded, values = run_guarded(
            self,
            lambda: (
                self.context.master.list_clients(),
                self.context.master.list_staff(),
                self.context.inventory.list_active_stocktakes(),
            ),
        )
        if not succeeded or values is None:
            self.export_button.setEnabled(False)
            return
        clients, staff, self._active_stocktakes = values
        locked_by_client = {entry.client_id: entry for entry in self._active_stocktakes}
        previous_client_id = self.client_combo.currentData()
        self.client_combo.clear()
        for client in clients:
            if client.id in locked_by_client:
                item_count = locked_by_client[client.id].item_count
                suffix = " (棚卸中)"
            else:
                item_succeeded, items = run_guarded(
                    self,
                    lambda client_id=client.id: self.context.inventory.list_items(
                        ItemFilter(client_id=client_id)
                    ),
                )
                if not item_succeeded or items is None:
                    self.export_button.setEnabled(False)
                    return
                item_count = len(items)
                suffix = ""
            self.client_combo.addItem(
                f"{client.name} / 有効品目 {item_count} 件{suffix}", client.id
            )
        if previous_client_id is not None:
            index = self.client_combo.findData(previous_client_id)
            if index >= 0:
                self.client_combo.setCurrentIndex(index)

        previous_export_staff = self.export_staff_combo.currentData()
        self.export_staff_combo.clear()
        self.export_staff_combo.addItem("(選択してください)", None)
        for member in staff:
            self.export_staff_combo.addItem(member.name, member.id)
        if previous_export_staff is not None:
            index = self.export_staff_combo.findData(previous_export_staff)
            if index >= 0:
                self.export_staff_combo.setCurrentIndex(index)

        previous_confirm_staff = self.confirm_staff_combo.currentData()
        self.confirm_staff_combo.clear()
        self.confirm_staff_combo.addItem("(選択してください)", None)
        for member in staff:
            self.confirm_staff_combo.addItem(member.name, member.id)
        if previous_confirm_staff is not None:
            index = self.confirm_staff_combo.findData(previous_confirm_staff)
            if index >= 0:
                self.confirm_staff_combo.setCurrentIndex(index)
        self._render_active_stocktakes()
        self._update_cancel_button()
        self.export_button.setEnabled(bool(clients and staff))
        self._update_confirm_button()

    def _render_active_stocktakes(self) -> None:
        self.active_table.setRowCount(len(self._active_stocktakes))
        for row, stocktake in enumerate(self._active_stocktakes):
            values = (
                stocktake.client_name,
                stocktake.staff_name,
                format_local(stocktake.started_at),
                f"{stocktake.item_count:,}",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, stocktake.client_id)
                self.active_table.setItem(row, column, cell)

    def _update_cancel_button(self, *_args) -> None:
        self.cancel_stocktake_button.setEnabled(self.active_table.currentRow() >= 0)

    def _selected_stocktake(self):
        row = self.active_table.currentRow()
        if row < 0 or row >= len(self._active_stocktakes):
            return None
        return self._active_stocktakes[row]

    def _export_sheet(self) -> None:
        client_id = self.client_combo.currentData()
        staff_id = self.export_staff_combo.currentData()
        if client_id is None or staff_id is None:
            QMessageBox.warning(self, "棚卸シート", "クライアントと担当者を選択してください")
            return
        active = next(
            (entry for entry in self._active_stocktakes if entry.client_id == client_id), None
        )
        if active is not None:
            answer = QMessageBox.question(
                self,
                "棚卸シートの再作成",
                "再作成すると前のシートは無効になります。続行しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        client_name = self.client_combo.currentText().split(" / 有効品目", 1)[0]
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", client_name).strip(" .")
        timestamp = (
            format_local(utc_now_str())[:16].replace("-", "").replace(" ", "_").replace(":", "")
        )
        suggested = f"棚卸シート_{safe_name}_{timestamp}_{secrets.token_hex(4)}.html"
        initial_path = self.context.paths.data_dir / suggested
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "棚卸シートの保存先",
            str(initial_path),
            "HTML ファイル (*.html)",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path_text:
            return
        path = Path(path_text)
        if path.suffix.lower() != ".html":
            path = path.with_suffix(".html")
        succeeded, _sheet = run_guarded(
            self,
            lambda: self.context.inventory.export_mobile_stocktake_html(path, client_id, staff_id),
        )
        if not succeeded:
            return
        self.context.data_bus.data_changed.emit()
        self._load_sources()
        QMessageBox.information(
            self,
            "棚卸シート",
            f"棚卸シートを保存しました。既存ファイルは上書きできません。\n{path}",
        )
        answer = QMessageBox.question(
            self,
            "保存先フォルダ",
            "保存先フォルダを開きますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _cancel_stocktake(self) -> None:
        stocktake = self._selected_stocktake()
        if stocktake is None:
            return
        answer = QMessageBox.question(
            self,
            "棚卸の中止",
            f"{stocktake.client_name}の棚卸を中止します。持ち出したシートの結果は"
            "取り込めなくなります。よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        succeeded, _result = run_guarded(
            self,
            lambda: self.context.inventory.cancel_mobile_stocktake(stocktake.client_id),
        )
        if succeeded:
            self.context.data_bus.data_changed.emit()
            self._load_sources()

    def _invalidate_preview(self, *_args) -> None:
        self._preview = None
        self._preview_raw_text = None
        self.preview_summary_label.clear()
        self.preview_table.setRowCount(0)
        self.preview_status_label.setText("入力が変更されました。再度プレビューしてください")
        self._update_confirm_button()

    def _paste_clipboard(self) -> None:
        self._invalidate_preview()
        self.result_edit.setPlainText(QApplication.clipboard().text())
        self._preview_result()

    def _load_result_file(self) -> None:
        self._invalidate_preview()
        path_text, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "棚卸結果ファイルを選択",
            str(self.context.paths.data_dir),
            "JSON ファイル (*.json)",
        )
        if not path_text:
            return
        try:
            raw_text = Path(path_text).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            QMessageBox.warning(self, "結果ファイル", f"ファイルを読み込めませんでした: {error}")
            return
        self.result_edit.setPlainText(raw_text)
        self._preview_result()

    def _preview_result(self) -> None:
        raw_text = self.result_edit.toPlainText()
        if not raw_text.strip():
            self._invalidate_preview()
            self.preview_status_label.setText("結果テキストまたはJSONを入力してください")
            return
        self._invalidate_preview()
        succeeded, preview = run_guarded(
            self, lambda: self.context.inventory.preview_mobile_stocktake(raw_text)
        )
        if not succeeded or preview is None:
            self.preview_status_label.setText("プレビューできません。入力を確認してください")
            return
        self._preview = preview
        self._preview_raw_text = raw_text
        self.preview_status_label.setText(
            f"{preview.client_name} / 棚卸開始: {format_local(preview.started_at)}"
        )
        self.preview_summary_label.setText(
            f"対象 {len(preview.rows):,} 件 / 変動なし {preview.unchanged_count:,} 件 / "
            f"増加 {preview.increased_count:,} 件 / 減少 {preview.decreased_count:,} 件"
        )
        staff_index = self.confirm_staff_combo.findData(preview.staff_id)
        self.confirm_staff_combo.setCurrentIndex(staff_index if staff_index >= 0 else 0)
        self._render_preview_rows()
        self._update_confirm_button()

    def _render_preview_rows(self, *_args) -> None:
        preview = self._preview
        rows = [] if preview is None else list(preview.rows)
        if self.differences_only_checkbox.isChecked():
            rows = [row for row in rows if row.delta != 0]
        self.preview_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row.code,
                row.name,
                row.location_name or "",
                f"{row.book_quantity:,}",
                f"{row.actual_quantity:,}",
                f"{row.delta:+,}",
                row.note or "",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if row.delta != 0:
                    cell.setBackground(QColor("#fff0d6"))
                self.preview_table.setItem(row_index, column, cell)

    def _update_confirm_button(self, *_args) -> None:
        button = getattr(self, "confirm_button", None)
        if button is None:
            return
        button.setEnabled(
            self._preview is not None
            and self._preview_raw_text is not None
            and self.confirm_staff_combo.currentData() is not None
        )

    def _complete_stocktake(self) -> None:
        raw_text = self._preview_raw_text
        staff_id = self.confirm_staff_combo.currentData()
        if self._preview is None or raw_text is None or staff_id is None:
            self._update_confirm_button()
            return
        succeeded, movements = run_guarded(
            self,
            lambda: self.context.inventory.complete_mobile_stocktake(raw_text, staff_id),
        )
        if not succeeded or movements is None:
            return
        self.context.data_bus.data_changed.emit()
        QMessageBox.information(
            self,
            "棚卸完了",
            f"{self._preview.client_name}の棚卸を {len(movements):,} 件確定しました。",
        )
        self.accept()
