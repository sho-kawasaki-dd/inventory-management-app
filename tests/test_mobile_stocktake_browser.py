from __future__ import annotations

import base64
import json
import re
import zlib
from pathlib import Path

from playwright.sync_api import FloatRect, Page, sync_playwright

from inventory_manager_mini.core.mobile_stocktake import (
    MAX_NOTE_LENGTH,
    parse_stocktake_payload,
    render_stocktake_html,
)
from inventory_manager_mini.core.models import (
    MAX_STOCK_QUANTITY,
    MobileStocktakeEntry,
    MobileStocktakeItem,
    MobileStocktakePayload,
    MobileStocktakeSheet,
)
from inventory_manager_mini.core.timeutil import to_local

PROTOTYPE_HTML = Path(__file__).parent / "fixtures" / "mobile_stocktake_prototype.html"
SESSION_ID = "0123456789abcdef0123456789abcdef"


def decode_result_text(value: str) -> dict[str, object]:
    prefix, encoded_with_checksum, suffix = value.split(":", 2)
    encoded, checksum = encoded_with_checksum.rsplit(".", 1)
    assert prefix == "IMM1"
    assert suffix == "END"
    assert checksum == f"{zlib.crc32(encoded.encode('ascii')):08x}"
    return json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))


def _boxes_overlap(first: FloatRect, second: FloatRect) -> bool:
    horizontal = first["x"] < second["x"] + second["width"] - 0.5 and (
        second["x"] < first["x"] + first["width"] - 0.5
    )
    vertical = first["y"] < second["y"] + second["height"] - 0.5 and (
        second["y"] < first["y"] + first["height"] - 0.5
    )
    return horizontal and vertical


def _assert_reachable_without_header_overlap(
    page: Page, target_selector: str, viewport_height: int
) -> None:
    target = page.locator(target_selector)
    target.scroll_into_view_if_needed()
    header_box = page.locator("#app-header").bounding_box()
    target_box = target.bounding_box()
    assert header_box is not None
    assert target_box is not None
    assert not _boxes_overlap(header_box, target_box)
    assert target_box["y"] >= header_box["y"] + header_box["height"] - 1.0
    assert target_box["y"] + target_box["height"] <= viewport_height + 1.0


def _wait_for_storage_status(page: Page, status: str) -> None:
    page.wait_for_function(
        "(expected) => document.querySelector('#storage-status')?.dataset.status === expected",
        arg=status,
    )


def _seed_indexeddb_record(page: Page, record: dict[str, object]) -> None:
    page.evaluate(
        """async record => {
            const database = await new Promise((resolve, reject) => {
                const request = indexedDB.open("imm_stocktake", 1);
                request.onupgradeneeded = () => {
                    if (!request.result.objectStoreNames.contains("drafts")) {
                        request.result.createObjectStore("drafts", { keyPath: "session_id" });
                    }
                };
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
            try {
                await new Promise((resolve, reject) => {
                    const transaction = database.transaction("drafts", "readwrite");
                    transaction.objectStore("drafts").put(record);
                    transaction.oncomplete = resolve;
                    transaction.onabort = () => reject(transaction.error);
                    transaction.onerror = () => reject(transaction.error);
                });
            } finally {
                database.close();
            }
        }""",
        record,
    )


def _read_indexeddb_record(page: Page, session_id: str) -> dict[str, object] | None:
    return page.evaluate(
        """sessionId => new Promise((resolve, reject) => {
            const request = indexedDB.open("imm_stocktake", 1);
            request.onsuccess = () => {
                const database = request.result;
                const transaction = database.transaction("drafts", "readonly");
                const readRequest = transaction.objectStore("drafts").get(sessionId);
                let result;
                readRequest.onsuccess = () => { result = readRequest.result ?? null; };
                readRequest.onerror = () => reject(readRequest.error);
                transaction.oncomplete = () => {
                    database.close();
                    resolve(result);
                };
                transaction.onabort = () => reject(transaction.error);
            };
            request.onerror = () => reject(request.error);
        })""",
        session_id,
    )


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


