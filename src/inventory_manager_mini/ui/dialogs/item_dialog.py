from __future__ import annotations

from PySide6.QtCore import QLocale, QUrl
from PySide6.QtGui import QDesktopServices, QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import (
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    ItemUpdate,
    NewItem,
)
from inventory_manager_mini.core.services import validate_purchase_url
from inventory_manager_mini.core.timeutil import local_date
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.error_handling import run_guarded
from inventory_manager_mini.ui.widgets.category_picker import CategoryPicker


class ItemDialog(QDialog):
    def __init__(
        self,
        context: AppContext,
        item_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.context = context
        self.item_id = item_id
        self.item = None
        self._threshold_out_of_range: int | None = None
        self.setWindowTitle("品目の登録" if item_id is None else "品目の編集")
        self.setMinimumSize(620, 480)

        if item_id is not None:
            succeeded, item = run_guarded(self, lambda: context.inventory.get_item(item_id))
            if not succeeded or item is None:
                if succeeded:
                    QMessageBox.warning(self, "品目が見つかりません", "対象の品目を取得できません")
                self._unavailable = True
                item = None
            else:
                self._unavailable = False
            self.item = item
        else:
            self._unavailable = False

        self._build_ui()
        self._load_choices()
        if self.item is not None:
            self._load_item()
        self._connect_validation()
        self._update_initial_staff_state()
        self._validate()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        form_widget = QWidget(self.scroll_area)
        form = QFormLayout(form_widget)
        self.form_layout = form

        self.code_label = QLabel("登録時に自動採番", form_widget)
        form.addRow("管理番号", self.code_label)

        self.client_combo = QComboBox(form_widget)
        form.addRow("クライアント", self.client_combo)
        self.client_notice = QLabel(form_widget)
        self.client_notice.setWordWrap(True)
        form.addRow(self.client_notice)
        self.name_edit = QLineEdit(form_widget)
        form.addRow("品名", self.name_edit)
        self.manufacturer_edit = QLineEdit(form_widget)
        form.addRow("メーカー型番", self.manufacturer_edit)
        self.application_edit = QLineEdit(form_widget)
        form.addRow("用途", self.application_edit)
        self.category_picker = CategoryPicker(form_widget)
        form.addRow("カテゴリ", self.category_picker)

        self.location_combo = QComboBox(form_widget)
        form.addRow("保管場所", self.location_combo)
        self.unit_label = QLabel("個", form_widget)
        form.addRow("単位", self.unit_label)

        self.initial_quantity_spin: QSpinBox | None = None
        self.initial_quantity_row: QSpinBox | None = None
        self.quantity_label: QLabel | None = None
        self.quantity_row: QLabel | None = None
        self.initial_staff_combo: QComboBox | None = None
        self.initial_staff_row: QComboBox | None = None
        self.last_purchase_label: QLabel | None = None
        self.last_purchase_row: QLabel | None = None
        if self.item is None:
            self.initial_quantity_spin = QSpinBox(form_widget)
            self.initial_quantity_spin.setRange(0, MAX_STOCK_QUANTITY)
            self.initial_quantity_row = self.initial_quantity_spin
            self.initial_staff_combo = QComboBox(form_widget)
            self.initial_staff_combo.addItem("選択してください", None)
            self.initial_staff_row = self.initial_staff_combo
            form.addRow("初期数量", self.initial_quantity_spin)
            form.addRow("初期数量の記録担当者", self.initial_staff_combo)
        else:
            self.quantity_label = QLabel("0", form_widget)
            self.quantity_row = self.quantity_label
            self.last_purchase_label = QLabel("-", form_widget)
            self.last_purchase_row = self.last_purchase_label
            assert self.quantity_label is not None
            form.addRow("現在数量", self.quantity_label)

        self.threshold_spin = QSpinBox(form_widget)
        self.threshold_spin.setRange(0, MAX_STOCK_QUANTITY)
        self.threshold_spin.setValue(0)
        form.addRow("閾値", self.threshold_spin)

        self.reorder_quantity_edit = self._integer_edit(1, MAX_STOCK_QUANTITY)
        form.addRow("推奨発注数", self.reorder_quantity_edit)

        purchasing_row = QWidget(form_widget)
        purchasing_layout = QHBoxLayout(purchasing_row)
        purchasing_layout.setContentsMargins(0, 0, 0, 0)
        self.purchaser_combo = QComboBox(purchasing_row)
        self.purchase_url_edit = QLineEdit(purchasing_row)
        self.purchase_url_edit.setPlaceholderText("https://")
        self.open_url_button = QPushButton("開く", purchasing_row)
        self.open_url_button.setToolTip("販売ページをブラウザーで開く")
        self.open_url_button.clicked.connect(self._open_purchase_url)
        purchasing_layout.addWidget(self.purchaser_combo, 1)
        purchasing_layout.addWidget(self.purchase_url_edit, 2)
        purchasing_layout.addWidget(self.open_url_button)
        form.addRow("発注主体 / 販売ページ", purchasing_row)
        self.purchaser_notice = QLabel(form_widget)
        self.purchaser_notice.setWordWrap(True)
        form.addRow(self.purchaser_notice)

        self.supplier_edit = QLineEdit(form_widget)
        form.addRow("仕入先", self.supplier_edit)
        self.reference_price_edit = self._integer_edit(0, MAX_UNIT_PRICE)
        form.addRow("参考価格", self.reference_price_edit)
        self.note_edit = QPlainTextEdit(form_widget)
        self.note_edit.setMaximumHeight(100)
        form.addRow("備考", self.note_edit)

        if self.item is not None:
            assert self.last_purchase_label is not None
            form.addRow("最終購入日・購入ロット数", self.last_purchase_label)
        self.error_label = QLabel(form_widget)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #b42318")
        form.addRow(self.error_label)

        self.scroll_area.setWidget(form_widget)
        outer.addWidget(self.scroll_area)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.button_box.accepted.connect(self._save)
        self.button_box.rejected.connect(self.reject)
        outer.addWidget(self.button_box)
        self.resize(760, 700)

    @staticmethod
    def _integer_edit(minimum: int, maximum: int) -> QLineEdit:
        edit = QLineEdit()
        locale = QLocale.c()
        locale.setNumberOptions(locale.numberOptions() | QLocale.NumberOption.RejectGroupSeparator)
        validator = QIntValidator(minimum, maximum, edit)
        validator.setLocale(locale)
        edit.setValidator(validator)
        edit.setMaximumWidth(220)
        return edit

    def _load_choices(self) -> None:
        succeeded, choices = run_guarded(
            self,
            lambda: (
                self.context.master.list_clients(include_inactive=True),
                self.context.master.list_purchasers(include_inactive=True),
                self.context.master.list_staff(),
                self.context.master.list_categories(),
                self.context.master.list_locations(),
            ),
        )
        if not succeeded or choices is None:
            self._unavailable = True
            return
        clients, purchasers, staff, categories, locations = choices
        current_client_id = None if self.item is None else self.item.client_id
        current_purchaser_id = None if self.item is None else self.item.purchaser_id
        if any(client.id == current_client_id and not client.is_active for client in clients):
            self.client_notice.setText(
                "現在のクライアントは無効化されています。変更する場合は有効なものを選択してください"
            )
        if any(
            purchaser.id == current_purchaser_id and not purchaser.is_active
            for purchaser in purchasers
        ):
            self.purchaser_notice.setText(
                "現在の発注主体は無効化されています。変更する場合は有効なものを選択してください"
            )
        self._fill_required_masters(self.client_combo, clients, current_client_id)
        self._fill_required_masters(self.purchaser_combo, purchasers, current_purchaser_id)
        self.category_picker.set_categories(categories)
        self.location_combo.addItem("(なし)", None)
        for location in locations:
            self.location_combo.addItem(location.name, location.id)
        if self.initial_staff_combo is not None:
            for member in staff:
                self.initial_staff_combo.addItem(member.name, member.id)

    @staticmethod
    def _fill_required_masters(combo: QComboBox, masters, current_id: int | None) -> None:
        combo.clear()
        current_inactive = [
            master for master in masters if master.id == current_id and not master.is_active
        ]
        available = current_inactive + [master for master in masters if master.is_active]
        for master in available:
            label = master.name if master.is_active else f"{master.name} (無効)"
            combo.addItem(label, master.id)
        index = combo.findData(current_id)
        combo.setCurrentIndex(index if index >= 0 else -1)

    def _load_item(self) -> None:
        assert self.item is not None
        item = self.item
        self.code_label.setText(item.code)
        self.client_combo.setCurrentIndex(self.client_combo.findData(item.client_id))
        self.name_edit.setText(item.name)
        self.manufacturer_edit.setText(item.manufacturer_part_number or "")
        self.application_edit.setText(item.application or "")
        self.category_picker.set_current_category_id(item.category_id)
        self.location_combo.setCurrentIndex(self.location_combo.findData(item.location_id))
        threshold = item.reorder_threshold
        if not 0 <= threshold <= MAX_STOCK_QUANTITY:
            self._threshold_out_of_range = threshold
            spinbox_minimum = -(2**31)
            spinbox_maximum = 2**31 - 1
            if spinbox_minimum <= threshold <= spinbox_maximum:
                self.threshold_spin.setRange(min(0, threshold), max(MAX_STOCK_QUANTITY, threshold))
                self.threshold_spin.setValue(threshold)
            else:
                self.threshold_spin.setEnabled(False)
        else:
            self.threshold_spin.setValue(threshold)
        self.reorder_quantity_edit.setText(
            "" if item.reorder_quantity is None else str(item.reorder_quantity)
        )
        self.purchaser_combo.setCurrentIndex(self.purchaser_combo.findData(item.purchaser_id))
        self.purchase_url_edit.setText(item.purchase_url or "")
        self.supplier_edit.setText(item.supplier or "")
        self.reference_price_edit.setText(
            "" if item.reference_price is None else str(item.reference_price)
        )
        self.note_edit.setPlainText(item.note or "")
        assert self.quantity_row is not None
        self.quantity_row.setText(f"{item.quantity:,} 個")
        succeeded, purchase_info = run_guarded(
            self, lambda: self.context.inventory.get_purchase_info(item.id)
        )
        if succeeded and purchase_info is not None and purchase_info.last_purchased_at:
            assert self.last_purchase_label is not None
            last_date = local_date(purchase_info.last_purchased_at).isoformat()
            lot_quantity = (
                "-" if purchase_info.lot_quantity is None else f"{purchase_info.lot_quantity:,} 個"
            )
            self.last_purchase_label.setText(f"{last_date} / {lot_quantity}")

    def _connect_validation(self) -> None:
        self.client_combo.currentIndexChanged.connect(self._validate)
        self.name_edit.textChanged.connect(self._validate)
        self.category_picker.category_changed.connect(self._validate)
        self.location_combo.currentIndexChanged.connect(self._validate)
        if self.initial_quantity_spin is not None:
            self.initial_quantity_spin.valueChanged.connect(self._update_initial_staff_state)
            self.initial_quantity_spin.valueChanged.connect(self._validate)
        if self.initial_staff_combo is not None:
            self.initial_staff_combo.currentIndexChanged.connect(self._validate)
        self.threshold_spin.valueChanged.connect(self._threshold_changed)
        self.reorder_quantity_edit.textChanged.connect(self._validate)
        self.purchaser_combo.currentIndexChanged.connect(self._validate)
        self.purchase_url_edit.textChanged.connect(self._validate)
        self.reference_price_edit.textChanged.connect(self._validate)

    def _update_initial_staff_state(self, *_args) -> None:
        if self.initial_staff_combo is None:
            return
        assert self.initial_quantity_spin is not None
        enabled = self.initial_quantity_spin.value() > 0
        self.initial_staff_combo.setEnabled(enabled)
        if not enabled:
            self.initial_staff_combo.setCurrentIndex(0)

    def _threshold_changed(self, value: int) -> None:
        if 0 <= value <= MAX_STOCK_QUANTITY:
            self._threshold_out_of_range = None
            self.threshold_spin.setRange(0, MAX_STOCK_QUANTITY)
        else:
            self._threshold_out_of_range = value
        self._validate()

    def _validate(self, *_args) -> None:
        messages: list[str] = []
        valid_url = True
        if self._unavailable:
            messages.append("品目またはマスタを取得できません")
        if self.item_id is None:
            if self.client_combo.count() == 0:
                messages.append("先にクライアントマスタを登録してください")
            if self.purchaser_combo.count() == 0:
                messages.append("先に発注主体マスタを登録してください")
            if self.category_picker.category_count() == 0:
                messages.append("先にカテゴリマスタを登録してください")
        if self.client_combo.currentData() is None:
            messages.append("クライアントを選択してください")
        if not self.name_edit.text().strip():
            messages.append("品名を入力してください")
        if self.category_picker.current_category_id() is None:
            messages.append("カテゴリを選択してください")
        if self.purchaser_combo.currentData() is None:
            messages.append("発注主体を選択してください")
        if self._threshold_out_of_range is not None:
            messages.append(
                f"閾値が 0〜{MAX_STOCK_QUANTITY:,} の範囲外です "
                f"(現在値: {self._threshold_out_of_range})。範囲内の値に修正してください"
            )
        if not self._has_valid_optional_integer(self.reorder_quantity_edit):
            messages.append("推奨発注数は 1〜1,000,000 の整数で指定してください")
        if not self._has_valid_optional_integer(self.reference_price_edit):
            messages.append("参考価格は 0〜10,000,000 の整数で指定してください")
        if self.item_id is None:
            assert self.initial_quantity_spin is not None
        if (
            self.item_id is None
            and self.initial_quantity_spin is not None
            and self.initial_quantity_spin.value() > 0
        ):
            assert self.initial_staff_combo is not None
            if self.initial_staff_combo.currentData() is None:
                messages.append("初期数量を記録する担当者を選択してください")
        try:
            validate_purchase_url(self.purchase_url_edit.text())
        except ValidationError as error:
            valid_url = False
            messages.append(error.message)
        self.error_label.setText("\n".join(dict.fromkeys(messages)))
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not messages)
        self.open_url_button.setEnabled(bool(self.purchase_url_edit.text().strip()) and valid_url)

    def _open_purchase_url(self) -> None:
        succeeded, url = run_guarded(
            self, lambda: validate_purchase_url(self.purchase_url_edit.text())
        )
        if succeeded and url is not None:
            QDesktopServices.openUrl(QUrl(url))

    def _save(self) -> None:
        self._validate()
        if not self.button_box.button(QDialogButtonBox.StandardButton.Ok).isEnabled():
            return
        category_id = self.category_picker.current_category_id()
        if category_id is None:
            return
        reorder_quantity = self._optional_integer(self.reorder_quantity_edit)
        reference_price = self._optional_integer(self.reference_price_edit)
        if self.item_id is None:
            assert self.initial_quantity_spin is not None
            assert self.initial_staff_combo is not None
            value = NewItem(
                client_id=self.client_combo.currentData(),
                purchaser_id=self.purchaser_combo.currentData(),
                name=self.name_edit.text().strip(),
                category_id=category_id,
                location_id=self.location_combo.currentData(),
                initial_quantity=self.initial_quantity_spin.value(),
                initial_staff_id=self.initial_staff_combo.currentData(),
                reorder_threshold=self.threshold_spin.value(),
                reorder_quantity=reorder_quantity,
                purchase_url=self.purchase_url_edit.text(),
                supplier=self.supplier_edit.text(),
                manufacturer_part_number=self.manufacturer_edit.text(),
                application=self.application_edit.text(),
                reference_price=reference_price,
                note=self.note_edit.toPlainText(),
            )
            succeeded, _item = run_guarded(self, lambda: self.context.inventory.create_item(value))
        else:
            value = ItemUpdate(
                id=self.item_id,
                client_id=self.client_combo.currentData(),
                purchaser_id=self.purchaser_combo.currentData(),
                name=self.name_edit.text().strip(),
                category_id=category_id,
                location_id=self.location_combo.currentData(),
                reorder_threshold=self.threshold_spin.value(),
                reorder_quantity=reorder_quantity,
                purchase_url=self.purchase_url_edit.text(),
                supplier=self.supplier_edit.text(),
                manufacturer_part_number=self.manufacturer_edit.text(),
                application=self.application_edit.text(),
                reference_price=reference_price,
                note=self.note_edit.toPlainText(),
            )
            succeeded, _item = run_guarded(self, lambda: self.context.inventory.update_item(value))
        if succeeded:
            self.context.data_bus.data_changed.emit()
            self.accept()

    @staticmethod
    def _optional_integer(edit: QLineEdit) -> int | None:
        text = edit.text().strip()
        if not text:
            return None
        value, valid = QLocale.c().toInt(text)
        if not valid:
            raise ValueError("整数を変換できません")
        return value

    @staticmethod
    def _has_valid_optional_integer(edit: QLineEdit) -> bool:
        return not edit.text() or edit.hasAcceptableInput()
