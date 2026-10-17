from __future__ import annotations

from pathlib import Path

from inventory_manager_mini.core.mobile_stocktake import encode_stocktake_payload
from inventory_manager_mini.core.models import (
    MobileStocktakeEntry,
    MobileStocktakePayload,
    NewItem,
)
from inventory_manager_mini.ui.dialogs.mobile_stocktake_dialog import MobileStocktakeDialog
from tests.conftest import create_test_context


def test_editing_result_after_preview_discards_confirmation(
    qtbot,
    seeded_conn,
    inventory,
    master,
    tmp_path: Path,
) -> None:
    item = inventory.create_item(
        NewItem(
            client_id=1,
            purchaser_id=1,
            name="棚卸対象",
            category_id=2,
            initial_quantity=2,
            initial_staff_id=1,
        )
    )
    sheet = inventory.export_mobile_stocktake_html(tmp_path / "sheet.html", 1, 1)
    result_text = encode_stocktake_payload(
        MobileStocktakePayload(
            session_id=sheet.session_id,
            client_id=sheet.client_id,
            entries=(MobileStocktakeEntry(item.id, 3, "現場"),),
        )
    )
    context = create_test_context(seeded_conn, inventory=inventory, master=master)
    dialog = MobileStocktakeDialog(context)
    qtbot.addWidget(dialog)

    dialog.result_edit.setPlainText(result_text)
    dialog.preview_button.click()

    assert dialog._preview is not None
    assert dialog._preview_raw_text == result_text
    assert dialog.confirm_button.isEnabled()
    assert dialog.preview_table.rowCount() == 1

    dialog.result_edit.appendPlainText("変更")

    assert dialog._preview is None
    assert dialog._preview_raw_text is None
    assert not dialog.confirm_button.isEnabled()
    assert dialog.preview_table.rowCount() == 0