def test_generated_html_round_trip_with_special_chars_and_boundaries(tmp_path: Path) -> None:
    sheet = MobileStocktakeSheet(
        session_id=SESSION_ID,
        client_id=7,
        client_name="A社 <現場 & テスト>",
        staff_id=3,
        staff_name="担当者 <山田>",
        started_at="2026-10-17 01:00:00",
        items=(
            MobileStocktakeItem(
                item_id=101,
                code="IT-001",
                name="耐切創手袋 </script><B>&\"'",
                manufacturer_part_number="GLV-<01>",
                location_name="棚A",
                book_quantity=15,
                unit="双",
            ),
            MobileStocktakeItem(
                item_id=102,
                code="IT-002",
                name="保護メガネ 🥽",
                manufacturer_part_number="GOG-02",
                location_name="棚B",
                book_quantity=5,
                unit="個",
            ),
            MobileStocktakeItem(
                item_id=103,
                code="IT-003",
                name="ウエス",
                manufacturer_part_number=None,
                location_name=None,
                book_quantity=8,
                unit="箱",
            ),
        ),
    )
    html_path = tmp_path / "stocktake_sheet.html"
    html_path.write_text(render_stocktake_html(sheet), encoding="utf-8")

    note_item1 = ' \u3000\x85\x1f日本語・絵文字✅\n2行目 & <タグ> "引用" \ufeff\t '
    note_item2_body = "😀" * (MAX_NOTE_LENGTH - 2) + "端A"
    note_item2_raw = f"\u3000 {note_item2_body} \t"

    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(html_path.as_uri())

        card1 = page.locator('[data-item-id="101"]')
        card2 = page.locator('[data-item-id="102"]')
        card3 = page.locator('[data-item-id="103"]')

        # ステップボタン (-10, -1, +1, +10) と 0 下限の確認
        card1.get_by_role("button", name="-10", exact=True).click()
        assert card1.get_by_label("IT-001 実数").input_value() == "5"
        card1.get_by_role("button", name="-10", exact=True).click()
        assert card1.get_by_label("IT-001 実数").input_value() == "0"
        card1.get_by_role("button", name="-1", exact=True).click()
        assert card1.get_by_label("IT-001 実数").input_value() == "0"
        card1.get_by_role("button", name="+10", exact=True).click()
        card1.get_by_role("button", name="+1", exact=True).click()
        assert card1.get_by_label("IT-001 実数").input_value() == "11"
        card1.get_by_label("IT-001 実数").fill("0")
        card1.get_by_label("IT-001 メモ").fill(note_item1)
        card1.get_by_role("button", name="この実数で確認").click()

        # 上限数量 (1,000,000) と 500 コードポイント境界のメモ
        card2.get_by_label("IT-002 実数").fill(str(MAX_STOCK_QUANTITY))
        card2.get_by_label("IT-002 メモ").fill(note_item2_raw)
        card2.get_by_role("button", name="この実数で確認").click()

        # フィルタ操作 (未確認・確認済・差異あり・保管場所・検索)
        page.get_by_role("button", name="未確認", exact=True).click()
        assert card1.is_hidden()
        assert card2.is_hidden()
        assert card3.is_visible()

        page.get_by_role("button", name="確認済", exact=True).click()
        assert card1.is_visible()
        assert card2.is_visible()
        assert card3.is_hidden()

        page.get_by_role("button", name="すべて", exact=True).click()
        page.get_by_label("保管場所").select_option("棚A")
        assert card1.is_visible()
        assert card2.is_hidden()
        page.get_by_label("保管場所").select_option("")

        page.get_by_label("キーワード検索").fill("GOG-02")
        assert card1.is_hidden()
        assert card2.is_visible()
        page.get_by_label("キーワード検索").fill("")

        # 3件目をワンタップ「帳簿通り」で確認 + 空メモ正規化
        card3.get_by_label("IT-003 メモ").fill("  \u3000\t  ")
        card3.get_by_role("button", name="✓ 帳簿通り (8 箱) で OK").click()

        assert page.locator("#progress-text").inner_text() == "確認済 3 / 3 件"
        page.get_by_role("button", name="差異あり", exact=True).click()
        assert card1.is_visible()
        assert card2.is_visible()
        assert card3.is_hidden()
        page.get_by_role("button", name="すべて", exact=True).click()

        # 結果テキストを表示して Python の parse_stocktake_payload で検証
        page.get_by_role("button", name="結果テキストを表示").click()
        result_text = page.get_by_label("結果テキスト").input_value()
        parsed_from_text = parse_stocktake_payload(result_text)

        expected_payload = MobileStocktakePayload(
            session_id=SESSION_ID,
            client_id=7,
            entries=(
                MobileStocktakeEntry(
                    item_id=101,
                    actual_quantity=0,
                    note=note_item1.strip(),
                ),
                MobileStocktakeEntry(
                    item_id=102,
                    actual_quantity=MAX_STOCK_QUANTITY,
                    note=note_item2_body,
                ),
                MobileStocktakeEntry(
                    item_id=103,
                    actual_quantity=8,
                    note=None,
                ),
            ),
        )
        assert parsed_from_text == expected_payload

        # JSON ファイルを保存して Python の parse_stocktake_payload で検証
        with page.expect_download() as download_info:
            page.get_by_role("button", name="💾 結果ファイル(.json)を保存").click()
        download = download_info.value
        assert download.suggested_filename.startswith("棚卸結果_")
        assert download.suggested_filename.endswith(".json")
        expected_stamp = to_local(sheet.started_at).strftime("%Y%m%d_%H%M%S")
        assert re.fullmatch(
            rf"棚卸結果_A社.*テスト.*_{expected_stamp}\.json", download.suggested_filename
        )
        assert not any(char in download.suggested_filename for char in '<>"/\\|?*')
        downloaded_text = Path(download.path()).read_text(encoding="utf-8")
        assert parse_stocktake_payload(downloaded_text) == expected_payload
        assert not page_errors
        browser.close()


