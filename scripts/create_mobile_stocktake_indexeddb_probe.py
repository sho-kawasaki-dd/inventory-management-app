from __future__ import annotations

import argparse
import sys
from pathlib import Path

from inventory_manager_mini.core.mobile_stocktake import render_stocktake_html
from inventory_manager_mini.core.models import MobileStocktakeItem, MobileStocktakeSheet

SESSION_ID = "0123456789abcdef0123456789abcdef"

_PROBE_SECTION_HTML = """
<style>
  #indexeddb-probe {
    max-width: 900px;
    margin: 16px auto 0;
    padding: 12px;
    border: 2px dashed #d97706;
    border-radius: 10px;
    background: #fffbeb;
  }
  #indexeddb-probe h2 { margin: 0 0 8px; font-size: 1rem; }
  #indexeddb-probe p { margin: 6px 0; font-size: 0.85rem; }
  #indexeddb-probe button { margin: 4px 6px 4px 0; padding: 8px 12px; font-size: 0.9rem; }
  #indexeddb-probe label { display: block; margin-top: 10px; font-size: 0.85rem; }
  #indexeddb-probe pre {
    margin: 6px 0;
    padding: 6px;
    background: #ffffff;
    font-size: 0.75rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
<section id="indexeddb-probe" aria-labelledby="probe-title">
  <h2 id="probe-title">IndexedDB・JSON 先行実証</h2>
  <p>架空データを使った動作確認専用です。実データは入力しないでください。</p>
  <p id="probe-status" role="status" aria-live="polite">保存データを確認中です。</p>
  <button id="probe-save" type="button">試験データを保存</button>
  <button id="probe-read" type="button">保存データを読み込む</button>
  <pre id="probe-result"></pre>
  <button id="probe-download" type="button">途中 JSON をダウンロード</button>
  <label for="probe-file">ダウンロードした JSON を選択して File API で読む</label>
  <input id="probe-file" type="file" accept="application/json,.json">
  <p id="probe-file-status" role="status" aria-live="polite"></p>
</section>
"""

