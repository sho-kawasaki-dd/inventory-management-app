from pathlib import Path

import pytest

from inventory_manager_mini.core.models import REASON_LABELS, Reason
from inventory_manager_mini.ui.context import AppContext
from inventory_manager_mini.ui.dialogs.stock_move_dialog import StockMoveDialog
from inventory_manager_mini.ui.signals import DataBus


@pytest.mark.parametrize("reason", list(Reason))
def test_reason_combo_uses_shared_labels_and_initial_reason(
    qtbot, reason: Reason, inventory, master, settings
) -> None:
    context = AppContext(
        inventory=inventory,
        master=master,
        settings=settings,
        data_bus=DataBus(),
        db_path=Path(":memory:"),
        schema_version=3,
        app_version="0.1.0",
    )
    dialog = StockMoveDialog(context, reason)
    qtbot.addWidget(dialog)

    assert dialog.reason_combo.count() == len(Reason)
    assert [dialog.reason_combo.itemText(index) for index in range(len(Reason))] == [
        REASON_LABELS[value] for value in Reason
    ]
    assert dialog.reason_combo.currentData() == reason
