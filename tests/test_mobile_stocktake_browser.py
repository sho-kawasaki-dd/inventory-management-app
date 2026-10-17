from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path

from playwright.sync_api import sync_playwright

PROTOTYPE_HTML = Path(__file__).parent / "fixtures" / "mobile_stocktake_prototype.html"


def decode_result_text(value: str) -> dict[str, object]:
    prefix, encoded_with_checksum, suffix = value.split(":", 2)
    encoded, checksum = encoded_with_checksum.rsplit(".", 1)
    assert prefix == "IMM1"
    assert suffix == "END"
    assert checksum == f"{zlib.crc32(encoded.encode('ascii')):08x}"
    return json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))


def test_local_html_input_resume_and_result_round_trip() -> None:
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(PROTOTYPE_HTML.as_uri())

        first_card = page.locator('[data-item-id="101"]')
        first_card.get_by_label("DEMO-001 実数").fill("3")
        first_card.get_by_label("DEMO-001 メモ").fill("現場確認 ✅")
        first_card.get_by_role("button", name="この実数で確認").click()
        assert page.get_by_role("button", name="結果テキストを表示").is_disabled()

        page.reload()
        first_card = page.locator('[data-item-id="101"]')
        assert first_card.get_by_label("DEMO-001 実数").input_value() == "3"
        assert first_card.get_by_label("DEMO-001 メモ").input_value() == "現場確認 ✅"
        assert first_card.get_by_role("button", name="確認済み").is_enabled()

        page.locator('[data-item-id="102"]').get_by_role("button", name="この実数で確認").click()
        page.get_by_role("button", name="結果テキストを表示").click()
        result_text = page.get_by_label("結果テキスト").input_value()
        payload = decode_result_text(result_text)
        assert payload == {
            "v": 1,
            "session_id": "a0-prototype-session-000000000000",
            "client_id": 1,
            "entries": [
                {"item_id": 101, "actual_quantity": 3, "note": "現場確認 ✅"},
                {"item_id": 102, "actual_quantity": 12, "note": None},
            ],
        }

        with page.expect_download() as download_info:
            page.get_by_role("button", name="結果 JSON を保存").click()
        downloaded = Path(download_info.value.path()).read_text(encoding="utf-8")
        assert json.loads(downloaded) == payload
        assert not page_errors
        browser.close()
