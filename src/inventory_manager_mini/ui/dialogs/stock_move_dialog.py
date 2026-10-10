from __future__ import annotations

from functools import partial

from PySide6.QtCore import QLocale, QStringListModel, QTimer
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.errors import DomainError
from inventory_manager_mini.core.models import (
    MAX_MOVEMENT_AMOUNT,
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    REASON_LABELS,
    ItemFilter,
    ItemRow,
    Reason,
)
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.error_handling import run_guarded


class StockMoveDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        reason: Reason,
        item_id: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.item_id: int | None = None
        self._selected_item: ItemRow | None = None
        self._candidate_items: list[ItemRow] = []
        self._unavailable = False
        self._stocktake_quantity_invalid = False
        self.setWindowTitle("在庫操作")

        self._build_ui(reason)
        self._load_staff()
        self._load_items()
        if item_id is not None:
            self._select_item_by_id(item_id)
        self._connect_validation()
        self._update_reason_fields()
        self._validate()

    def _build_ui(self, reason: Reason) -> None:
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        form_widget = QWidget(self.scroll_area)
        self.form_layout = QFormLayout(form_widget)

        self.search_edit = QLineEdit(form_widget)
        self.search_edit.setPlaceholderText("品名・管理番号・メーカー型番")
        self.form_layout.addRow("品目を検索", self.search_edit)
        self.item_list = QListView(form_widget)
        self.item_list.setModel(QStringListModel(self.item_list))
        self.item_list.setMaximumHeight(145)
        self.form_layout.addRow("候補", self.item_list)
        self.selected_item_label = QLabel("品目未選択", form_widget)
        self.selected_item_label.setWordWrap(True)
        self.form_layout.addRow("選択中の品目", self.selected_item_label)

        self.reason_combo = QComboBox(form_widget)
        for value, label in REASON_LABELS.items():
            self.reason_combo.addItem(label, value)
        self.reason_combo.setCurrentIndex(self.reason_combo.findData(reason))
        self.form_layout.addRow("操作種別", self.reason_combo)

        self.quantity_spin = QSpinBox(form_widget)
        self.quantity_spin.setRange(1, MAX_STOCK_QUANTITY)
        self.quantity_label = QLabel("数量", form_widget)
        self.form_layout.addRow(self.quantity_label, self.quantity_spin)

        self.used_for_edit = QLineEdit(form_widget)
        self.form_layout.addRow("使用先", self.used_for_edit)
        self.price_locale = QLocale.c()
        self.price_locale.setNumberOptions(
            self.price_locale.numberOptions() | QLocale.NumberOption.RejectGroupSeparator
        )
        self.unit_price_edit = self._integer_edit(form_widget, self.price_locale)
        self.form_layout.addRow("単価", self.unit_price_edit)

        self.staff_combo = QComboBox(form_widget)
        self.staff_combo.addItem("(選択してください)", None)
        self.form_layout.addRow("担当者", self.staff_combo)
        self.note_edit = QPlainTextEdit(form_widget)
        self.note_edit.setMaximumHeight(90)
        self.form_layout.addRow("メモ", self.note_edit)

        self.preview_label = QLabel("操作後数量: -", form_widget)
        self.preview_label.setWordWrap(True)
        self.form_layout.addRow("操作後数量", self.preview_label)
        self.error_label = QLabel(form_widget)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318")
        self.form_layout.addRow(self.error_label)

        self.scroll_area.setWidget(form_widget)
        outer.addWidget(self.scroll_area)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.button_box.accepted.connect(self._save)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)
        available = self.screen().availableGeometry()
        self.resize(min(760, available.width() - 24), min(660, available.height() - 48))

    @staticmethod
    def _integer_edit(parent: QWidget, locale: QLocale) -> QLineEdit:
        edit = QLineEdit(parent)
        validator = QIntValidator(0, MAX_UNIT_PRICE, edit)
        validator.setLocale(locale)
        edit.setValidator(validator)
        return edit

    def _load_staff(self) -> None:
        succeeded, staff = run_guarded(self, self.context.master.list_staff)
        if not succeeded or staff is None:
            self._unavailable = True
            return
        for member in staff:
            self.staff_combo.addItem(member.name, member.id)

    def _load_items(self) -> None:
        selected_id = None if self._selected_item is None else self._selected_item.id
        succeeded, items = run_guarded(
            self,
            lambda: self.context.inventory.list_items(
                ItemFilter(text=self.search_edit.text(), include_inactive=False)
            ),
        )
        if not succeeded or items is None:
            self._unavailable = True
            self._validate()
            return
        self._candidate_items = items
        model = self.item_list.model()
        assert isinstance(model, QStringListModel)
        candidate_labels = [f"{item.code}  {item.name}  ({item.quantity:,} 個)" for item in items]
        model.setStringList(candidate_labels)
        if selected_id is not None:
            self._select_item_by_id(selected_id)

    def _select_item_by_id(self, item_id: int) -> None:
        for item in self._candidate_items:
            if item.id == item_id:
                self._set_selected_item(item)
                index = self.item_list.model().index(self._candidate_items.index(item), 0)
                self.item_list.setCurrentIndex(index)
                return

    def _set_selected_item(self, item: ItemRow) -> None:
        self._selected_item = item
        self.item_id = item.id
        price = "未登録" if item.reference_price is None else f"{item.reference_price:,} 円"
        self.selected_item_label.setText(
            f"{item.code} / {item.name} / 現在数量 {item.quantity:,} 個 / 参考価格 {price}"
        )
        self._update_reason_fields()
        self._validate()

    def _select_candidate(self, current, _previous) -> None:
        row = current.row()
        if 0 <= row < len(self._candidate_items):
            self._set_selected_item(self._candidate_items[row])

    def _connect_validation(self) -> None:
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._load_items)
        self.search_edit.textChanged.connect(lambda _text: self._search_timer.start())
        self.item_list.selectionModel().currentChanged.connect(self._select_candidate)
        self.reason_combo.currentIndexChanged.connect(self._update_reason_fields)
        self.reason_combo.currentIndexChanged.connect(self._validate)
        self.quantity_spin.valueChanged.connect(self._validate)
        self.staff_combo.currentIndexChanged.connect(self._validate)
        self.used_for_edit.textChanged.connect(self._validate)
        self.unit_price_edit.textChanged.connect(self._validate)

    def _reason(self) -> Reason:
        reason = self.reason_combo.currentData()
        return Reason(reason)

    def _update_reason_fields(self, *_args) -> None:
        reason = self._reason()
        is_stocktake = reason is Reason.ADJUST
        self.quantity_label.setText("実数" if is_stocktake else "数量")
        self.form_layout.setRowVisible(self.used_for_edit, reason is Reason.OUT)
        self.form_layout.setRowVisible(self.unit_price_edit, reason is Reason.IN)
        self.quantity_spin.setRange(0 if is_stocktake else 1, MAX_STOCK_QUANTITY)
        self._stocktake_quantity_invalid = False
        if is_stocktake and self._selected_item is not None:
            current_quantity = self._selected_item.quantity
            if current_quantity > MAX_STOCK_QUANTITY:
                self._stocktake_quantity_invalid = True
                if current_quantity <= 2**31 - 1:
                    self.quantity_spin.setRange(0, current_quantity)
                    self.quantity_spin.setValue(current_quantity)
                self.quantity_spin.setEnabled(False)
            else:
                self.quantity_spin.setEnabled(True)
                self.quantity_spin.setValue(current_quantity)
        else:
            self.quantity_spin.setEnabled(True)
            if is_stocktake:
                self.quantity_spin.setValue(0)
        if reason is Reason.IN and self._selected_item is not None:
            reference_price = self._selected_item.reference_price
            self.unit_price_edit.setText("" if reference_price is None else str(reference_price))
        self._validate()

    def _validate(self, *_args) -> bool:
        messages: list[str] = []
        item = self._selected_item
        reason = self._reason()
        quantity = self.quantity_spin.value()
        resulting_quantity: int | None = None
        if item is None:
            messages.append("品目を選択してください")
        else:
            delta = quantity if reason is Reason.IN else -quantity
            if reason is Reason.ADJUST:
                delta = quantity - item.quantity
            resulting_quantity = item.quantity + delta
            if self._stocktake_quantity_invalid:
                messages.append(
                    f"現在数量 {item.quantity:,} が入力上限({MAX_STOCK_QUANTITY:,})を超えています"
                )
            if not 0 <= resulting_quantity <= MAX_STOCK_QUANTITY:
                messages.append(f"操作後数量は 0〜{MAX_STOCK_QUANTITY:,} の範囲にしてください")

        if self.staff_combo.currentData() is None:
            messages.append("担当者を選択してください")
        if self.staff_combo.count() == 1:
            messages.append("先に担当者マスタを登録してください")
        if reason is Reason.OUT and not self.used_for_edit.text().strip():
            messages.append("使用先を入力してください")

        if reason is Reason.IN:
            price_text = self.unit_price_edit.text().strip()
            if price_text:
                if not self.unit_price_edit.hasAcceptableInput():
                    messages.append(f"単価は 0〜{MAX_UNIT_PRICE:,} 円で入力してください")
                else:
                    _, ok = self.price_locale.toInt(price_text)
                    if ok:
                        pass
                    else:
                        messages.append("単価を数値に変換できません")

        if reason in (Reason.OUT, Reason.DISPOSE) and item is not None:
            try:
                estimate = self.context.inventory.estimate_outflow(item.id, quantity)
            except DomainError as error:
                messages.append(str(error))
            else:
                if estimate.cost_amount > MAX_MOVEMENT_AMOUNT:
                    messages.append(f"1 操作の金額は {MAX_MOVEMENT_AMOUNT:,} 円以下にしてください")

        if resulting_quantity is None:
            self.preview_label.setText("操作後数量: -")
        elif reason is Reason.ADJUST:
            assert item is not None
            self.preview_label.setText(
                f"操作後数量: {resulting_quantity:,} 個 / "
                f"差分: {resulting_quantity - item.quantity:+,} 個"
            )
        else:
            self.preview_label.setText(f"操作後数量: {resulting_quantity:,} 個")
        self.error_label.setText("\n".join(dict.fromkeys(messages)))
        enabled = not messages and not self._unavailable
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)
        return enabled

    def _save(self) -> None:
        if not self._validate():
            return
        item = self._selected_item
        staff_id = self.staff_combo.currentData()
        assert item is not None and isinstance(staff_id, int)
        reason = self._reason()
        quantity = self.quantity_spin.value()
        note = self.note_edit.toPlainText().strip() or None
        if reason is Reason.IN:
            price_text = self.unit_price_edit.text().strip()
            unit_price: int | None = None
            if price_text:
                unit_price, ok = self.price_locale.toInt(price_text)
                if not ok:
                    self._validate()
                    return
            update_reference_price = False
            if price_text and unit_price != item.reference_price:
                answer = QMessageBox.question(
                    self,
                    "参考価格の更新",
                    "参考価格を更新しますか",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Cancel:
                    return
                update_reference_price = answer == QMessageBox.StandardButton.Yes
            operation = partial(
                self.context.inventory.receive,
                item.id,
                staff_id,
                quantity,
                unit_price,
                update_reference_price,
                note,
            )
        elif reason is Reason.OUT:
            operation = partial(
                self.context.inventory.issue,
                item.id,
                staff_id,
                quantity,
                self.used_for_edit.text().strip(),
                note,
            )
        elif reason is Reason.RETURN:
            operation = partial(
                self.context.inventory.return_to_supplier, item.id, staff_id, quantity, note
            )
        elif reason is Reason.DISPOSE:
            operation = partial(self.context.inventory.dispose, item.id, staff_id, quantity, note)
        else:
            operation = partial(self.context.inventory.stocktake, item.id, staff_id, quantity, note)

        succeeded, movement = run_guarded(self, operation)
        if succeeded and movement is not None:
            self.context.data_bus.data_changed.emit()
            self.accept()