def test_generated_html_confirmation_validation_restore_and_fallbacks(tmp_path: Path) -> None:
    sheet = MobileStocktakeSheet(
        session_id=SESSION_ID,
        client_id=2,
        client_name="B社",
        staff_id=1,
        staff_name="鈴木",
        started_at="2026-10-17 02:00:00",
        items=(
            MobileStocktakeItem(
                item_id=201,
                code="B-001",
                name="品目1",
                manufacturer_part_number=None,
                location_name="倉庫1",
                book_quantity=10,
                unit="個",
            ),
            MobileStocktakeItem(
                item_id=202,
                code="B-002",
                name="品目2",
                manufacturer_part_number=None,
                location_name="倉庫1",
                book_quantity=20,
                unit="個",
            ),
        ),
    )
    html_path = tmp_path / "sheet_validation.html"
    html_path.write_text(render_stocktake_html(sheet), encoding="utf-8")

    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(html_path.as_uri())

        card1 = page.locator('[data-item-id="201"]')
        card2 = page.locator('[data-item-id="202"]')
        copy_btn = page.get_by_role("button", name="📋 結果テキストをコピー(IMM1:...)")
        save_btn = page.get_by_role("button", name="💾 結果ファイル(.json)を保存")
        show_btn = page.get_by_role("button", name="結果テキストを表示")

        # 初期状態はすべて未確認で出力ボタンはすべて disabled
        assert copy_btn.is_disabled()
        assert save_btn.is_disabled()
        assert show_btn.is_disabled()

        # 未確認フィルタ中に数量・メモを入力しても未確認のままでカードが消えないこと
        page.get_by_role("button", name="未確認", exact=True).click()
        card1.get_by_label("B-001 実数").fill("9")
        card1.get_by_label("B-001 メモ").fill("入力中メモ")
        assert card1.is_visible()
        page.get_by_role("button", name="すべて", exact=True).click()

        # 不正入力: 空欄・小数・負数・上限超過・501文字メモで確認不可
        for invalid_qty in ["", "1.5", "-1", str(MAX_STOCK_QUANTITY + 1)]:
            card1.get_by_label("B-001 実数").fill(invalid_qty)
            assert card1.get_by_role("button", name="この実数で確認").is_disabled()
            assert card1.locator(".item-error").is_visible()

        card1.get_by_label("B-001 実数").fill("8")
        card1.get_by_label("B-001 メモ").fill("😀" * (MAX_NOTE_LENGTH + 1))
        assert card1.get_by_role("button", name="この実数で確認").is_disabled()
        assert card1.locator(".item-error").is_visible()

        # 正常入力に戻して両方確認済みにすると出力ボタンが活性化
        card1.get_by_label("B-001 メモ").fill("正常メモ")
        card1.get_by_role("button", name="この実数で確認").click()
        card2.get_by_role("button", name="✓ 帳簿通り (20 個) で OK").click()
        assert copy_btn.is_enabled()
        assert save_btn.is_enabled()
        assert show_btn.is_enabled()

        # 結果テキストを表示した後、確認解除や数量変更で未確認に戻り古い結果テキストが消えること
        show_btn.click()
        assert page.get_by_label("結果テキスト").is_visible()
        card1.get_by_role("button", name="確認解除").click()
        assert copy_btn.is_disabled()
        assert page.get_by_label("結果テキスト").is_hidden()
        assert page.get_by_label("結果テキスト").input_value() == ""

        # 再確認後、メモ変更でも自動的に未確認へ戻ること
        card1.get_by_role("button", name="この実数で確認").click()
        assert copy_btn.is_enabled()
        card1.get_by_label("B-001 メモ").fill("変更後メモ")
        assert copy_btn.is_disabled()
        card1.get_by_role("button", name="この実数で確認").click()
        assert copy_btn.is_enabled()

        # リロードによる状態復元
        _wait_for_storage_status(page, "saved")
        page.reload()
        _wait_for_storage_status(page, "saved")
        assert page.locator('[data-item-id="201"]').get_by_label("B-001 実数").input_value() == "8"
        assert (
            page.locator('[data-item-id="201"]').get_by_label("B-001 メモ").input_value()
            == "変更後メモ"
        )
        assert copy_btn.is_enabled()

        # コピー不可時に自動で結果テキストが全選択表示され案内が出ること
        page.evaluate(
            """() => {
                Object.defineProperty(navigator, 'clipboard', {
                    value: { writeText: () => Promise.reject(new Error('Clipboard blocked')) },
                    configurable: true
                });
                document.execCommand = () => false;
            }"""
        )
        copy_btn.click()
        assert page.get_by_label("結果テキスト").is_visible()
        assert "長押しでコピーしてください" in page.locator("#result-feedback").inner_text()
        assert (
            parse_stocktake_payload(page.get_by_label("結果テキスト").input_value()).client_id == 2
        )

        # ダウンロードが無反応でも手動で「結果テキストを表示」から取得できること
        page.get_by_role("button", name="初期状態にリセット").click()
        assert copy_btn.is_disabled()
        page.locator('[data-item-id="201"]').get_by_role("button", name="この実数で確認").click()
        page.locator('[data-item-id="202"]').get_by_role("button", name="この実数で確認").click()
        page.evaluate("() => { HTMLAnchorElement.prototype.click = function() {}; }")
        save_btn.click()
        show_btn.click()
        assert page.get_by_label("結果テキスト").is_visible()
        assert (
            parse_stocktake_payload(page.get_by_label("結果テキスト").input_value()).client_id == 2
        )

        # 保存処理で例外が発生した際は自動で結果テキストが表示されること
        page.locator('[data-item-id="201"]').get_by_role("button", name="確認解除").click()
        page.locator('[data-item-id="201"]').get_by_role("button", name="この実数で確認").click()
        assert page.get_by_label("結果テキスト").is_hidden()
        page.evaluate(
            "() => { window.URL.createObjectURL = () => { throw new Error('Blob failed'); }; }"
        )
        save_btn.click()
        assert page.get_by_label("結果テキスト").is_visible()
        assert "長押しでコピーしてください" in page.locator("#result-feedback").inner_text()
        assert (
            parse_stocktake_payload(page.get_by_label("結果テキスト").input_value()).client_id == 2
        )

        # 旧 localStorage の保存データが存在しても新方式の復元に影響しないこと
        storage_key = f"imm_stocktake_{SESSION_ID}"
        page.evaluate(
            "key => localStorage.setItem(key, '{not json}')",
            storage_key,
        )
        page.reload()
        _wait_for_storage_status(page, "saved")
        assert page.locator("#restore-warning").is_hidden()
        assert copy_btn.is_enabled()
        assert page.locator('[data-item-id="201"]').get_by_label("B-001 実数").input_value() == "10"

        # localStorage が利用できない環境でも IndexedDB の保存・復元を行えること
        page2 = context.new_page()
        page2.on("pageerror", lambda error: page_errors.append(str(error)))
        page2.add_init_script(
            """Storage.prototype.getItem = function() { throw new Error("SecurityError"); };
            Storage.prototype.setItem = function() { throw new Error("SecurityError"); };"""
        )
        page2.goto(html_path.as_uri())
        _wait_for_storage_status(page2, "saved")
        assert page2.locator("#storage-warning").is_hidden()
        assert (
            page2.locator('[data-item-id="201"]').get_by_label("B-001 実数").input_value() == "10"
        )
        assert not page_errors
        browser.close()


