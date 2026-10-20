from __future__ import annotations

import base64
import json
import re
import zlib

import pytest

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.mobile_stocktake import (
    MAX_NOTE_LENGTH,
    RESULT_CODE_PREFIX,
    RESULT_CODE_SUFFIX,
    dump_stocktake_payload_json,
    encode_stocktake_payload,
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

VALID_SESSION_ID = "0123456789abcdef0123456789abcdef"
SECOND_SESSION_ID = "fedcba9876543210fedcba9876543210"


def _sample_payload() -> MobileStocktakePayload:
    return MobileStocktakePayload(
        session_id=VALID_SESSION_ID,
        client_id=1,
        entries=(
            MobileStocktakeEntry(item_id=101, actual_quantity=3, note="現場確認 ✅"),
            MobileStocktakeEntry(item_id=102, actual_quantity=0, note=None),
        ),
    )


def _sample_sheet() -> MobileStocktakeSheet:
    return MobileStocktakeSheet(
        session_id=VALID_SESSION_ID,
        client_id=1,
        client_name="A社 <テスト & Co.>",
        staff_id=2,
        staff_name="山田 <太郎> & Co.",
        started_at="2026-10-17 00:30:00",
        items=(
            MobileStocktakeItem(
                item_id=101,
                code="ITEM-001",
                name="耐切創手袋 </script><script>alert(1)</script> & 軍手\u2028\u2029",
                manufacturer_part_number="GLV-<100>&A",
                location_name="第1倉庫 <A棚>",
                book_quantity=5,
                unit="双",
            ),
            MobileStocktakeItem(
                item_id=102,
                code="ITEM-002",
                name="アルコール除菌スプレー",
                manufacturer_part_number=None,
                location_name=None,
                book_quantity=12,
                unit="本",
            ),
        ),
    )


def _make_imm1_from_bytes(raw_bytes: bytes) -> str:
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    crc = f"{zlib.crc32(encoded.encode('ascii')) & 0xFFFFFFFF:08x}"
    return f"{RESULT_CODE_PREFIX}{encoded}.{crc}{RESULT_CODE_SUFFIX}"


def test_encode_and_parse_imm1_code_round_trip() -> None:
    payload = _sample_payload()
    code = encode_stocktake_payload(payload)

    assert code.startswith(RESULT_CODE_PREFIX)
    assert code.endswith(RESULT_CODE_SUFFIX)
    assert parse_stocktake_payload(code) == payload


def test_dump_and_parse_json_round_trip() -> None:
    payload = _sample_payload()
    json_text = dump_stocktake_payload_json(payload)

    parsed_dict = json.loads(json_text)
    assert parsed_dict["v"] == 1
    assert parsed_dict["session_id"] == VALID_SESSION_ID
    assert parsed_dict["client_id"] == 1
    assert parse_stocktake_payload(json_text) == payload
    assert parse_stocktake_payload("\ufeff  " + json_text + "\n") == payload


def test_parse_extracts_imm1_with_surrounding_text_wrapping_and_quotes() -> None:
    payload = _sample_payload()
    code = encode_stocktake_payload(payload)

    chunks = [code[i : i + 24] for i in range(0, len(code), 24)]
    quoted_wrapped = "\n".join(
        f"{'> ' * ((idx % 3) + 1)}{chunk}" for idx, chunk in enumerate(chunks)
    )
    email_body = (
        "お疲れ様です。A社の月次棚卸を実施しました。\r\n"
        "以下が結果テキストです。\r\n\r\n"
        f"{quoted_wrapped}\r\n\r\n"
        "> よろしくお願いいたします。\r\n"
    )

    assert parse_stocktake_payload(email_body) == payload


def test_parse_extracts_json_with_surrounding_text() -> None:
    payload = _sample_payload()
    json_text = dump_stocktake_payload_json(payload)
    wrapped = f"棚卸結果ファイルを貼り付けます:\n{json_text}\n以上です。"

    assert parse_stocktake_payload(wrapped) == payload


def test_parse_accepts_identical_duplicate_imm1_blocks_in_quoted_reply() -> None:
    payload = _sample_payload()
    code = encode_stocktake_payload(payload)
    quoted = "\n".join(f">> {code[i : i + 30]}" for i in range(0, len(code), 30))

    reply_text = f"再送します。\n{code}\n\nOn 2026-10-17 wrote:\n{quoted}\n"
    assert parse_stocktake_payload(reply_text) == payload


def test_parse_rejects_multiple_different_imm1_blocks() -> None:
    first = encode_stocktake_payload(_sample_payload())
    second_payload = MobileStocktakePayload(
        session_id=VALID_SESSION_ID,
        client_id=1,
        entries=(
            MobileStocktakeEntry(item_id=101, actual_quantity=4, note="修正後"),
            MobileStocktakeEntry(item_id=102, actual_quantity=0, note=None),
        ),
    )
    second = encode_stocktake_payload(second_payload)
    other_session = encode_stocktake_payload(
        MobileStocktakePayload(
            session_id=SECOND_SESSION_ID,
            client_id=1,
            entries=(MobileStocktakeEntry(item_id=101, actual_quantity=3, note=None),),
        )
    )

    with pytest.raises(
        ValidationError,
        match="複数の棚卸結果が含まれています。取り込む結果を1件だけ貼り付けてください",
    ):
        parse_stocktake_payload(f"{first}\n\n> {second}")

    with pytest.raises(
        ValidationError,
        match="複数の棚卸結果が含まれています。取り込む結果を1件だけ貼り付けてください",
    ):
        parse_stocktake_payload(f"{first}\n{other_session}")


@pytest.mark.parametrize(
    "corrupted_factory",
    [
        lambda valid: f"{valid[:-4]}\n\n{valid}",
        lambda valid: f"{valid}\n\n{valid[:-4]}",
        lambda valid: f"{valid[5:]}\n\n{valid}",
        lambda valid: f"{valid}\n\n{valid[5:]}",
        lambda valid: f"{valid}\n\n{valid[:-6]}00:END",
    ],
)
def test_parse_rejects_mixed_valid_and_corrupted_blocks(corrupted_factory) -> None:
    valid = encode_stocktake_payload(_sample_payload())
    mixed_text = corrupted_factory(valid)

    with pytest.raises(ValidationError):
        parse_stocktake_payload(mixed_text)


def test_parse_rejects_missing_end_or_checksum_mismatch_or_bad_base64_or_json() -> None:
    valid = encode_stocktake_payload(_sample_payload())

    # :END 欠落
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload(valid[:-4])

    # 先頭 IMM1: 欠落
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload(valid[5:])

    # ドット欠落
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload("IMM1:YWJj:END")

    # 検査値フォーマット不正 (大文字や桁数不足)
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload("IMM1:YWJj.ABCDEF12:END")

    # 非 ASCII 混入
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload("IMM1:あいうえ.00000000:END")

    # 検査値不一致 (改変)
    tampered_crc = "00000000" if not valid.endswith("00000000:END") else "ffffffff"
    tampered = valid[:-12] + tampered_crc + ":END"
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload(tampered)

    # 不正 Base64 (CRC-32 だけは一致させた壊れた Base64)
    bad_b64 = "!!!not_base64!!!"
    bad_b64_crc = f"{zlib.crc32(bad_b64.encode('ascii')) & 0xFFFFFFFF:08x}"
    with pytest.raises(ValidationError, match="Base64 形式が正しくありません"):
        parse_stocktake_payload(f"IMM1:{bad_b64}.{bad_b64_crc}:END")

    # Base64 長さ不正
    short_b64 = "abc"
    short_crc = f"{zlib.crc32(short_b64.encode('ascii')) & 0xFFFFFFFF:08x}"
    with pytest.raises(ValidationError, match="Base64 形式が正しくありません"):
        parse_stocktake_payload(f"IMM1:{short_b64}.{short_crc}:END")

    # UTF-8 として復号不能なバイト列
    non_utf8_code = _make_imm1_from_bytes(b"\xff\xfe\xfd")
    with pytest.raises(ValidationError, match="Base64 形式が正しくありません"):
        parse_stocktake_payload(non_utf8_code)

    # 不正 JSON (IMM1 内および生テキスト)
    broken_json_code = _make_imm1_from_bytes(b"{broken json")
    with pytest.raises(ValidationError, match="JSON 形式が正しくありません"):
        parse_stocktake_payload(broken_json_code)

    non_dict_json_code = _make_imm1_from_bytes(b"[]")
    with pytest.raises(ValidationError, match="形式が正しくありません"):
        parse_stocktake_payload(non_dict_json_code)

    with pytest.raises(ValidationError, match="JSON 形式が正しくありません"):
        parse_stocktake_payload("{broken json")

    with pytest.raises(ValidationError, match="JSON 形式が正しくありません"):
        parse_stocktake_payload("結果: {broken json")

    # 複数 JSON オブジェクト混在
    json_str = dump_stocktake_payload_json(_sample_payload())
    with pytest.raises(ValidationError, match="複数の棚卸結果が含まれています"):
        parse_stocktake_payload(f"結果:\n{json_str}\n{json_str}")

    # 空入力・無関係な文字列・非文字列
    with pytest.raises(ValidationError):
        parse_stocktake_payload("   ")
    with pytest.raises(ValidationError):
        parse_stocktake_payload("棚卸結果はありません")
    with pytest.raises(ValidationError):
        parse_stocktake_payload(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "invalid_obj",
    [
        [],
        {
            "v": 2,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": True,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": "short",
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID.upper(),
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 0,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": True,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {"v": 1, "session_id": VALID_SESSION_ID, "client_id": 1, "entries": []},
        {"v": 1, "session_id": VALID_SESSION_ID, "client_id": 1, "entries": "not_a_list"},
        {"v": 1, "session_id": VALID_SESSION_ID, "client_id": 1, "entries": ["not_dict"]},
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 0, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": True, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [
                {"item_id": 10, "actual_quantity": 1},
                {"item_id": 10, "actual_quantity": 2},
            ],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": -1}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": MAX_STOCK_QUANTITY + 1}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 1.5}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": False}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": "1"}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 1, "note": 123}],
        },
        {
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": 123,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": "1",
            "entries": [{"item_id": 1, "actual_quantity": 0}],
        },
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"actual_quantity": 0}],
        },
    ],
)
def test_parse_rejects_invalid_payload_fields(invalid_obj: object) -> None:
    raw_json = json.dumps(invalid_obj)
    with pytest.raises(ValidationError):
        parse_stocktake_payload(raw_json)


