from __future__ import annotations

import base64
import binascii
import html
import json
import re
import zlib
from typing import cast

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.models import (
    MAX_STOCK_QUANTITY,
    MobileStocktakeEntry,
    MobileStocktakePayload,
    MobileStocktakeSheet,
)
from inventory_manager_mini.core.timeutil import to_local

RESULT_CODE_PREFIX = "IMM1:"
RESULT_CODE_SUFFIX = ":END"
MAX_NOTE_LENGTH = 500
PAYLOAD_VERSION = 1

_SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{8}$")
_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_LEADING_QUOTE_PATTERN = re.compile(r"^[ \t>]+")
_WHITESPACE_PATTERN = re.compile(r"\s+")

_SCRIPT_JSON_ESCAPE_TABLE = str.maketrans(
    {
        "<": r"\u003c",
        ">": r"\u003e",
        "&": r"\u0026",
        "\u2028": r"\u2028",
        "\u2029": r"\u2029",
    }
)


def _crc32_hex(value: bytes) -> str:
    return f"{zlib.crc32(value) & 0xFFFFFFFF:08x}"


def _validate_session_id(value: object) -> str:
    if not isinstance(value, str) or _SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValidationError("セッション ID の形式が正しくありません")
    return value


def _validate_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValidationError(f"{label}は 1 以上の整数で指定してください")
    return value


def _validate_actual_quantity(value: object) -> int:
    if type(value) is not int or not (0 <= value <= MAX_STOCK_QUANTITY):
        raise ValidationError(f"棚卸実数は 0〜{MAX_STOCK_QUANTITY:,} の整数で指定してください")
    return value


def _validate_note(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("メモは文字列で指定してください")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_NOTE_LENGTH:
        raise ValidationError(f"メモは {MAX_NOTE_LENGTH} 文字以内で入力してください")
    return normalized


def _payload_to_dict(payload: MobileStocktakePayload) -> dict[str, object]:
    session_id = _validate_session_id(payload.session_id)
    client_id = _validate_positive_int(payload.client_id, "クライアント ID")
    if not payload.entries:
        raise ValidationError("棚卸結果の品目データがありません")

    seen_item_ids: set[int] = set()
    serialized_entries: list[dict[str, object]] = []
    for entry in payload.entries:
        item_id = _validate_positive_int(entry.item_id, "品目 ID")
        if item_id in seen_item_ids:
            raise ValidationError(f"品目 ID {item_id} が重複しています")
        seen_item_ids.add(item_id)
        actual_quantity = _validate_actual_quantity(entry.actual_quantity)
        note = _validate_note(entry.note)
        serialized_entries.append(
            {
                "item_id": item_id,
                "actual_quantity": actual_quantity,
                "note": note,
            }
        )

    return {
        "v": PAYLOAD_VERSION,
        "session_id": session_id,
        "client_id": client_id,
        "entries": serialized_entries,
    }


def encode_stocktake_payload(payload: MobileStocktakePayload) -> str:
    data = _payload_to_dict(payload)
    json_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded_bytes = base64.b64encode(json_bytes)
    encoded_str = encoded_bytes.decode("ascii")
    checksum = _crc32_hex(encoded_bytes)
    return f"{RESULT_CODE_PREFIX}{encoded_str}.{checksum}{RESULT_CODE_SUFFIX}"


def dump_stocktake_payload_json(payload: MobileStocktakePayload) -> str:
    data = _payload_to_dict(payload)
    return json.dumps(data, ensure_ascii=False, indent=2)


def _decode_imm1_block(block: str) -> str:
    inner = block[len(RESULT_CODE_PREFIX) : -len(RESULT_CODE_SUFFIX)]
    if "." not in inner:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")
    encoded, checksum = inner.rsplit(".", 1)
    if not encoded or _CHECKSUM_PATTERN.fullmatch(checksum) is None:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")

    try:
        encoded_bytes = encoded.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValidationError("結果テキストが途中で欠けているか改変されています") from error

    expected_checksum = _crc32_hex(encoded_bytes)
    if checksum != expected_checksum:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")

    if len(encoded) % 4 != 0 or _BASE64_PATTERN.fullmatch(encoded) is None:
        raise ValidationError("結果テキストの Base64 形式が正しくありません")

    try:
        decoded_bytes = base64.b64decode(encoded_bytes, validate=True)
        return decoded_bytes.decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        raise ValidationError("結果テキストの Base64 形式が正しくありません") from error


def _extract_json_object(raw_text: object) -> object:
    if not isinstance(raw_text, str):
        raise ValidationError("棚卸結果テキストを入力してください")

    stripped = raw_text.lstrip("\ufeff").strip()
    if not stripped:
        raise ValidationError("棚卸結果テキストを入力してください")

    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as error:
            raise ValidationError("結果データの JSON 形式が正しくありません") from error

    cleaned_lines = [_LEADING_QUOTE_PATTERN.sub("", line) for line in stripped.splitlines()]
    cleaned_text = "\n".join(cleaned_lines)

    starts = [match.start() for match in re.finditer(re.escape(RESULT_CODE_PREFIX), cleaned_text)]
    if starts:
        prefix_before_first = _WHITESPACE_PATTERN.sub("", cleaned_text[: starts[0]])
        if RESULT_CODE_SUFFIX in prefix_before_first:
            raise ValidationError("結果テキストが途中で欠けているか改変されています")

        normalized_blocks: list[str] = []
        for index, start in enumerate(starts):
            next_start = starts[index + 1] if index + 1 < len(starts) else len(cleaned_text)
            segment = cleaned_text[start:next_start]
            compact_segment = _WHITESPACE_PATTERN.sub("", segment)
            end_pos = compact_segment.find(RESULT_CODE_SUFFIX)
            if end_pos == -1:
                raise ValidationError(
                    "結果テキストの終端(:END)が見つかりません。"
                    "結果テキストが途中で欠けているか改変されています"
                )
            block = compact_segment[: end_pos + len(RESULT_CODE_SUFFIX)]
            remainder = compact_segment[end_pos + len(RESULT_CODE_SUFFIX) :]
            if RESULT_CODE_SUFFIX in remainder:
                raise ValidationError("結果テキストが途中で欠けているか改変されています")
            normalized_blocks.append(block)

        unique_blocks = list(dict.fromkeys(normalized_blocks))
        if len(unique_blocks) > 1:
            raise ValidationError(
                "複数の棚卸結果が含まれています。取り込む結果を1件だけ貼り付けてください"
            )

        decoded_json = _decode_imm1_block(unique_blocks[0])
        try:
            return json.loads(decoded_json)
        except json.JSONDecodeError as error:
            raise ValidationError("結果データの JSON 形式が正しくありません") from error

    compact_all = _WHITESPACE_PATTERN.sub("", cleaned_text)
    if RESULT_CODE_SUFFIX in compact_all:
        raise ValidationError("結果テキストが途中で欠けているか改変されています")

    brace_pos = cleaned_text.find("{")
    if brace_pos != -1:
        decoder = json.JSONDecoder()
        try:
            parsed, end_idx = decoder.raw_decode(cleaned_text, brace_pos)
        except json.JSONDecodeError as error:
            raise ValidationError("結果データの JSON 形式が正しくありません") from error
        trailing = cleaned_text[end_idx:]
        if "{" in trailing:
            raise ValidationError(
                "複数の棚卸結果が含まれています。取り込む結果を1件だけ貼り付けてください"
            )
        return parsed

    raise ValidationError("有効な棚卸結果データが見つかりません")


def _validate_payload_object(data: object) -> MobileStocktakePayload:
    if not isinstance(data, dict):
        raise ValidationError("棚卸結果データの形式が正しくありません")
    payload_dict = cast(dict[str, object], data)

    version = payload_dict.get("v")
    if type(version) is not int or version != PAYLOAD_VERSION:
        raise ValidationError("対応していない棚卸結果フォーマット版数です")

    session_id = _validate_session_id(payload_dict.get("session_id"))
    client_id = _validate_positive_int(payload_dict.get("client_id"), "クライアント ID")

    entries_raw = payload_dict.get("entries")
    if not isinstance(entries_raw, list):
        raise ValidationError("棚卸結果の品目データがありません")
    entries_list = cast(list[object], entries_raw)
    if not entries_list:
        raise ValidationError("棚卸結果の品目データがありません")

    seen_item_ids: set[int] = set()
    entries: list[MobileStocktakeEntry] = []
    for raw_entry in entries_list:
        if not isinstance(raw_entry, dict):
            raise ValidationError("品目データの形式が正しくありません")
        entry_dict = cast(dict[str, object], raw_entry)
        item_id = _validate_positive_int(entry_dict.get("item_id"), "品目 ID")
        if item_id in seen_item_ids:
            raise ValidationError(f"品目 ID {item_id} が重複しています")
        seen_item_ids.add(item_id)

        actual_quantity = _validate_actual_quantity(entry_dict.get("actual_quantity"))
        note = _validate_note(entry_dict.get("note"))
        entries.append(
            MobileStocktakeEntry(
                item_id=item_id,
                actual_quantity=actual_quantity,
                note=note,
            )
        )

    return MobileStocktakePayload(
        session_id=session_id,
        client_id=client_id,
        entries=tuple(entries),
    )


def parse_stocktake_payload(raw_text: str) -> MobileStocktakePayload:
    data = _extract_json_object(raw_text)
    return _validate_payload_object(data)


def _safe_json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).translate(
        _SCRIPT_JSON_ESCAPE_TABLE
    )