def test_generated_html_500_items_layout_across_viewports(tmp_path: Path) -> None:
    items = tuple(
        MobileStocktakeItem(
            item_id=index,
            code=f"ITM-{index:04d}",
            name=f"テスト備品 {index} 番 (長めの品名サンプル)",
            manufacturer_part_number=f"MPN-{index:04d}",
            location_name=f"棚{(index % 5) + 1}",
            book_quantity=index % 50,
            unit="個",
        )
        for index in range(1, 501)
    )
    sheet = MobileStocktakeSheet(
        session_id=SESSION_ID,
        client_id=10,
        client_name="大規模クライアント株式会社",
        staff_id=1,
        staff_name="佐藤",
        started_at="2026-10-17 03:00:00",
        items=items,
    )
    html_path = tmp_path / "sheet_500.html"
    html_path.write_text(render_stocktake_html(sheet), encoding="utf-8")

    viewports = [
        ("desktop", 1280, 800),
        ("mobile_portrait", 390, 844),
        ("mobile_landscape", 844, 390),
    ]

    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for label, width, height in viewports:
            context = browser.new_context(viewport={"width": width, "height": height})
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(html_path.as_uri())

            # 初期表示のスクリーンショットとヘッダ・先頭カードの非重複検証
            screenshot_path = tmp_path / f"layout_{label}.png"
            page.screenshot(path=str(screenshot_path))
            assert screenshot_path.exists()

            _assert_reachable_without_header_overlap(page, '[data-item-id="1"]', height)
            _assert_reachable_without_header_overlap(
                page, '[data-item-id="1"] .input-actual', height
            )
            _assert_reachable_without_header_overlap(page, '[data-item-id="250"]', height)
            _assert_reachable_without_header_overlap(page, '[data-item-id="500"]', height)
            _assert_reachable_without_header_overlap(
                page, '[data-item-id="500"] .input-note', height
            )
            _assert_reachable_without_header_overlap(page, "#completion-panel", height)
            _assert_reachable_without_header_overlap(page, "#show-result", height)
            context.close()

        assert not page_errors
        browser.close()