def test_parse_rejects_draft_backup_with_specific_error() -> None:
    draft_json = json.dumps(
        {
            "kind": "imm-stocktake-draft",
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "started_at": "2026-10-17 00:30:00",
            "items": [],
        }
    )

    with pytest.raises(
        ValidationError,
        match="これは途中保存用の下書きデータです。全品目を確認して出力した棚卸結果データを読み込んでください",
    ):
        parse_stocktake_payload(draft_json)


def test_parse_and_encode_note_normalization_and_length_boundary() -> None:
    note_500 = "😀" * MAX_NOTE_LENGTH
    payload_500 = MobileStocktakePayload(
        session_id=VALID_SESSION_ID,
        client_id=1,
        entries=(
            MobileStocktakeEntry(
                item_id=1,
                actual_quantity=MAX_STOCK_QUANTITY,
                note=f" \u3000\t\n{note_500}\r\n ",
            ),
            MobileStocktakeEntry(
                item_id=2,
                actual_quantity=0,
                note="   \u3000\t\n  ",
            ),
        ),
    )

    code = encode_stocktake_payload(payload_500)
    parsed = parse_stocktake_payload(code)
    assert parsed.entries[0].note == note_500
    assert len(parsed.entries[0].note or "") == MAX_NOTE_LENGTH
    assert parsed.entries[0].actual_quantity == MAX_STOCK_QUANTITY
    assert parsed.entries[1].note is None
    assert parsed.entries[1].actual_quantity == 0

    note_501 = "😀" * (MAX_NOTE_LENGTH + 1)
    raw_501 = json.dumps(
        {
            "v": 1,
            "session_id": VALID_SESSION_ID,
            "client_id": 1,
            "entries": [{"item_id": 1, "actual_quantity": 1, "note": note_501}],
        },
        ensure_ascii=False,
    )
    with pytest.raises(ValidationError, match="500 文字以内"):
        parse_stocktake_payload(raw_501)

    with pytest.raises(ValidationError, match="500 文字以内"):
        encode_stocktake_payload(
            MobileStocktakePayload(
                session_id=VALID_SESSION_ID,
                client_id=1,
                entries=(MobileStocktakeEntry(item_id=1, actual_quantity=1, note=note_501),),
            )
        )

    with pytest.raises(ValidationError, match="品目データがありません"):
        encode_stocktake_payload(
            MobileStocktakePayload(session_id=VALID_SESSION_ID, client_id=1, entries=())
        )

    with pytest.raises(ValidationError, match="重複"):
        encode_stocktake_payload(
            MobileStocktakePayload(
                session_id=VALID_SESSION_ID,
                client_id=1,
                entries=(
                    MobileStocktakeEntry(item_id=1, actual_quantity=1),
                    MobileStocktakeEntry(item_id=1, actual_quantity=2),
                ),
            )
        )


