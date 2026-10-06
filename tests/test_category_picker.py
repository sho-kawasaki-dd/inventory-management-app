from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTreeWidgetItem

from inventory_manager_mini.core.models import Category
from inventory_manager_mini.ui.widgets.category_picker import CategoryPicker, CategoryPickerDialog


def _categories() -> list[Category]:
    return [
        Category(4, 1, "LED電球", "LE", 1),
        Category(3, None, "工具", "TL", 1),
        Category(2, 1, "文具", "ST", 1),
        Category(1, None, "備品", "TO", 1),
    ]


def _find(dialog: CategoryPickerDialog, category_id: int | None) -> QTreeWidgetItem:
    return dialog._items[category_id]


def _ok(dialog: CategoryPickerDialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)


def _top(dialog: CategoryPickerDialog, row: int) -> QTreeWidgetItem:
    item = dialog.tree.topLevelItem(row)
    assert item is not None
    return item


def _child(item: QTreeWidgetItem, row: int) -> QTreeWidgetItem:
    child = item.child(row)
    assert child is not None
    return child


def test_dialog_builds_expanded_sorted_tree(qtbot) -> None:
    dialog = CategoryPickerDialog(_categories(), None, None)
    qtbot.addWidget(dialog)

    top = [_top(dialog, row).text(0) for row in range(dialog.tree.topLevelItemCount())]
    supplies = _top(dialog, 0)

    assert top == ["備品", "工具"]
    assert [_child(supplies, row).text(0) for row in range(supplies.childCount())] == [
        "LED電球",
        "文具",
    ]
    assert supplies.isExpanded()


def test_dialog_without_selection_disables_ok(qtbot) -> None:
    dialog = CategoryPickerDialog(_categories(), None, None)
    qtbot.addWidget(dialog)

    assert dialog.selected_id() is None
    assert not _ok(dialog).isEnabled()

    dialog.tree.setCurrentItem(_find(dialog, 4))

    assert dialog.selected_id() == 4
    assert _ok(dialog).isEnabled()


def test_dialog_selects_current_and_leading_item(qtbot) -> None:
    current = CategoryPickerDialog(_categories(), "すべて", 4)
    qtbot.addWidget(current)
    leading = CategoryPickerDialog(_categories(), "すべて", None)
    qtbot.addWidget(leading)

    assert current.selected_id() == 4
    assert _top(leading, 0).text(0) == "すべて"
    assert _top(leading, 0).data(0, Qt.ItemDataRole.UserRole) is None
    assert leading.selected_id() is None
    assert _ok(leading).isEnabled()


def test_dialog_double_click_on_child_accepts(qtbot) -> None:
    dialog = CategoryPickerDialog(_categories(), None, None)
    qtbot.addWidget(dialog)
    dialog.show()
    item = _find(dialog, 4)
    rect = dialog.tree.visualItemRect(item)

    qtbot.mouseClick(dialog.tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qtbot.mouseDClick(dialog.tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_id() == 4


def test_dialog_ok_button_accepts_selection(qtbot) -> None:
    dialog = CategoryPickerDialog(_categories(), None, None)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.tree.setCurrentItem(_find(dialog, 2))

    qtbot.mouseClick(_ok(dialog), Qt.MouseButton.LeftButton)

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_id() == 2


def _pick_with(monkeypatch, category_id: int | None, accepted: bool = True) -> None:
    def fake_exec(dialog: CategoryPickerDialog) -> QDialog.DialogCode:
        if accepted:
            dialog.tree.setCurrentItem(dialog._items[category_id])
            return QDialog.DialogCode.Accepted
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(CategoryPickerDialog, "exec", fake_exec)


def test_picker_shows_full_path_and_emits_on_confirm(qtbot, monkeypatch) -> None:
    picker = CategoryPicker()
    qtbot.addWidget(picker)
    picker.set_categories(_categories())
    changed: list[object] = []
    picker.category_changed.connect(changed.append)
    _pick_with(monkeypatch, 4)

    qtbot.mouseClick(picker.button, Qt.MouseButton.LeftButton)

    assert picker.current_category_id() == 4
    assert picker.display.text() == "備品 > LED電球"
    assert picker.display.toolTip() == "備品 > LED電球"
    assert changed == [4]


def test_picker_cancel_and_same_selection_do_not_change_or_emit(qtbot, monkeypatch) -> None:
    picker = CategoryPicker()
    qtbot.addWidget(picker)
    picker.set_categories(_categories())
    picker.set_current_category_id(2)
    changed: list[object] = []
    picker.category_changed.connect(changed.append)

    _pick_with(monkeypatch, 4, accepted=False)
    picker.open_dialog()
    _pick_with(monkeypatch, 2)
    picker.open_dialog()

    assert picker.current_category_id() == 2
    assert changed == []


def test_picker_leading_label_selection(qtbot, monkeypatch) -> None:
    picker = CategoryPicker()
    qtbot.addWidget(picker)
    picker.set_categories(_categories(), leading_label="すべて")
    assert picker.display.text() == "すべて"
    picker.set_current_category_id(4)
    changed: list[object] = []
    picker.category_changed.connect(changed.append)
    _pick_with(monkeypatch, None)

    picker.open_dialog()

    assert picker.current_category_id() is None
    assert picker.display.text() == "すべて"
    assert changed == [None]


def test_picker_rebuild_preserves_selection_and_missing_id_clears(qtbot) -> None:
    picker = CategoryPicker()
    qtbot.addWidget(picker)
    picker.set_categories(_categories())
    picker.set_current_category_id(4)

    picker.set_categories([*_categories(), Category(5, 1, "照明", "LI", 1)])

    assert picker.current_category_id() == 4
    assert picker.category_count() == 5
    assert picker.display.text() == "備品 > LED電球"

    picker.set_current_category_id(999)

    assert picker.current_category_id() is None
    assert picker.display.text() == ""