def _write_small_sheet(tmp_path: Path) -> Path:
    sheet = MobileStocktakeSheet(
        session_id=SESSION_ID,
        client_id=2,
        client_name="B社",
        staff_id=1,
        staff_name="鈴木",
        started_at="2026-10-17 02:00:00",
        items=(
            MobileStocktakeItem(
                item_id=201,
                code="B-001",
                name="品目1",
                manufacturer_part_number=None,
                location_name="倉庫1",
                book_quantity=10,
                unit="個",
            ),
            MobileStocktakeItem(
                item_id=202,
                code="B-002",
                name="品目2",
                manufacturer_part_number=None,
                location_name="倉庫1",
                book_quantity=20,
                unit="個",
            ),
        ),
    )
    html_path = tmp_path / "small_sheet.html"
    html_path.write_text(render_stocktake_html(sheet), encoding="utf-8")
    return html_path


def test_generated_html_indexeddb_minimum_round_trip(tmp_path: Path) -> None:
    html_path = _write_small_sheet(tmp_path)
    expected_record = {
        "session_id": SESSION_ID,
        "storage_revision": 1,
        "draft": {
            "kind": "imm-stocktake-draft",
            "v": 1,
            "session_id": SESSION_ID,
            "client_id": 2,
            "started_at": "2026-10-17 02:00:00",
            "items": [
                {
                    "item_id": 201,
                    "book_quantity": 10,
                    "actual": "7",
                    "note": "先行実証",
                    "confirmed": True,
                    "badInput": False,
                },
                {
                    "item_id": 202,
                    "book_quantity": 20,
                    "actual": "",
                    "note": "",
                    "confirmed": False,
                    "badInput": False,
                },
            ],
        },
    }
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(html_path.as_uri())

        transaction_completed = page.evaluate(
            """async record => {
                const database = await new Promise((resolve, reject) => {
                    const request = indexedDB.open("imm_stocktake", 1);
                    request.onupgradeneeded = () => {
                        request.result.createObjectStore("drafts", { keyPath: "session_id" });
                    };
                    request.onsuccess = () => resolve(request.result);
                    request.onerror = () => reject(request.error);
                });
                try {
                    await new Promise((resolve, reject) => {
                        const transaction = database.transaction("drafts", "readwrite");
                        transaction.objectStore("drafts").put(record);
                        transaction.oncomplete = () => resolve(true);
                        transaction.onerror = () => reject(transaction.error);
                        transaction.onabort = () => reject(transaction.error);
                    });
                    return true;
                } finally {
                    database.close();
                }
            }""",
            expected_record,
        )
        assert transaction_completed is True

        page.reload()
        restored_record = page.evaluate(
            """sessionId => new Promise((resolve, reject) => {
                const request = indexedDB.open("imm_stocktake", 1);
                request.onsuccess = () => {
                    const database = request.result;
                    const transaction = database.transaction("drafts", "readonly");
                    const readRequest = transaction.objectStore("drafts").get(sessionId);
                    readRequest.onsuccess = () => resolve(readRequest.result);
                    readRequest.onerror = () => reject(readRequest.error);
                    transaction.onabort = () => reject(transaction.error);
                };
                request.onerror = () => reject(request.error);
            })""",
            SESSION_ID,
        )

        assert page.url == html_path.as_uri()
        assert restored_record == expected_record
        assert not page_errors
        context.close()
        browser.close()