def test_render_stocktake_html_escapes_script_and_contains_required_elements() -> None:
    sheet = _sample_sheet()
    rendered = render_stocktake_html(sheet)

    assert "<!doctype html>" in rendered
    assert "<noscript>" in rendered
    assert "JavaScript が無効です" in rendered
    assert "Chrome で開く" in rendered
    assert "右上のメニューから Chrome で開いてください" in rendered

    # HTML 特殊文字や </script>、U+2028/U+2029 が生で <script> 内に露出しないこと
    assert "</script><script>alert(1)" not in rendered
    assert "A社 <テスト & Co.>" not in rendered
    assert "山田 <太郎> & Co." not in rendered
    assert "\u2028" not in rendered
    assert "\u2029" not in rendered

    assert "A社 &lt;テスト &amp; Co.&gt;" in rendered
    assert "山田 &lt;太郎&gt; &amp; Co." in rendered
    assert r"\u003c/script\u003e\u003cscript\u003ealert(1)\u003c/script\u003e" in rendered
    assert r"\u2028" in rendered
    assert r"\u2029" in rendered

    # 必須 UI 要素
    assert "確認済 0 / 2 件" in rendered
    assert "未確認" in rendered
    assert "確認済" in rendered
    assert "差異あり" in rendered
    assert "すべて" in rendered
    assert "保管場所" in rendered
    assert "品名・管理番号・型番" in rendered
    assert "初期状態にリセット" in rendered
    assert 'id="reset-confirmation"' in rendered
    assert 'id="confirm-reset-state"' in rendered
    assert 'id="cancel-reset-state"' in rendered
    assert "📋 結果テキストをコピー(IMM1:...)" in rendered
    assert "💾 結果ファイル(.json)を保存" in rendered
    assert "結果テキストを表示" in rendered
    assert "途中保存できません。画面を閉じないでください。" in rendered

    # 非標準の started_at でもフォールバックしてレンダリングできること
    custom_sheet = MobileStocktakeSheet(
        session_id=VALID_SESSION_ID,
        client_id=1,
        client_name="B社",
        staff_id=1,
        staff_name="佐藤",
        started_at="custom-time",
        items=sheet.items,
    )
    custom_html = render_stocktake_html(custom_sheet)
    assert "custom-time" in custom_html


