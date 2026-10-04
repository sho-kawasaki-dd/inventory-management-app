from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from inventory_manager_mini.core.models import Category
from inventory_manager_mini.ui.widgets.category_combo import CategoryComboBox


def _categories() -> list[Category]:
    return [
        Category(4, 1, "LED電球", "LE", 1),
        Category(3, None, "工具", "TL", 1),
        Category(2, 1, "文具", "ST", 1),
        Category(1, None, "備品", "TO", 1),
    ]


def test_builds_tree_and_sorts_each_level(qtbot) -> None:
    combo = CategoryComboBox()
    qtbot.addWidget(combo)

    combo.set_categories(_categories())

    assert [combo._model.item(row).text() for row in range(combo._model.rowCount())] == [
        "備品",
        "工具",
    ]
    supplies = combo._model.item(0)
    assert not supplies.isEditable()
    assert [supplies.child(row).text() for row in range(supplies.rowCount())] == [
        "LED電球",
        "文具",
    ]


def test_selecting_descendant_updates_full_path_and_signal(qtbot) -> None:
    combo = CategoryComboBox()
    qtbot.addWidget(combo)
    combo.set_categories(_categories())
    changed: list[object] = []
    combo.category_changed.connect(changed.append)
    child_index = combo._model.item(0).child(0).index()

    combo._select_index(child_index)

    assert combo.current_category_id() == 4
    assert combo.currentText() == "備品 > LED電球"
    assert combo.toolTip() == "備品 > LED電球"
    assert changed == [4]


def test_leading_label_has_none_id_and_can_be_selected(qtbot) -> None:
    combo = CategoryComboBox()
    qtbot.addWidget(combo)

    combo.set_categories(_categories(), leading_label="すべて")
    leading_item: QStandardItem = combo._model.item(0)

    assert leading_item.data(Qt.ItemDataRole.UserRole) is None
    assert combo.current_category_id() is None
    assert combo.currentText() == "すべて"


def test_rebuild_preserves_selected_id_when_it_still_exists(qtbot) -> None:
    combo = CategoryComboBox()
    qtbot.addWidget(combo)
    combo.set_categories(_categories())
    combo.set_current_category_id(4)

    combo.set_categories([*_categories(), Category(5, 1, "照明", "LI", 1)])

    assert combo.current_category_id() == 4
    assert combo.currentText() == "備品 > LED電球"


def test_missing_id_clears_selection(qtbot) -> None:
    combo = CategoryComboBox()
    qtbot.addWidget(combo)
    combo.set_categories(_categories())
    combo.set_current_category_id(999)

    assert combo.current_category_id() is None
    assert combo.currentText() == ""


def test_branch_click_expands_without_closing_popup(qtbot) -> None:
    combo = CategoryComboBox()
    qtbot.addWidget(combo)
    combo.resize(240, 32)
    combo.set_categories(_categories())
    combo.show()
    combo.showPopup()
    qtbot.waitUntil(combo._view.isVisible)
    index = combo._model.item(0).index()
    option = QStyleOptionViewItem()
    combo._view.initViewItemOption(option)
    option.rect = combo._view.visualRect(index)
    option.index = index
    branch_rect = combo._view.style().subElementRect(
        QStyle.SubElement.SE_TreeViewDisclosureItem, option, combo._view
    )
    branch_position = branch_rect.center()
    assert combo._view.indexAt(branch_position) == index, (branch_rect, branch_position)
    assert combo._is_branch_click(index, branch_position), (branch_rect, branch_position)

    qtbot.mouseClick(combo._view.viewport(), Qt.MouseButton.LeftButton, pos=branch_position)

    assert combo._view.isExpanded(index)
    assert combo._view.isVisible()