def _format_sheet_timestamps(started_at: str) -> tuple[str, str]:
    try:
        local_dt = to_local(started_at)
        return (
            local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            local_dt.strftime("%Y%m%d_%H%M%S"),
        )
    except ValueError:
        sanitized = re.sub(r"[^0-9A-Za-z]+", "", started_at) or "result"
        return started_at, sanitized


def render_stocktake_html(sheet: MobileStocktakeSheet) -> str:
    started_at_display, filename_timestamp = _format_sheet_timestamps(sheet.started_at)
    sheet_data = {
        "session_id": sheet.session_id,
        "client_id": sheet.client_id,
        "client_name": sheet.client_name,
        "staff_id": sheet.staff_id,
        "staff_name": sheet.staff_name,
        "started_at": sheet.started_at,
        "started_at_display": started_at_display,
        "filename_timestamp": filename_timestamp,
        "max_quantity": MAX_STOCK_QUANTITY,
        "max_note_length": MAX_NOTE_LENGTH,
        "items": [
            {
                "item_id": item.item_id,
                "code": item.code,
                "name": item.name,
                "manufacturer_part_number": item.manufacturer_part_number,
                "location_name": item.location_name,
                "book_quantity": item.book_quantity,
                "unit": item.unit,
            }
            for item in sheet.items
        ],
    }

    escaped_client = html.escape(sheet.client_name, quote=True)
    escaped_staff = html.escape(sheet.staff_name, quote=True)
    escaped_started_utc = html.escape(sheet.started_at, quote=True)
    escaped_started_display = html.escape(started_at_display, quote=True)
    total_count = len(sheet.items)
    embedded_json = _safe_json_for_script(sheet_data)
    noscript_message = (
        "JavaScript が無効です。Android のメニュー [⋮] から"
        "『Chrome で開く』(またはブラウザで開く)を選択してください。"
        "右上のメニューから Chrome で開いてください。"
    )

    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>スマホ棚卸シート - {escaped_client}</title>
  <style>
    *, *::before, *::after {{
      box-sizing: border-box;
    }}
    html, body {{
      margin: 0;
      padding: 0;
      height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Hiragino Sans", "Noto Sans JP", sans-serif;
      background: #f3f4f6;
      color: #111827;
      line-height: 1.4;
      overflow: hidden;
    }}
    [hidden] {{
      display: none !important;
    }}
    .visually-hidden {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }}
    .noscript-banner {{
      margin: 16px;
      padding: 20px;
      border: 3px solid #dc2626;
      border-radius: 10px;
      background: #fef2f2;
      color: #991b1b;
      font-size: 1.1rem;
      font-weight: 700;
    }}
    .app-shell {{
      display: flex;
      flex-direction: column;
      height: 100vh;
      height: 100dvh;
      width: 100%;
      overflow: hidden;
    }}
    .sticky-header {{
      flex: 0 0 auto;
      background: #ffffff;
      border-bottom: 1px solid #d1d5db;
      padding: 8px 12px;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
      z-index: 10;
    }}
    .header-top {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 6px 12px;
    }}
    .title-block h1 {{
      margin: 0;
      font-size: 1rem;
      font-weight: 700;
      color: #111827;
    }}
    .meta-line {{
      margin: 2px 0 0;
      font-size: 0.8rem;
      color: #4b5563;
      display: flex;
      flex-wrap: wrap;
      gap: 4px 12px;
    }}
    .progress-block {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px 10px;
    }}
    .progress-label {{
      font-size: 0.85rem;
      font-weight: 700;
      color: #1f2937;
      white-space: nowrap;
    }}
    progress {{
      width: 110px;
      height: 12px;
    }}
    .filter-bar {{
      margin-top: 6px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 6px 10px;
    }}
    .status-tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }}
    .tab-btn {{
      padding: 4px 10px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      background: #f9fafb;
      color: #374151;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .tab-btn.active {{
      background: #2563eb;
      border-color: #2563eb;
      color: #ffffff;
    }}
    .filter-inputs {{
      display: flex;
      flex: 1 1 240px;
      gap: 6px;
      min-width: 0;
    }}
    .filter-inputs select,
    .filter-inputs input {{
      padding: 4px 8px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      font-size: 0.85rem;
      background: #ffffff;
      color: #111827;
      min-width: 0;
    }}
    .filter-inputs select {{
      flex: 0 1 150px;
    }}
    .filter-inputs input {{
      flex: 1 1 140px;
    }}
    .warning-banner {{
      margin-top: 6px;
      padding: 6px 10px;
      border-radius: 6px;
      background: #fef2f2;
      border: 1px solid #f87171;
      color: #991b1b;
      font-size: 0.8rem;
      font-weight: 600;
    }}
    .content-scroll {{
      flex: 1 1 auto;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      padding: 10px 12px 24px;
    }}
    .items-list {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      max-width: 900px;
      margin: 0 auto;
    }}
    .item-card {{
      background: #ffffff;
      border: 2px solid #e5e7eb;
      border-radius: 10px;
      padding: 10px 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
    }}
    .item-card[data-confirmed="true"] {{
      border-color: #16a34a;
      background: #f0fdf4;
    }}
    .item-card[data-invalid="true"] {{
      border-color: #dc2626;
      background: #fef2f2;
    }}
    .card-header {{
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      justify-content: space-between;
      gap: 4px 8px;
    }}
    .card-title {{
      margin: 0;
      font-size: 0.95rem;
      font-weight: 700;
      color: #111827;
      word-break: break-word;
    }}
    .card-code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.85rem;
      color: #1d4ed8;
      margin-right: 6px;
    }}
    .card-badges {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }}
    .status-badge, .delta-badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 700;
    }}
    .status-badge[data-state="unconfirmed"] {{
      background: #fef3c7;
      color: #92400e;
    }}
    .status-badge[data-state="confirmed"] {{
      background: #dcfce7;
      color: #166534;
    }}
    .status-badge[data-state="invalid"] {{
      background: #fee2e2;
      color: #991b1b;
    }}
    .delta-badge {{
      background: #e5e7eb;
      color: #374151;
    }}
    .delta-badge[data-diff="true"] {{
      background: #dbeafe;
      color: #1e40af;
    }}
    .card-meta {{
      font-size: 0.8rem;
      color: #4b5563;
      display: flex;
      flex-wrap: wrap;
      gap: 4px 12px;
    }}
    .qty-controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
    }}
    .btn-step {{
      min-width: 42px;
      padding: 6px 8px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      background: #f9fafb;
      color: #111827;
      font-size: 0.85rem;
      font-weight: 700;
      cursor: pointer;
    }}
    .input-actual {{
      width: 110px;
      padding: 6px 8px;
      border: 1px solid #9ca3af;
      border-radius: 6px;
      font-size: 0.95rem;
      font-weight: 700;
      text-align: right;
      background: #ffffff;
      color: #111827;
    }}
    .unit-label {{
      font-size: 0.85rem;
      color: #374151;
      font-weight: 600;
    }}
    .input-note {{
      width: 100%;
      padding: 6px 8px;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      font-family: inherit;
      font-size: 0.85rem;
      background: #ffffff;
      color: #111827;
      resize: vertical;
    }}
    .item-error {{
      margin: 0;
      padding: 4px 8px;
      border-radius: 6px;
      background: #fee2e2;
      color: #991b1b;
      font-size: 0.8rem;
      font-weight: 600;
    }}
    .card-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .btn-book-ok,
    .btn-confirm,
    .btn-unconfirm,
    .btn-primary,
    .btn-secondary {{
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 0.85rem;
      font-weight: 700;
      cursor: pointer;
      border: 1px solid transparent;
    }}
    .btn-book-ok {{
      background: #eff6ff;
      border-color: #93c5fd;
      color: #1d4ed8;
    }}
    .btn-confirm {{
      background: #16a34a;
      color: #ffffff;
    }}
    .btn-unconfirm {{
      background: #ffffff;
      border-color: #d1d5db;
      color: #374151;
    }}
    .btn-primary {{
      background: #2563eb;
      color: #ffffff;
    }}
    .btn-secondary {{
      background: #ffffff;
      border-color: #9ca3af;
      color: #1f2937;
    }}
    .btn-small {{
      padding: 3px 8px;
      font-size: 0.75rem;
    }}
    button:disabled {{
      opacity: 0.45;
      cursor: not-allowed;
    }}
    .empty-message {{
      text-align: center;
      color: #6b7280;
      padding: 20px;
      font-size: 0.9rem;
    }}
    .completion-panel {{
      max-width: 900px;
      margin: 16px auto 0;
      padding: 12px 14px;
      background: #ffffff;
      border: 2px solid #d1d5db;
      border-radius: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .completion-panel h2 {{
      margin: 0;
      font-size: 0.95rem;
    }}
    .completion-status {{
      margin: 0;
      font-size: 0.85rem;
      font-weight: 600;
      color: #374151;
    }}
    .completion-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .result-feedback {{
      margin: 0;
      padding: 6px 10px;
      border-radius: 6px;
      background: #eff6ff;
      border: 1px solid #93c5fd;
      color: #1e3a8a;
      font-size: 0.8rem;
      font-weight: 600;
    }}
    .result-box {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .result-box p {{
      margin: 0;
      font-size: 0.8rem;
      font-weight: 600;
      color: #374151;
    }}
    #result-text {{
      width: 100%;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.8rem;
      padding: 8px;
      border: 1px solid #9ca3af;
      border-radius: 6px;
      background: #f9fafb;
      color: #111827;
      resize: vertical;
    }}
    @media (max-height: 500px) {{
      .sticky-header {{
        padding: 4px 10px;
      }}
      .title-block h1 {{
        font-size: 0.9rem;
      }}
      .meta-line {{
        margin: 0;
        font-size: 0.75rem;
      }}
      .filter-bar {{
        margin-top: 4px;
        gap: 4px 8px;
      }}
      .tab-btn {{
        padding: 2px 8px;
        font-size: 0.75rem;
      }}
      .filter-inputs select,
      .filter-inputs input {{
        padding: 2px 6px;
        font-size: 0.8rem;
      }}
      .content-scroll {{
        padding: 6px 10px 16px;
      }}
    }}
  </style>
</head>
<body>
  <noscript>
    <div class="noscript-banner" role="alert">
      {noscript_message}
    </div>
  </noscript>
  <div class="app-shell">
    <header class="sticky-header" id="app-header">
      <div class="header-top">
        <div class="title-block">
          <h1>スマホ棚卸シート: <span id="client-name">{escaped_client}</span></h1>
          <p class="meta-line">
            <span>担当: <strong id="staff-name">{escaped_staff}</strong></span>
            <span>
              開始日時:
              <time
                id="started-at"
                datetime="{escaped_started_utc}"
              >{escaped_started_display}</time>
              ({escaped_started_utc} UTC)
            </span>
          </p>
        </div>
        <div class="progress-block">
          <span id="progress-text" class="progress-label">確認済 0 / {total_count} 件</span>
          <progress id="progress-bar" value="0" max="{total_count}"></progress>
          <button type="button" id="reset-state" class="btn-secondary btn-small">
            初期状態にリセット
          </button>
        </div>
      </div>
      <div class="filter-bar">
        <div class="status-tabs" role="group" aria-label="状態フィルタ">
          <button
            type="button"
            class="tab-btn active"
            data-status-filter="all"
            aria-pressed="true"
          >すべて</button>
          <button
            type="button"
            class="tab-btn"
            data-status-filter="unconfirmed"
            aria-pressed="false"
          >未確認</button>
          <button
            type="button"
            class="tab-btn"
            data-status-filter="confirmed"
            aria-pressed="false"
          >確認済</button>
          <button
            type="button"
            class="tab-btn"
            data-status-filter="diff"
            aria-pressed="false"
          >差異あり</button>
        </div>
        <div class="filter-inputs">
          <label class="visually-hidden" for="location-filter">保管場所</label>
          <select id="location-filter" aria-label="保管場所">
            <option value="">すべての保管場所</option>
          </select>
          <label class="visually-hidden" for="search-input">キーワード検索</label>
          <input
            id="search-input"
            type="search"
            aria-label="キーワード検索"
            placeholder="品名・管理番号・型番"
          >
        </div>
      </div>
      <div id="storage-warning" class="warning-banner" role="alert" hidden>
        途中保存できません。画面を閉じないでください。
      </div>
      <div id="restore-warning" class="warning-banner" role="alert" hidden>
        保存されていた途中データが不正または現在のシートと一致しないため、初期状態で読み込みました。
        内容を再入力するか「初期状態にリセット」を押してください。
      </div>
    </header>

    <div class="content-scroll" id="content-scroll">
      <main id="items-container" class="items-list"></main>
      <p id="empty-filter-message" class="empty-message" hidden>
        条件に一致する品目がありません。
      </p>

      <footer class="completion-panel" id="completion-panel">
        <h2>棚卸結果の出力</h2>
        <p id="completion-status" class="completion-status"></p>
        <div class="completion-actions">
          <button type="button" id="copy-result" class="btn-primary" disabled>
            📋 結果テキストをコピー(IMM1:...)
          </button>
          <button type="button" id="save-result" class="btn-primary" disabled>
            💾 結果ファイル(.json)を保存
          </button>
          <button type="button" id="show-result" class="btn-secondary" disabled>
            結果テキストを表示
          </button>
        </div>
        <p id="result-feedback" class="result-feedback" role="status" hidden></p>
        <div id="result-box" class="result-box" hidden>
          <p>結果テキスト（長押しでコピーしてください）</p>
          <textarea
            id="result-text"
            aria-label="結果テキスト"
            rows="5"
            readonly
            hidden
          ></textarea>
        </div>
      </footer>
    </div>
  </div>
  <script>
    const SHEET = {embedded_json};
    const STORAGE_KEY = `imm_stocktake_${{SHEET.session_id}}`;
    const MAX_STOCK_QUANTITY = SHEET.max_quantity;
    const MAX_NOTE_LENGTH = SHEET.max_note_length;
    const NONE_LOCATION_VALUE = "__NONE__";
    const PYTHON_SPACE_CLASS =
      "[\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0" +
      "\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
    const STRIP_SPACE_RE = new RegExp(
      `^${{PYTHON_SPACE_CLASS}}+|${{PYTHON_SPACE_CLASS}}+$`,
      "g"
    );

    const itemsById = new Map(SHEET.items.map(item => [item.item_id, item]));
    const cardRefs = new Map();

    let statusFilter = "all";
    let locationFilter = "";
    let keywordFilter = "";

    const storageWarningEl = document.getElementById("storage-warning");
    const restoreWarningEl = document.getElementById("restore-warning");
    const progressTextEl = document.getElementById("progress-text");
    const progressBarEl = document.getElementById("progress-bar");
    const resetStateBtn = document.getElementById("reset-state");
    const locationFilterEl = document.getElementById("location-filter");
    const searchInputEl = document.getElementById("search-input");
    const itemsContainerEl = document.getElementById("items-container");
    const emptyFilterMessageEl = document.getElementById("empty-filter-message");
    const completionStatusEl = document.getElementById("completion-status");
    const copyResultBtn = document.getElementById("copy-result");
    const saveResultBtn = document.getElementById("save-result");
    const showResultBtn = document.getElementById("show-result");
    const resultFeedbackEl = document.getElementById("result-feedback");
    const resultBoxEl = document.getElementById("result-box");
    const resultTextEl = document.getElementById("result-text");

    function stripNote(value) {{
      if (typeof value !== "string") {{
        return "";
      }}
      return value.replace(STRIP_SPACE_RE, "");
    }}

    function codePointLength(value) {{
      return Array.from(value).length;
    }}

    function validateItemState(actualStr, noteStr, badInput = false) {{
      const errors = [];
      let quantity = null;
      if (badInput || typeof actualStr !== "string" || actualStr.length === 0) {{
        errors.push("実数を入力してください（0〜1,000,000 の整数）");
      }} else if (!/^\\d+$/.test(actualStr)) {{
        errors.push("実数は 0〜1,000,000 の整数で入力してください（小数・負数は不可）");
      }} else {{
        const parsed = Number(actualStr);
        if (!Number.isSafeInteger(parsed) || parsed < 0 || parsed > MAX_STOCK_QUANTITY) {{
          errors.push("実数は 0〜1,000,000 の範囲で入力してください");
        }} else {{
          quantity = parsed;
        }}
      }}

      let normalizedNote = null;
      if (typeof noteStr !== "string") {{
        errors.push("メモの形式が不正です");
      }} else {{
        const stripped = stripNote(noteStr);
        const length = codePointLength(stripped);
        if (length > MAX_NOTE_LENGTH) {{
          errors.push(`メモは 500 文字以内で入力してください（現在 ${{length}} 文字）`);
        }} else {{
          normalizedNote = stripped.length > 0 ? stripped : null;
        }}
      }}

      return {{
        valid: errors.length === 0,
        errors,
        quantity,
        normalizedNote,
      }};
    }}

    function createInitialState() {{
      return Object.fromEntries(
        SHEET.items.map(item => [
          String(item.item_id),
          {{
            actual: String(item.book_quantity),
            note: "",
            confirmed: false,
            badInput: false,
          }},
        ])
      );
    }}

    let state = createInitialState();

    function setStorageError(flag) {{
      storageWarningEl.hidden = !flag;
      document.body.dataset.storageError = flag ? "true" : "false";
    }}

    function setRestoreError(flag) {{
      restoreWarningEl.hidden = !flag;
      document.body.dataset.restoreError = flag ? "true" : "false";
    }}

    function validateStoredPayload(parsed) {{
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {{
        return null;
      }}
      let itemsMap = parsed;
      if ("v" in parsed || "session_id" in parsed || "items" in parsed) {{
        if (
          parsed.v !== 1 ||
          parsed.session_id !== SHEET.session_id ||
          typeof parsed.items !== "object" ||
          parsed.items === null ||
          Array.isArray(parsed.items)
        ) {{
          return null;
        }}
        itemsMap = parsed.items;
      }}

      const keys = Object.keys(itemsMap);
      if (keys.length !== SHEET.items.length) {{
        return null;
      }}

      const restored = {{}};
      for (const item of SHEET.items) {{
        const key = String(item.item_id);
        if (!Object.prototype.hasOwnProperty.call(itemsMap, key)) {{
          return null;
        }}
        const entry = itemsMap[key];
        if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {{
          return null;
        }}
        if (
          typeof entry.actual !== "string" ||
          typeof entry.note !== "string" ||
          typeof entry.confirmed !== "boolean"
        ) {{
          return null;
        }}
        const check = validateItemState(entry.actual, entry.note, false);
        if (!check.valid) {{
          return null;
        }}
        restored[key] = {{
          actual: String(check.quantity),
          note: entry.note,
          confirmed: entry.confirmed,
          badInput: false,
        }};
      }}
      return restored;
    }}

    function loadSavedState() {{
      let stored = null;
      try {{
        stored = window.localStorage.getItem(STORAGE_KEY);
        setStorageError(false);
      }} catch {{
        setStorageError(true);
        return;
      }}
      if (stored === null) {{
        return;
      }}
      try {{
        const parsed = JSON.parse(stored);
        const validated = validateStoredPayload(parsed);
        if (validated === null) {{
          state = createInitialState();
          setRestoreError(true);
          return;
        }}
        state = validated;
        setRestoreError(false);
      }} catch {{
        state = createInitialState();
        setRestoreError(true);
      }}
    }}

    function saveState() {{
      try {{
        const serializableItems = {{}};
        for (const item of SHEET.items) {{
          const key = String(item.item_id);
          const itemState = state[key];
          serializableItems[key] = {{
            actual: itemState.actual,
            note: itemState.note,
            confirmed: itemState.confirmed,
          }};
        }}
        const payload = {{
          v: 1,
          session_id: SHEET.session_id,
          items: serializableItems,
        }};
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
        setStorageError(false);
      }} catch {{
        setStorageError(true);
      }}
    }}

    function hideResultOutput() {{
      resultTextEl.value = "";
      resultTextEl.hidden = true;
      resultBoxEl.hidden = true;
      resultFeedbackEl.textContent = "";
      resultFeedbackEl.hidden = true;
    }}

    function populateLocationFilter() {{
      const seen = new Set();
      let hasNone = false;
      for (const item of SHEET.items) {{
        if (item.location_name === null || item.location_name === "") {{
          hasNone = true;
        }} else if (!seen.has(item.location_name)) {{
          seen.add(item.location_name);
          const option = document.createElement("option");
          option.value = item.location_name;
          option.textContent = item.location_name;
          locationFilterEl.appendChild(option);
        }}
      }}
      if (hasNone) {{
        const noneOption = document.createElement("option");
        noneOption.value = NONE_LOCATION_VALUE;
        noneOption.textContent = "未設定";
        locationFilterEl.appendChild(noneOption);
      }}
    }}

    function matchesFilter(item) {{
      const key = String(item.item_id);
      const itemState = state[key];
      const validation = validateItemState(itemState.actual, itemState.note, itemState.badInput);

      if (statusFilter === "unconfirmed" && itemState.confirmed) {{
        return false;
      }}
      if (statusFilter === "confirmed" && !itemState.confirmed) {{
        return false;
      }}
      if (statusFilter === "diff") {{
        if (!validation.valid || validation.quantity === item.book_quantity) {{
          return false;
        }}
      }}

      if (locationFilter !== "") {{
        if (locationFilter === NONE_LOCATION_VALUE) {{
          if (item.location_name !== null && item.location_name !== "") {{
            return false;
          }}
        }} else if (item.location_name !== locationFilter) {{
          return false;
        }}
      }}

      if (keywordFilter !== "") {{
        const haystack = [
          item.code,
          item.name,
          item.manufacturer_part_number || "",
        ].join(" ").toLowerCase();
        if (!haystack.includes(keywordFilter)) {{
          return false;
        }}
      }}
      return true;
    }}

    function applyFilters() {{
      let visibleCount = 0;
      for (const item of SHEET.items) {{
        const refs = cardRefs.get(item.item_id);
        if (!refs) continue;
        const visible = matchesFilter(item);
        refs.card.hidden = !visible;
        if (visible) visibleCount += 1;
      }}
      emptyFilterMessageEl.hidden = visibleCount > 0;
    }}

    function updateCardUI(item, syncInputs = false) {{
      const refs = cardRefs.get(item.item_id);
      if (!refs) return;
      const key = String(item.item_id);
      const itemState = state[key];
      const validation = validateItemState(itemState.actual, itemState.note, itemState.badInput);

      if (!validation.valid && itemState.confirmed) {{
        itemState.confirmed = false;
      }}

      if (syncInputs) {{
        refs.actualInput.value = itemState.actual;
        refs.noteInput.value = itemState.note;
      }}

      refs.card.dataset.confirmed = itemState.confirmed ? "true" : "false";
      refs.card.dataset.invalid = validation.valid ? "false" : "true";

      if (!validation.valid) {{
        refs.statusBadge.textContent = "入力エラー";
        refs.statusBadge.dataset.state = "invalid";
      }} else if (itemState.confirmed) {{
        refs.statusBadge.textContent = "確認済";
        refs.statusBadge.dataset.state = "confirmed";
      }} else {{
        refs.statusBadge.textContent = "未確認";
        refs.statusBadge.dataset.state = "unconfirmed";
      }}

      if (validation.valid) {{
        const delta = validation.quantity - item.book_quantity;
        if (delta === 0) {{
          refs.deltaBadge.textContent = "差異なし (±0)";
          refs.deltaBadge.dataset.diff = "false";
        }} else if (delta > 0) {{
          refs.deltaBadge.textContent = `差異 +${{delta}} ${{item.unit}}`;
          refs.deltaBadge.dataset.diff = "true";
        }} else {{
          refs.deltaBadge.textContent = `差異 ${{delta}} ${{item.unit}}`;
          refs.deltaBadge.dataset.diff = "true";
        }}
        refs.errorEl.textContent = "";
        refs.errorEl.hidden = true;
      }} else {{
        refs.deltaBadge.textContent = "実数未確定";
        refs.deltaBadge.dataset.diff = "false";
        refs.errorEl.textContent = validation.errors.join(" / ");
        refs.errorEl.hidden = false;
      }}

      refs.confirmBtn.disabled = !validation.valid || itemState.confirmed;
      refs.unconfirmBtn.disabled = !itemState.confirmed;
    }}

    function updateOverallProgress() {{
      let confirmedCount = 0;
      let invalidCount = 0;

      for (const item of SHEET.items) {{
        const key = String(item.item_id);
        const itemState = state[key];
        const validation = validateItemState(itemState.actual, itemState.note, itemState.badInput);
        if (!validation.valid) {{
          invalidCount += 1;
        }} else if (itemState.confirmed) {{
          confirmedCount += 1;
        }}
      }}

      const total = SHEET.items.length;
      progressTextEl.textContent = `確認済 ${{confirmedCount}} / ${{total}} 件`;
      progressBarEl.value = confirmedCount;
      progressBarEl.max = total;

      const allComplete = total > 0 && confirmedCount === total && invalidCount === 0;
      copyResultBtn.disabled = !allComplete;
      saveResultBtn.disabled = !allComplete;
      showResultBtn.disabled = !allComplete;

      if (allComplete) {{
        completionStatusEl.textContent =
          `全 ${{total}} 件の確認が完了しました。結果を出力できます。`;
      }} else {{
        const unconfirmedCount = total - confirmedCount;
        const parts = [`未確認が ${{unconfirmedCount}} 件あります`];
        if (invalidCount > 0) {{
          parts.push(`入力エラーが ${{invalidCount}} 件あります`);
        }}
        completionStatusEl.textContent = parts.join(" / ") + "（全件確認後に結果を出力できます）";
        hideResultOutput();
      }}
    }}

    function adjustQuantity(item, deltaStep) {{
      const key = String(item.item_id);
      const itemState = state[key];
      const validation = validateItemState(itemState.actual, itemState.note, false);
      const base = validation.quantity !== null ? validation.quantity : item.book_quantity;
      const next = Math.max(0, Math.min(MAX_STOCK_QUANTITY, base + deltaStep));
      itemState.actual = String(next);
      itemState.badInput = false;
      itemState.confirmed = false;
      hideResultOutput();
      saveState();
      updateCardUI(item, true);
      updateOverallProgress();
    }}

    function buildCards() {{
      itemsContainerEl.replaceChildren();
      cardRefs.clear();
      const fragment = document.createDocumentFragment();

      for (const item of SHEET.items) {{
        const key = String(item.item_id);

        const card = document.createElement("section");
        card.className = "item-card";
        card.dataset.itemId = String(item.item_id);

        const header = document.createElement("div");
        header.className = "card-header";

        const title = document.createElement("h2");
        title.className = "card-title";
        const codeSpan = document.createElement("span");
        codeSpan.className = "card-code";
        codeSpan.textContent = item.code;
        const nameSpan = document.createElement("span");
        nameSpan.textContent = item.name;
        title.append(codeSpan, nameSpan);

        const badges = document.createElement("div");
        badges.className = "card-badges";
        const deltaBadge = document.createElement("span");
        deltaBadge.className = "delta-badge";
        const statusBadge = document.createElement("span");
        statusBadge.className = "status-badge";
        badges.append(deltaBadge, statusBadge);

        header.append(title, badges);

        const meta = document.createElement("div");
        meta.className = "card-meta";
        const partSpan = document.createElement("span");
        partSpan.textContent = `型番: ${{item.manufacturer_part_number || "未設定"}}`;
        const locSpan = document.createElement("span");
        locSpan.textContent = `保管場所: ${{item.location_name || "未設定"}}`;
        const bookSpan = document.createElement("span");
        bookSpan.textContent = `帳簿数量: ${{item.book_quantity}} ${{item.unit}}`;
        meta.append(partSpan, locSpan, bookSpan);

        const qtyRow = document.createElement("div");
        qtyRow.className = "qty-controls";

        const minus10Btn = document.createElement("button");
        minus10Btn.type = "button";
        minus10Btn.className = "btn-step";
        minus10Btn.textContent = "-10";
        minus10Btn.addEventListener("click", () => adjustQuantity(item, -10));

        const minus1Btn = document.createElement("button");
        minus1Btn.type = "button";
        minus1Btn.className = "btn-step";
        minus1Btn.textContent = "-1";
        minus1Btn.addEventListener("click", () => adjustQuantity(item, -1));

        const actualInput = document.createElement("input");
        actualInput.type = "number";
        actualInput.className = "input-actual";
        actualInput.min = "0";
        actualInput.max = String(MAX_STOCK_QUANTITY);
        actualInput.step = "1";
        actualInput.inputMode = "numeric";
        actualInput.setAttribute("aria-label", `${{item.code}} 実数`);
        actualInput.value = state[key].actual;
        actualInput.addEventListener("input", () => {{
          const current = state[key];
          current.actual = actualInput.value;
          current.badInput = Boolean(actualInput.validity && actualInput.validity.badInput);
          current.confirmed = false;
          hideResultOutput();
          saveState();
          updateCardUI(item, false);
          updateOverallProgress();
        }});

        const unitSpan = document.createElement("span");
        unitSpan.className = "unit-label";
        unitSpan.textContent = item.unit;

        const plus1Btn = document.createElement("button");
        plus1Btn.type = "button";
        plus1Btn.className = "btn-step";
        plus1Btn.textContent = "+1";
        plus1Btn.addEventListener("click", () => adjustQuantity(item, 1));

        const plus10Btn = document.createElement("button");
        plus10Btn.type = "button";
        plus10Btn.className = "btn-step";
        plus10Btn.textContent = "+10";
        plus10Btn.addEventListener("click", () => adjustQuantity(item, 10));

        qtyRow.append(minus10Btn, minus1Btn, actualInput, unitSpan, plus1Btn, plus10Btn);

        const noteInput = document.createElement("textarea");
        noteInput.rows = 1;
        noteInput.className = "input-note";
        noteInput.placeholder = "備考・メモ (最大500文字)";
        noteInput.setAttribute("aria-label", `${{item.code}} メモ`);
        noteInput.value = state[key].note;
        noteInput.addEventListener("input", () => {{
          const current = state[key];
          current.note = noteInput.value;
          current.confirmed = false;
          hideResultOutput();
          saveState();
          updateCardUI(item, false);
          updateOverallProgress();
        }});

        const errorEl = document.createElement("p");
        errorEl.className = "item-error";
        errorEl.setAttribute("role", "alert");
        errorEl.hidden = true;

        const actionsRow = document.createElement("div");
        actionsRow.className = "card-actions";

        const bookOkBtn = document.createElement("button");
        bookOkBtn.type = "button";
        bookOkBtn.className = "btn-book-ok";
        bookOkBtn.textContent = `✓ 帳簿通り (${{item.book_quantity}} ${{item.unit}}) で OK`;
        bookOkBtn.addEventListener("click", () => {{
          const current = state[key];
          current.actual = String(item.book_quantity);
          current.badInput = false;
          const check = validateItemState(current.actual, current.note, false);
          current.confirmed = check.valid;
          hideResultOutput();
          saveState();
          updateCardUI(item, true);
          applyFilters();
          updateOverallProgress();
        }});

        const confirmBtn = document.createElement("button");
        confirmBtn.type = "button";
        confirmBtn.className = "btn-confirm";
        confirmBtn.textContent = "この実数で確認";
        confirmBtn.addEventListener("click", () => {{
          const current = state[key];
          const check = validateItemState(current.actual, current.note, current.badInput);
          if (!check.valid) {{
            current.confirmed = false;
          }} else {{
            current.actual = String(check.quantity);
            current.confirmed = true;
          }}
          hideResultOutput();
          saveState();
          updateCardUI(item, true);
          applyFilters();
          updateOverallProgress();
        }});

        const unconfirmBtn = document.createElement("button");
        unconfirmBtn.type = "button";
        unconfirmBtn.className = "btn-unconfirm";
        unconfirmBtn.textContent = "確認解除";
        unconfirmBtn.addEventListener("click", () => {{
          const current = state[key];
          current.confirmed = false;
          hideResultOutput();
          saveState();
          updateCardUI(item, false);
          applyFilters();
          updateOverallProgress();
        }});

        actionsRow.append(bookOkBtn, confirmBtn, unconfirmBtn);
        card.append(header, meta, qtyRow, noteInput, errorEl, actionsRow);
        fragment.appendChild(card);

        cardRefs.set(item.item_id, {{
          card,
          statusBadge,
          deltaBadge,
          actualInput,
          noteInput,
          errorEl,
          bookOkBtn,
          confirmBtn,
          unconfirmBtn,
        }});
        updateCardUI(item, false);
      }}

      itemsContainerEl.appendChild(fragment);
      applyFilters();
      updateOverallProgress();
    }}

    function crc32(value) {{
      let crc = 0xffffffff;
      for (const byte of new TextEncoder().encode(value)) {{
        crc ^= byte;
        for (let bit = 0; bit < 8; bit += 1) {{
          crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
        }}
      }}
      return ((crc ^ 0xffffffff) >>> 0).toString(16).padStart(8, "0");
    }}

    function createPayload() {{
      const entries = [];
      for (const item of SHEET.items) {{
        const key = String(item.item_id);
        const itemState = state[key];
        const validation = validateItemState(itemState.actual, itemState.note, itemState.badInput);
        if (!itemState.confirmed || !validation.valid) {{
          throw new Error("未確認または入力エラーの品目があります");
        }}
        entries.push({{
          item_id: item.item_id,
          actual_quantity: validation.quantity,
          note: validation.normalizedNote,
        }});
      }}
      return {{
        v: 1,
        session_id: SHEET.session_id,
        client_id: SHEET.client_id,
        entries,
      }};
    }}

    function createResultText() {{
      const json = JSON.stringify(createPayload());
      const bytes = new TextEncoder().encode(json);
      let binary = "";
      const chunkSize = 0x8000;
      for (let i = 0; i < bytes.length; i += chunkSize) {{
        binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
      }}
      const encoded = btoa(binary);
      return `IMM1:${{encoded}}.${{crc32(encoded)}}:END`;
    }}

    // file: などの安全でないコンテキストでは navigator.clipboard が使えないための代替
    function copyTextLegacy(text) {{
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "0";
      area.style.left = "0";
      area.style.opacity = "0";
      document.body.appendChild(area);
      try {{
        area.focus();
        area.select();
        area.setSelectionRange(0, text.length);
        return document.execCommand("copy");
      }} catch {{
        return false;
      }} finally {{
        area.remove();
      }}
    }}

    function showResultInTextarea(message) {{
      const code = createResultText();
      resultTextEl.value = code;
      resultBoxEl.hidden = false;
      resultTextEl.hidden = false;
      resultFeedbackEl.textContent = message;
      resultFeedbackEl.hidden = false;
      resultTextEl.focus();
      resultTextEl.select();
    }}

    function buildDownloadFilename() {{
      const safeClient =
        String(SHEET.client_name).replace(/[\\\\/:*?"<>|\\u0000-\\u001f]+/g, "_") || "client";
      return `棚卸結果_${{safeClient}}_${{SHEET.filename_timestamp}}.json`;
    }}

    document.querySelectorAll(".tab-btn").forEach(btn => {{
      btn.addEventListener("click", () => {{
        statusFilter = btn.dataset.statusFilter || "all";
        document.querySelectorAll(".tab-btn").forEach(other => {{
          const active = other === btn;
          other.classList.toggle("active", active);
          other.setAttribute("aria-pressed", active ? "true" : "false");
        }});
        applyFilters();
      }});
    }});

    locationFilterEl.addEventListener("change", () => {{
      locationFilter = locationFilterEl.value;
      applyFilters();
    }});

    searchInputEl.addEventListener("input", () => {{
      keywordFilter = searchInputEl.value.trim().toLowerCase();
      applyFilters();
    }});

    resetStateBtn.addEventListener("click", () => {{
      state = createInitialState();
      setRestoreError(false);
      hideResultOutput();
      saveState();
      for (const item of SHEET.items) {{
        updateCardUI(item, true);
      }}
      applyFilters();
      updateOverallProgress();
    }});

    showResultBtn.addEventListener("click", () => {{
      try {{
        showResultInTextarea("結果テキストを表示しました。長押しでコピーしてください。");
      }} catch (err) {{
        resultFeedbackEl.textContent = String(err);
        resultFeedbackEl.hidden = false;
      }}
    }});

    copyResultBtn.addEventListener("click", () => {{
      let code = "";
      try {{
        code = createResultText();
      }} catch (err) {{
        resultFeedbackEl.textContent = String(err);
        resultFeedbackEl.hidden = false;
        return;
      }}

      const onCopied = () => {{
        resultFeedbackEl.textContent =
          "結果テキストをクリップボードにコピーしました。" +
          "貼り付けできない場合は「結果テキストを表示」も利用できます。";
        resultFeedbackEl.hidden = false;
      }};
      const onFailed = () => {{
        showResultInTextarea(
          "クリップボードへ直接コピーできないため、結果テキストを表示しました。" +
          "長押しでコピーしてください。"
        );
      }};

      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {{
        navigator.clipboard.writeText(code).then(onCopied, () => {{
          if (copyTextLegacy(code)) {{
            onCopied();
          }} else {{
            onFailed();
          }}
        }});
      }} else if (copyTextLegacy(code)) {{
        onCopied();
      }} else {{
        onFailed();
      }}
    }});

    saveResultBtn.addEventListener("click", () => {{
      try {{
        const payload = createPayload();
        const jsonText = JSON.stringify(payload, null, 2);
        const blob = new Blob([jsonText], {{ type: "application/json" }});
        if (!window.URL || typeof window.URL.createObjectURL !== "function") {{
          throw new Error("createObjectURL is unavailable");
        }}
        const url = window.URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = buildDownloadFilename();
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => {{
          try {{
            window.URL.revokeObjectURL(url);
          }} catch {{
            // ignore revoke errors
          }}
        }}, 1000);
        resultFeedbackEl.textContent =
          "結果ファイル(.json)の保存処理を実行しました。" +
          "保存できない場合や反応がない場合は「結果テキストを表示」からコピーしてください。";
        resultFeedbackEl.hidden = false;
      }} catch {{
        showResultInTextarea(
          "ファイル保存に失敗したため、結果テキストを表示しました。" +
          "長押しでコピーしてください。"
        );
      }}
    }});

    populateLocationFilter();
    loadSavedState();
    buildCards();
  </script>
</body>
</html>
"""