def test_render_uses_indexeddb_without_local_storage_or_network_access() -> None:
    rendered = render_stocktake_html(_sample_sheet())

    assert "indexedDB" in rendered
    assert 'const STORAGE_DATABASE_NAME = "imm_stocktake";' in rendered
    assert "imm-stocktake-draft" in rendered
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "fetch(",
        "XMLHttpRequest",
        "sendBeacon",
        "WebSocket",
        "EventSource",
        "http://",
        "https://",
        "<link",
        "src=",
        "@import",
    ):
        assert forbidden not in rendered, forbidden


def test_parse_rejects_repeated_end_marker_and_end_before_prefix() -> None:
    code = encode_stocktake_payload(_sample_payload())

    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload(f"{code}:END")
    with pytest.raises(ValidationError, match="途中で欠けているか改変されています"):
        parse_stocktake_payload(f"前文:END\n{code}")


def test_parse_json_note_boundary_matches_python_strip_semantics() -> None:
    def raw(note: str) -> str:
        return json.dumps(
            {
                "v": 1,
                "session_id": VALID_SESSION_ID,
                "client_id": 1,
                "entries": [{"item_id": 1, "actual_quantity": 0, "note": note}],
            },
            ensure_ascii=False,
        )

    body = "😀" * MAX_NOTE_LENGTH
    parsed = parse_stocktake_payload(raw(f"\u3000 \x85\x1f{body}\t "))
    assert parsed.entries[0].note == body

    # U+FEFF は str.strip() の対象外のため本文として数える(スマホ側も同じ扱い)
    with pytest.raises(ValidationError, match="500 文字以内"):
        parse_stocktake_payload(raw(f"{body}\ufeff"))
    assert parse_stocktake_payload(raw("\u3000\x85\x1f\t ")).entries[0].note is None