def test_generated_html_indexeddb_auto_save_and_resume(tmp_path: Path) -> None:
    html_path = _write_small_sheet(tmp_path)
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(html_path.as_uri())
        _wait_for_storage_status(page, "ready")

        card = page.locator('[data-item-id="201"]')
        card.get_by_label("B-001 実数").fill("7")
        card.get_by_label("B-001 メモ").fill("自動保存")
        card.get_by_role("button", name="この実数で確認").click()
        _wait_for_storage_status(page, "saved")

        record = _read_indexeddb_record(page, SESSION_ID)
        assert record is not None
        assert record["storage_revision"] == 3
        draft = record["draft"]
        assert isinstance(draft, dict)
        assert draft["kind"] == "imm-stocktake-draft"
        assert draft["client_id"] == 2
        assert draft["started_at"] == "2026-10-17 02:00:00"
        items = draft["items"]
        assert isinstance(items, list)
        assert items[0] == {
            "item_id": 201,
            "book_quantity": 10,
            "actual": "7",
            "note": "自動保存",
            "confirmed": True,
            "badInput": False,
        }

        page.reload()
        _wait_for_storage_status(page, "saved")
        card = page.locator('[data-item-id="201"]')
        assert card.get_by_label("B-001 実数").input_value() == "7"
        assert card.get_by_label("B-001 メモ").input_value() == "自動保存"
        assert card.locator(".status-badge").inner_text() == "確認済"
        assert card.get_by_role("button", name="確認解除").is_enabled()
        assert not page_errors
        browser.close()


def test_generated_html_indexeddb_failure_keeps_input_and_retries(tmp_path: Path) -> None:
    html_path = _write_small_sheet(tmp_path)
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """window.__nativeIndexedDB = window.indexedDB;
            Object.defineProperty(window, "indexedDB", {
                configurable: true,
                get() { throw new Error("IndexedDB denied"); }
            });"""
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(html_path.as_uri())
        _wait_for_storage_status(page, "error")
        assert page.locator("#storage-warning").is_visible()
        assert not page.locator("#items-container").evaluate("(element) => element.inert")

        actual = page.locator('[data-item-id="201"]').get_by_label("B-001 実数")
        actual.fill("6")
        assert actual.input_value() == "6"
        assert page.locator("#storage-warning").is_visible()

        page.evaluate(
            """() => Object.defineProperty(window, "indexedDB", {
                configurable: true,
                value: window.__nativeIndexedDB
            })"""
        )
        page.get_by_role("button", name="再試行").click()
        _wait_for_storage_status(page, "saved")
        assert page.locator("#storage-warning").is_hidden()
        record = _read_indexeddb_record(page, SESSION_ID)
        assert record is not None
        draft = record["draft"]
        assert isinstance(draft, dict)
        assert isinstance(draft["items"], list)
        assert draft["items"][0]["actual"] == "6"
        assert not page_errors
        browser.close()