_PROBE_SCRIPT_HTML = """
<script>
(() => {
  const sessionId = "__SESSION_ID__";
  const draft = {
    kind: "imm-stocktake-draft",
    v: 1,
    session_id: sessionId,
    client_id: 2,
    started_at: "2026-10-17 02:00:00",
    items: [
      {
        item_id: 201,
        book_quantity: 10,
        actual: "7",
        note: "先行実証",
        confirmed: true,
        badInput: false
      },
      {
        item_id: 202,
        book_quantity: 20,
        actual: "",
        note: "",
        confirmed: false,
        badInput: false
      }
    ]
  };
  const status = document.getElementById("probe-status");
  const result = document.getElementById("probe-result");
  const fileStatus = document.getElementById("probe-file-status");

  function openDatabase() {
    return new Promise((resolve, reject) => {
      if (!window.indexedDB) {
        reject(new Error("このブラウザでは IndexedDB を利用できません。"));
        return;
      }
      const request = indexedDB.open("imm_stocktake_probe", 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore("drafts", { keyPath: "session_id" });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("DB を開けません。"));
      request.onblocked = () => reject(new Error("DB の open が blocked になりました。"));
    });
  }

  function readRecord(database) {
    return new Promise((resolve, reject) => {
      const transaction = database.transaction("drafts", "readonly");
      const request = transaction.objectStore("drafts").get(sessionId);
      let record;
      request.onsuccess = () => { record = request.result; };
      request.onerror = () => reject(request.error || new Error("読み込みに失敗しました。"));
      transaction.oncomplete = () => resolve(record);
      transaction.onerror = () => reject(
        transaction.error || new Error("読込 transaction に失敗しました。")
      );
      transaction.onabort = () => reject(
        transaction.error || new Error("読込 transaction が中断されました。")
      );
    });
  }

  async function loadRecord() {
    let database;
    try {
      database = await openDatabase();
      const record = await readRecord(database);
      if (record) {
        result.textContent = JSON.stringify(record, null, 2);
        status.textContent = "保存データを読み込みました。";
      } else {
        result.textContent = "";
        status.textContent = "この起動元に保存データはありません。";
      }
    } catch (error) {
      status.textContent = `読込失敗: ${error}`;
    } finally {
      database?.close();
    }
  }

  async function saveRecord() {
    let database;
    status.textContent = "保存中です。";
    try {
      database = await openDatabase();
      await new Promise((resolve, reject) => {
        const transaction = database.transaction("drafts", "readwrite");
        transaction.objectStore("drafts").put({
          session_id: sessionId,
          storage_revision: 1,
          draft
        });
        transaction.oncomplete = resolve;
        transaction.onerror = () => reject(transaction.error || new Error("保存に失敗しました。"));
        transaction.onabort = () => reject(
          transaction.error || new Error("保存 transaction が中断されました。")
        );
      });
      status.textContent = "保存 transaction の complete を確認しました。";
      await loadRecord();
    } catch (error) {
      status.textContent = `保存失敗: ${error}`;
    } finally {
      database?.close();
    }
  }

  document.getElementById("probe-save").addEventListener("click", saveRecord);
  document.getElementById("probe-read").addEventListener("click", loadRecord);
  document.getElementById("probe-download").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(draft, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `imm-stocktake-draft-${sessionId}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    fileStatus.textContent =
      "ダウンロードを要求しました。Files でファイルの存在を確認してください。";
  });
  document.getElementById("probe-file").addEventListener("change", event => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const value = JSON.parse(String(reader.result));
        if (
          value.kind !== draft.kind
          || value.session_id !== sessionId
          || JSON.stringify(value) !== JSON.stringify(draft)
        ) {
          throw new Error("試験シートと一致しない JSON です。");
        }
        fileStatus.textContent = "File API で読み込み、JSON とシート識別情報を確認しました。";
      } catch (error) {
        fileStatus.textContent = `読込失敗: ${error}`;
      }
    };
    reader.onerror = () => {
      fileStatus.textContent = "File API でファイルを読み込めませんでした。";
    };
    reader.readAsText(file);
  });
  loadRecord();
})();
</script>
"""


def _sample_sheet() -> MobileStocktakeSheet:
    return MobileStocktakeSheet(
        session_id=SESSION_ID,
        client_id=2,
        client_name="B社・先行実証用",
        staff_id=1,
        staff_name="鈴木",
        started_at="2026-10-17 02:00:00",
        items=(
            MobileStocktakeItem(
                item_id=201,
                code="B-001",
                name="試験品目1",
                manufacturer_part_number=None,
                location_name="倉庫1",
                book_quantity=10,
                unit="個",
            ),
            MobileStocktakeItem(
                item_id=202,
                code="B-002",
                name="試験品目2",
                manufacturer_part_number=None,
                location_name="倉庫1",
                book_quantity=20,
                unit="個",
            ),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="架空データの Android IndexedDB 先行実証用 HTML を生成します"
    )
    parser.add_argument("output", type=Path, help="新規作成する HTML ファイルのパス")
    args = parser.parse_args(argv)
    output_path: Path = args.output
    if output_path.exists():
        print(f"出力先が既に存在します。上書きしません: {output_path}", file=sys.stderr)
        return 1

    try:
        html = render_stocktake_html(_sample_sheet())
        script = _PROBE_SCRIPT_HTML.replace("__SESSION_ID__", SESSION_ID)
        # 本体はページ全体のスクロールが無効なため、内側のスクロール領域へ配置する
        footer_end = "</footer>"
        if html.count(footer_end) != 1 or html.count("</body>") != 1:
            print("生成 HTML の構造が想定と異なります", file=sys.stderr)
            return 1
        html = html.replace(footer_end, f"{footer_end}{_PROBE_SECTION_HTML}", 1)
        output_path.write_text(
            html.replace("</body>", f"{script}</body>", 1),
            encoding="utf-8",
        )
    except OSError as error:
        print(f"HTML を書き込めません: {error}", file=sys.stderr)
        return 1

    print(f"先行実証用 HTML を作成しました: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