def test_render_embeds_sheet_data_that_round_trips_through_json() -> None:
    sheet = _sample_sheet()
    rendered = render_stocktake_html(sheet)

    match = re.search(r"const SHEET = (\{.*\});\n", rendered)
    assert match is not None
    embedded = json.loads(match.group(1))

    assert embedded["session_id"] == sheet.session_id
    assert embedded["client_id"] == sheet.client_id
    assert embedded["client_name"] == sheet.client_name
    assert embedded["staff_name"] == sheet.staff_name
    assert embedded["max_quantity"] == MAX_STOCK_QUANTITY
    assert embedded["max_note_length"] == MAX_NOTE_LENGTH
    assert [item["item_id"] for item in embedded["items"]] == [101, 102]
    assert embedded["items"][0]["name"] == sheet.items[0].name
    assert embedded["items"][0]["location_name"] == "第1倉庫 <A棚>"
    assert embedded["items"][1]["manufacturer_part_number"] is None
    assert embedded["items"][1]["book_quantity"] == 12


def test_render_shows_started_at_in_local_time_and_builds_filename_timestamp() -> None:
    sheet = _sample_sheet()
    embedded = json.loads(
        re.search(r"const SHEET = (\{.*\});\n", render_stocktake_html(sheet)).group(1)  # type: ignore[union-attr]
    )

    local = to_local(sheet.started_at)
    assert embedded["started_at"] == sheet.started_at
    assert embedded["started_at_display"] == local.strftime("%Y-%m-%d %H:%M:%S")
    assert embedded["filename_timestamp"] == local.strftime("%Y%m%d_%H%M%S")
    assert re.fullmatch(r"\d{8}_\d{6}", embedded["filename_timestamp"])

    fallback = MobileStocktakeSheet(
        session_id=VALID_SESSION_ID,
        client_id=1,
        client_name="B社",
        staff_id=1,
        staff_name="佐藤",
        started_at="custom-time",
        items=sheet.items,
    )
    fallback_embedded = json.loads(
        re.search(r"const SHEET = (\{.*\});\n", render_stocktake_html(fallback)).group(1)  # type: ignore[union-attr]
    )
    assert fallback_embedded["started_at_display"] == "custom-time"
    assert fallback_embedded["filename_timestamp"] == "customtime"