def test_generated_html_disables_input_until_initial_read_finishes(tmp_path: Path) -> None:
    html_path = _write_small_sheet(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.add_init_script(
            """(() => {
                const originalOpen = IDBFactory.prototype.open;
                IDBFactory.prototype.open = function(...args) {
                    const request = originalOpen.apply(this, args);
                    request.addEventListener("success", event => {
                        event.stopImmediatePropagation();
                        window.__heldStorageOpen = request;
                    }, { once: true });
                    return request;
                };
                window.__releaseStorageOpen = () => {
                    const request = window.__heldStorageOpen;
                    if (!request || typeof request.onsuccess !== "function") return false;
                    request.onsuccess.call(request, new Event("success"));
                    return true;
                };
            })();"""
        )
        page.goto(html_path.as_uri())
        page.wait_for_function("() => window.__heldStorageOpen !== undefined")
        assert page.locator("#storage-status").get_attribute("data-status") == "loading"
        assert page.locator("#items-container").evaluate("(element) => element.inert")
        assert page.locator("#reset-state").is_disabled()

        assert page.evaluate("() => window.__releaseStorageOpen()") is True
        _wait_for_storage_status(page, "ready")
        assert not page.locator("#items-container").evaluate("(element) => element.inert")
        assert page.locator("#reset-state").is_enabled()
        browser.close()


def test_generated_html_detects_other_tab_revision_conflict(tmp_path: Path) -> None:
    html_path = _write_small_sheet(tmp_path)
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        first_page = context.new_page()
        second_page = context.new_page()
        first_page.on("pageerror", lambda error: page_errors.append(str(error)))
        second_page.on("pageerror", lambda error: page_errors.append(str(error)))
        first_page.goto(html_path.as_uri())
        _wait_for_storage_status(first_page, "ready")
        second_page.goto(html_path.as_uri())
        _wait_for_storage_status(second_page, "ready")

        first_page.locator('[data-item-id="201"]').get_by_label("B-001 実数").fill("5")
        _wait_for_storage_status(first_page, "saved")
        second_page.locator('[data-item-id="201"]').get_by_label("B-001 実数").fill("6")
        second_page.locator("#storage-resolution").wait_for(state="visible")
        assert second_page.locator("#storage-warning").is_visible()
        assert (
            second_page.locator('[data-item-id="201"]').get_by_label("B-001 実数").input_value()
            == "6"
        )

        record = _read_indexeddb_record(first_page, SESSION_ID)
        assert record is not None
        assert isinstance(record["draft"], dict)
        assert record["draft"]["items"][0]["actual"] == "5"

        second_page.get_by_role("button", name="保存データを読み込む").click()
        _wait_for_storage_status(second_page, "saved")
        assert (
            second_page.locator('[data-item-id="201"]').get_by_label("B-001 実数").input_value()
            == "5"
        )
        assert not page_errors
        context.close()
        browser.close()


def test_generated_html_copy_success_and_missing_clipboard(tmp_path: Path) -> None:
    html_path = _write_small_sheet(tmp_path)
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_context().new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(html_path.as_uri())

        page.locator('[data-item-id="201"]').get_by_label("B-001 実数").fill("7")
        page.locator('[data-item-id="201"]').get_by_label("B-001 メモ").fill("コピー確認")
        page.locator('[data-item-id="201"]').get_by_role("button", name="この実数で確認").click()
        page.locator('[data-item-id="202"]').get_by_role(
            "button", name="✓ 帳簿通り (20 個) で OK"
        ).click()
        copy_btn = page.get_by_role("button", name="📋 結果テキストをコピー(IMM1:...)")
        result_box = page.get_by_label("結果テキスト")

        page.evaluate(
            """() => {
                window.__copied = null;
                Object.defineProperty(navigator, 'clipboard', {
                    value: {
                        writeText: text => { window.__copied = text; return Promise.resolve(); }
                    },
                    configurable: true
                });
            }"""
        )
        copy_btn.click()
        page.wait_for_function("() => window.__copied !== null")
        copied = page.evaluate("() => window.__copied")
        assert parse_stocktake_payload(copied) == MobileStocktakePayload(
            session_id=SESSION_ID,
            client_id=2,
            entries=(
                MobileStocktakeEntry(item_id=201, actual_quantity=7, note="コピー確認"),
                MobileStocktakeEntry(item_id=202, actual_quantity=20, note=None),
            ),
        )
        page.wait_for_function(
            "() => document.getElementById('result-feedback')"
            ".textContent.includes('コピーしました')"
        )
        assert result_box.is_hidden()

        # navigator.clipboard が使えない環境でも execCommand でコピーできること
        page.evaluate(
            """() => {
                window.__legacy_copied = null;
                Object.defineProperty(navigator, 'clipboard', {
                    value: undefined, configurable: true
                });
                document.execCommand = command => {
                    const active = document.activeElement;
                    window.__legacy_copied = command === 'copy' ? active.value : null;
                    return true;
                };
            }"""
        )
        copy_btn.click()
        legacy_copied = page.evaluate("() => window.__legacy_copied")
        assert parse_stocktake_payload(legacy_copied).entries[0].actual_quantity == 7
        assert "コピーしました" in page.locator("#result-feedback").inner_text()
        assert result_box.is_hidden()

        # 代替コピーも失敗する環境
        page.evaluate("() => { document.execCommand = () => false; }")
        copy_btn.click()
        assert result_box.is_visible()
        assert "直接コピーできない" in page.locator("#result-feedback").inner_text()
        assert parse_stocktake_payload(result_box.input_value()).entries[0].actual_quantity == 7
        assert not page_errors
        browser.close()


def test_generated_html_rejects_invalid_saved_states_and_restores_valid_one(
    tmp_path: Path,
) -> None:
    html_path = _write_small_sheet(tmp_path)
    valid_items: list[dict[str, object]] = [
        {
            "item_id": 201,
            "book_quantity": 10,
            "actual": "7",
            "note": "",
            "confirmed": True,
            "badInput": False,
        },
        {
            "item_id": 202,
            "book_quantity": 20,
            "actual": "20",
            "note": "",
            "confirmed": True,
            "badInput": False,
        },
    ]
    valid_draft: dict[str, object] = {
        "kind": "imm-stocktake-draft",
        "v": 1,
        "session_id": SESSION_ID,
        "client_id": 2,
        "started_at": "2026-10-17 02:00:00",
        "items": valid_items,
    }

    def wrapped(**overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "session_id": SESSION_ID,
            "storage_revision": 1,
            "draft": valid_draft,
        }
        data.update(overrides)
        return data

    def changed_item(index: int, **overrides: object) -> list[dict[str, object]]:
        items = [dict(item) for item in valid_items]
        items[index].update(overrides)
        return items

    def changed_draft(**overrides: object) -> dict[str, object]:
        draft = dict(valid_draft)
        draft.update(overrides)
        return wrapped(draft=draft)

    invalid_values = {
        "version": changed_draft(v=2),
        "kind": changed_draft(kind="imm-stocktake-result"),
        "session": changed_draft(session_id="f" * 32),
        "client": changed_draft(client_id=3),
        "started_at": changed_draft(started_at="2026-10-18 02:00:00"),
        "items_not_array": changed_draft(items={}),
        "missing_item": changed_draft(items=valid_items[:1]),
        "extra_item": changed_draft(items=[*valid_items, dict(valid_items[0], item_id=203)]),
        "duplicate_item": changed_draft(items=[valid_items[0], dict(valid_items[1], item_id=201)]),
        "book_quantity": changed_draft(items=changed_item(0, book_quantity=11)),
        "confirmed_not_bool": changed_draft(items=changed_item(0, confirmed="true")),
        "bad_input_not_bool": changed_draft(items=changed_item(0, badInput="false")),
        "actual_not_string": changed_draft(items=changed_item(0, actual=7)),
        "negative_actual": changed_draft(items=changed_item(0, actual="-1")),
        "note_too_long": changed_draft(items=changed_item(0, note="😀" * (MAX_NOTE_LENGTH + 1))),
        "revision": wrapped(storage_revision=0),
    }
    page_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        copy_btn = page.get_by_role("button", name="📋 結果テキストをコピー(IMM1:...)")
        card = page.locator('[data-item-id="201"]')
        page.goto(html_path.as_uri())
        _wait_for_storage_status(page, "ready")

        for name, record in invalid_values.items():
            _seed_indexeddb_record(page, record)
            page.reload()
            _wait_for_storage_status(page, "error")
            assert page.locator("#restore-warning").is_visible(), name
            assert copy_btn.is_disabled(), name
            assert card.get_by_label("B-001 実数").input_value() == "10", name
            assert card.get_by_label("B-001 メモ").input_value() == "", name
            assert _read_indexeddb_record(page, SESSION_ID) == record, name

        # 正常な保存値は確認状態・数量・メモ(前後の Unicode 空白を含む)のまま復元される
        restored_note = "  \u3000復元メモ \t"
        valid_items[0]["note"] = restored_note
        valid_record = wrapped(draft={**valid_draft, "items": valid_items})
        _seed_indexeddb_record(page, valid_record)
        page.reload()
        _wait_for_storage_status(page, "saved")
        assert page.locator("#restore-warning").is_hidden()
        assert copy_btn.is_enabled()
        assert card.get_by_label("B-001 実数").input_value() == "7"
        assert card.get_by_label("B-001 メモ").input_value() == restored_note
        page.get_by_role("button", name="結果テキストを表示").click()
        parsed = parse_stocktake_payload(page.get_by_label("結果テキスト").input_value())
        assert [(entry.actual_quantity, entry.note) for entry in parsed.entries] == [
            (7, "復元メモ"),
            (20, None),
        ]
        assert not page_errors
        context.close()
        browser.close()
