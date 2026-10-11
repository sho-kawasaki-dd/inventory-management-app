# Phase 5 実装計画書(販売ページ導線・CSV・バックアップ/復元・表示設定)

- 作成日: 2026-10-11
- ステータス: 承認
- 作業ブランチ: `feature/phase5-url-csv`(5a)→ `feature/phase5-backup-restore`(5b)→ `feature/phase5-display-settings`(5c)
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(3.4・3.5・5.6・5.8・5.9・6.2・6.3・6.4・7.1・7.2・7.3・8・9 章)、[実装計画書_Phase2](実装計画書_Phase2.md)、[実装計画書_Phase3](実装計画書_Phase3.md)、[実装計画書_Phase4](実装計画書_Phase4.md)
- 改訂履歴: 2026-10-11 初版。販売ページ導線の範囲、CSV の出力対象と導線、復元時の接続の扱い、長時間処理の実行方式、「既定に戻す」の範囲、バックアップの保存先指定、PR 分割を決定。同日レビュー反映: 再起動の成功判定、CSV の拡張子補完後の上書き確認、バックアップ出力エラー、原価列の期待値、表示設定の読み込み失敗、CSV 絞り込みテストを明確化。同日レビュー反映2: CSV・バックアップ・復元のログ記録タスク化、復元成功・失敗時の UI 活性制御とメッセージ表示順序の明確化、設定ダイアログ「既定に戻す」の確定タイミング(OK 押下時)、テスト用 AppContext 生成ヘルパーの共通化を追加

---

## 1. 目的

- 一覧・メニュー・ツールバー・右クリックから、選択した品目の販売ページを既定ブラウザで開けるようにする(開発計画書 5.6。低在庫ドックのボタンは Phase 4 で実装済み)。
- 在庫一覧・履歴を CSV で出力できるようにする(開発計画書 5.8)。
- 手動バックアップと、検査付きの復元(PC 移行を含む)を UI から実行できるようにする。復元成功後は接続とロックを解放してアプリを再起動する(開発計画書 7.2)。
- 年度開始月・フォント・文字サイズを設定ダイアログから変更でき、フォントと文字サイズは `ui_settings.json` に保存して再起動後も保持する(開発計画書 5.9)。
- 文字サイズ 150% でも 1366×768 で画面が崩れないことを、主要な全ダイアログで検証する(開発計画書 6.4)。

## 2. 要件

### 2.1 満たすべき仕様

- 開発計画書 5.6(販売ページ)、5.8(CSV)、5.9(設定)、6.2(メニュー・ツールバー)、6.3(設定ダイアログ)、6.4(表示要件)、7.2(バックアップ・復元)、7.3(ログ)に従う。
- 完了条件(開発計画書 9 章): 復元フロー(7.2)が動作し、不正ファイルを拒否する。移行・上書き失敗時の保全/復旧と再起動失敗時の案内を確認する。フォント・文字サイズを変更して即時反映でき、再起動後も保持される。不正な設定ファイルでも既定値で起動でき、文字サイズ 150% でも 1366×768 で画面が崩れない。
- `core/` と `db/` は PySide6 を import しない。UI は `sqlite3`・`inventory_manager_mini.db` を import せず、Service(`ReportService`・`BackupService`・`SettingsService` を含む)のみを呼ぶ。
- 日時は DB では UTC、CSV・ファイル名・画面ではローカル時刻とし、時刻の取得・変換は `core/timeutil.py` に集約する(`datetime.now`・`'localtime'` を他で使わない)。
- ネットワーク通信はブラウザ起動以外に行わない。URL は `http`/`https` かつホスト名ありのみ許可し、`QDesktopServices.openUrl` に渡す直前に再検証する。
- UI 表示設定は業務データではないため、`SettingsService`・`settings` テーブル・スキーマ版数に含めず、バックアップ・復元の対象外とする。
- 次のコマンドがローカル(Windows)と CI(Windows/Linux)で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`core/`・`db/` は strict、`ui/` ほかは standard でエラー 0 件)
  - `uv run pytest --cov`(Linux は `QT_QPA_PLATFORM=offscreen`)
  - `uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90`
- テストで実データの保存先(`%APPDATA%` 等)を使用しない。ファイル選択ダイアログ・メッセージボックス・ブラウザ起動・プロセス起動はテストで差し替える。

### 2.2 既存実装の状況(調査結果)

| 項目 | 状況 |
| --- | --- |
| `BackupService` | `create_backup(conn, dest_dir)`・`prepare_restore(source_path)`・`apply_restore(prepared, current_db_path, backup_dir)`・`cancel(prepared)`、`PreparedRestore`(件数・最終履歴日時・元の版数を保持)、`InvalidBackupError`・`RestoreError(stage, recovered, backup_path)` は実装済み。接続・ロックの管理は持たない(開発計画書 7.2 の役割分担どおり UI・アプリ起動管理が担当) |
| `ReportService` CSV | `export_items_csv(path, filter)`・`export_history_csv(path, item_id=None)`・`export_dashboard_csv(...)` は実装済み。UTF-8 BOM・CRLF・全項目クォート・数式対策・一時ファイル経由の置換を実装済み |
| 履歴 CSV | 「原価金額(出庫・廃棄のみ)」列が未出力。開発計画書 5.8 の列定義に合わせて追加が必要 |
| CSV 出力の失敗 | 書き込み先が使用中・権限なしなどの `OSError` は業務例外に変換されていない |
| `SettingsService` | `get_fiscal_year_start_month`・`set_fiscal_year_start_month`・`validate_all` は実装済み |
| `config.py` | `AppPaths` に `data_dir`・`db_path`・`backup_dir`・`lock_path`・`log_dir`・`log_path` がある。`ui_settings.json` のパスは未定義 |
| `AppContext` | `inventory`・`master`・`settings`・`data_bus`・`db_path`・`schema_version`・`app_version`。`ReportService`・`BackupService`・保存先パスは未保持 |
| `app.py` | 起動シーケンスは実装済み。復元後の再起動・異常終了の制御、表示設定の適用は未実装 |
| `MainWindow` | ファイルメニューは「終了」のみ。「ツール」メニューなし。販売ページを開く操作は未実装 |
| 150% 検証 | `MainWindow`・`HistoryDialog`・`StockMoveDialog`・`LowStockNoticeDialog` で実施済み。`ItemDialog`・`MasterDialog`・取り消し確認・カテゴリ選択は未検証 |

### 2.3 決定事項

| 項目 | 決定 |
| --- | --- |
| PR 分割 | 5a(販売ページ導線・CSV)→ 5b(バックアップ・復元・再起動)→ 5c(表示設定・設定ダイアログ・150% 検証・文書)の 3 PR。各 PR は CI 成功後にマージし、後続は直前のマージ後の最新の `main` から作成する |
| 販売ページの導線 | 品目メニュー・一覧の右クリックメニュー・ツールバー(参照グループ)で 1 つの `QAction`(「販売ページを開く」)を共有する。一覧に販売ページのボタン列は追加しない(1366×768 での横幅確保) |
| 販売ページの活性条件 | 品目が選択され、かつその品目に URL がある場合。廃止品目でも開ける。押下時に `validate_purchase_url` で再検証し、不正なら `run_guarded` で表示して開かない。通過した URL のみ `QDesktopServices.openUrl` に渡す |
| 在庫一覧 CSV の対象 | MainWindow の現在の検索・絞り込み条件(`ItemFilter`)と同じ。全件は条件解除で出力する。一覧の更新と同じ条件を 1 か所で生成して共用する |
| 履歴 CSV の導線 | ファイルメニュー(全品目の全履歴)と、履歴ビューの「CSV 出力」ボタン(当該品目の履歴)の 2 か所 |
| CSV の保存先・名前 | `QFileDialog.getSaveFileName`。既定名は `在庫一覧_<ローカル日時>.csv`・`履歴_<ローカル日時>.csv`(日時は `local_timestamp_for_filename`)。キャンセル時は何も出力しない |
| CSV の上書き確認 | 通常の `.csv` 選択は保存ダイアログ側の上書き確認に任せる。ダイアログ終了後の拡張子補完でパスが変わり、補完後のファイルが存在する場合だけ追加確認する(既定ボタンは「いいえ」)。拒否・キャンセル時は出力しない。MainWindow と履歴ビューで共通化する |
| ツールバー | 区切り線で分けた「出力」グループに「CSV 出力」(ドロップダウン: 在庫一覧 / 履歴(全品目))を置く。印刷は Phase 7 で追加する。「参照」グループに「販売ページ」を追加する |
| バックアップの保存先 | `QFileDialog.getExistingDirectory`(既定は `paths.backup_dir`)。ファイル名は `inventory_YYYYMMDD_HHMMSS.db` を自動生成(`create_backup` の既存契約) |
| バックアップの出力エラー | CSV と同じ `FileOutputError` を使用する。保存先ディレクトリ作成・DB コピー時の `OSError` を変換し、保存先・原因をメッセージに含め、元例外を保持する。同名ファイルの既存 `ValidationError` は維持し、SQLite 例外は一括変換しない |
| 長時間処理 | UI スレッドで同期実行し、待機カーソル(`QApplication.setOverrideCursor`)とステータスバーのメッセージを表示する。バックアップ作成・復元検査・上書きが対象。非同期化・進捗ダイアログは行わない |
| 復元時の現行接続と UI 状態 | 上書き(`apply_restore`)中も現行の接続は開いたまま、一切操作しない。MainWindow は無効化(`setEnabled(False)`)して操作を停止する。成功時はウィンドウ操作可能状態(または前面表示)でメッセージボックスを表示し、OK 押下後に終了要求フラグ(再起動)をセットして接続を閉じ、ロックを解放して再起動する。上書き失敗後の自動復旧に成功した場合は、操作可能(`setEnabled(True)`)に戻してから `refresh()` して通常操作へ戻す(Service の再構築は不要) |
| 復元の確認 | 検査・準備(`prepare_restore`)に合格した後に確認ダイアログを表示する。既定ボタンは「いいえ」。確認待ちの間も準備済み一時 DB を保持し、キャンセル・例外・完了のいずれでも `try/finally` で破棄する |
| 復元後の再起動 | `main()` のイベントループ終了後、既存の `finally` で接続を閉じロックを解放してから `QProcess.startDetached` を呼ぶ。PyInstaller 版は `sys.executable`、Python 環境では `sys.executable -m inventory_manager_mini`。戻り値 `(bool, int)` を `started, process_id` に分解し、先頭の成功フラグだけを判定する。`started` が偽なら「復元済みです。手動で起動してください」と表示して終了コード 1 で終了する。タプル全体を真偽判定しない |
| 自動復旧に失敗した場合 | DB 操作を再開しない。自動バックアップの場所とログの場所を表示し、再起動せず終了コード 1 で終了する |
| UI 表示設定の保存先 | `AppPaths.ui_settings_path = data_dir / "ui_settings.json"`(開発計画書 3.5) |
| UI 表示設定の読み込み失敗 | 読み取り時の `OSError`・`UnicodeDecodeError`・`JSONDecodeError` は警告ログを残して全項目を既定値へ戻し、起動を継続する。JSON を正常に解析できた場合は項目別に検証し、有効な値を維持する。保存失敗は握りつぶさず呼び出し側へ伝える |
| 設定ダイアログ | 「ツール」メニューに「設定」を追加する。年度開始月(1〜12)・フォント(「OS 既定」または `QFontDatabase.families()`)・文字サイズ(90 / 100 / 125 / 150%)・「既定に戻す」 |
| 「既定に戻す」の範囲と確定 | フォントと文字サイズのみ(OS 既定・100%)。年度開始月は対象外。ダイアログ内の選択肢をリセットするのみで、OK 押下時に初めて保存・即時適用される(キャンセル時は変更が破棄される) |
| 設定の保存順 | OK 押下時に年度開始月(DB・業務検証あり)→ UI 表示設定(JSON)の順で保存する。いずれかが失敗した場合は、どちらが保存済みかをメッセージに含める |
| フォント倍率の基準 | 起動時に取得した OS 既定フォントを基準(100%)とし、現在のフォントに倍率を重ねて掛けない。ポイント指定が使えないフォントはピクセルサイズで拡縮する |
| ダイアログの固定寸法 | 文字サイズ 150% で作業領域に収まらないダイアログは `QScrollArea` で包むか固定寸法を `sizeHint` に任せる |
| ログ | CSV 出力・バックアップ・復元の開始と結果を INFO で記録する。失敗は例外とともに記録する。個人情報や DB 内容は記録しない |

### 2.4 対象外

- ダッシュボードおよびその CSV・PNG 出力(Phase 6)、印刷(Phase 7)、パッケージング(Phase 8)
- CSV インポート、自動世代バックアップ、起動時・終了時の自動バックアップ
- 一覧への販売ページのボタン列
- バックアップ・復元の非同期化・進捗率表示・キャンセル
- 印刷用フォントの固定(Phase 7 で UI 設定から独立させる)
- 設定ダイアログの設定項目の追加(年度開始月・フォント・文字サイズ以外)

## 3. タスク

### 5a. 販売ページ導線・CSV(`feature/phase5-url-csv`)

前提: `main` の最新から作成する。

#### A. core の改修

- [x] `core/errors.py` に `FileOutputError(DomainError)` を追加する(出力先に書き込めない場合。5b のバックアップでも共用し、UI は既存の `DomainError` 処理で表示する)
- [x] `core/reports.py` の `_write_csv` で `OSError` を `FileOutputError` に変換する(一時ファイルは `finally` で必ず削除し、既存ファイルは壊さない。保存先・原因をメッセージに含め、`raise ... from error` で元例外を保持する)
- [x] `export_history_csv` に「原価金額」列を「単価」の後ろに追加する(開発計画書 5.8 の列順)。出庫・廃棄とそれらの取り消しは、保存済みの `cost_amount` をそのまま出力する。取り消し行の値は元行の符号反転済みなので、再度符号反転しない。入庫・返品・棚卸およびそれらの取り消しは空欄とする。原価 0 は `"0"` として空欄と区別し、数値セルは数式対策の対象外とする
- [x] `tests/test_csv.py`
  - [x] 原価金額の列順が「単価」の直後であること。出庫・廃棄は保存済み原価、各取り消しは符号反転した負の原価、入庫・返品・棚卸およびそれらの取り消しは空欄であること
  - [x] 原価 0 が `"0"` として出力され、負の原価に数式対策の `'` が付かず、取り消し行の符号が再反転しないこと
  - [x] 書き込み不可の出力先で `FileOutputError` が発生し、失敗後も既存ファイルのバイト列は変更されず、一時ファイルが残らないこと
- [x] `tests/test_errors.py`: `FileOutputError` が `DomainError` 派生であること
- [x] `tests/test_dependency_rules.py` が引き続き成功すること

#### B. AppContext の拡張

- [x] `ui/context.py` の `AppContext` に `reports: ReportService` と `paths: AppPaths` を追加する(`BackupService` は 5b で追加)
- [x] `app.py` の `build_context` で `ReportService(conn)` を生成して渡す
- [x] `tests/conftest.py` にテスト用 `AppContext` を生成する共通フィクスチャ／ヘルパー関数(`create_test_context`)を追加し、既存のテストコードで直に生成している箇所を集約・更新する(5b での `BackupService` 追加時の改修工数を抑える)

#### C. 販売ページを開く導線 `ui/main_window.py`

- [x] 「品目」メニューに「販売ページを開く」の `QAction` を追加する(履歴の前、区切り線で分ける)。同じ `QAction` をツールバーの「参照」グループと一覧の右クリックメニューに追加する
- [x] `_update_selection_actions` で活性状態を更新する(品目が選択され URL がある場合のみ活性)。選択変更・再読込・品目編集後のいずれでも追従する
- [x] 押下処理: 選択品目の URL を `run_guarded` 内で `validate_purchase_url` により再検証し、通過した URL のみ `QDesktopServices.openUrl(QUrl(url))` に渡す。`ItemDialog._open_purchase_url` と同じ手順にそろえる
- [x] `tests/test_main_window.py`
  - [x] URL あり・なし・未選択・廃止品目の活性状態
  - [x] `http`/`https` で `openUrl` が呼ばれること(`QDesktopServices.openUrl` を差し替え)
  - [x] 不正な URL(`javascript:`・ホスト名なし)では呼ばれずエラーが表示されること(モデルへ直接データを与えて検証する)
  - [x] メニュー・ツールバー・右クリックメニューで同じ `QAction` が共有され、活性状態とラベルが一致すること

#### D. CSV 出力 `ui/main_window.py`

- [x] `refresh_items` が `ItemFilter` を組み立てる処理を `_current_item_filter()` に切り出し、一覧の更新と CSV 出力で共用する(挙動は変えない)
- [x] 「ファイル」メニューに「CSV 出力」サブメニュー(在庫一覧 / 履歴(全品目))を追加する。ツールバーに区切り線付きの「出力」グループを追加し、同じ `QAction` を持つドロップダウンボタンを置く
- [x] 保存先は `QFileDialog.getSaveFileName`(既定名は `在庫一覧_<日時>.csv`・`履歴_<日時>.csv`、拡張子 `.csv` を補う)。キャンセル時は何もしない。出力は待機カーソルを表示して `run_guarded` 内で実行し、成功時は保存先をステータスバーまたはメッセージで通知する
- [x] ダイアログ終了後の拡張子補完によってパスが変わり、補完後のファイルが存在する場合だけ、最終パスへの上書きを追加確認する(既定ボタンは「いいえ」)。拒否・キャンセル時は何も出力しない。通常の `.csv` 選択はダイアログ側の上書き確認に任せ、二重確認を避ける
- [x] CSV 出力の開始・出力成功・出力失敗(保存先・件数・例外情報など)を INFO / ERROR ログに記録する(個人情報・品目詳細などの DB 内容は出力しない)
- [x] `ui/dialogs/history_dialog.py` に「CSV 出力」ボタンを追加し、当該品目の履歴を `export_history_csv(path, item_id)` で出力する。保存先・拡張子補完・補完後の上書き確認・エラー表示・ログ記録は MainWindow と同じ手順とし、共通の補助関数に切り出す
- [x] `tests/test_main_window.py`・`tests/test_history_dialog.py`(`QFileDialog.getSaveFileName` を差し替え)
  - [x] 在庫一覧 CSV が現在の検索・絞り込み条件(検索語・クライアント・発注主体・カテゴリ・保管場所・低在庫のみ・廃止を含む)を反映すること。全条件をパラメータ化して検証する
  - [x] 親カテゴリ選択で子孫カテゴリの品目も含まれることと、複数条件の組み合わせの代表例を 1 件以上検証すること。期待する品目 ID 集合をテスト内で管理し、一覧と CSV を一意な管理番号で照合して対象品目が一致することを確認する(件数だけの比較にしない)
  - [x] 履歴 CSV(全品目)と当該品目の履歴 CSV の件数が正しいこと
  - [x] キャンセル時にファイルが作られないこと
  - [x] MainWindow と履歴ビューで、拡張子なしの入力に `.csv` が補完されること。補完後のファイルが存在する場合だけ追加確認され、「いいえ」・キャンセルでは既存バイト列が保持され、「はい」では出力されること。通常の `.csv` 選択では追加確認されないこと
  - [x] 出力失敗(読み取り専用の出力先など)でエラー表示され、アプリが継続すること
  - [x] CSV 出力の開始・成功・失敗時のログ出力(INFO / ERROR)が正しく記録されること

#### E. 5a の仕上げ

- [x] `uv run ruff check`・`uv run ruff format --check`・`uv run pyright`・`uv run pytest --cov`・カバレッジ(`core/`・`db/` 90%)が成功する
- [x] 5a の PR を作成し、CI 成功後にマージする

### 5b. バックアップ・復元・再起動(`feature/phase5-backup-restore`)

前提: 5a をマージした最新の `main` から作成する。

#### F. core / AppContext

- [x] UI が `sqlite3` に触れずに現行 DB をバックアップできるよう、`BackupService` に現行接続を保持する仕組み(コンストラクタの任意引数など)と、現行 DB をバックアップするメソッドを追加する。既存の `create_backup(conn, dest_dir)` の契約は変更しない
- [x] `create_backup` の保存先ディレクトリ作成・DB コピー時の `OSError` を、5a で追加した `FileOutputError` に変換する。保存先・原因をメッセージに含め、`raise ... from error` で元例外を保持する。同名ファイルに対する既存の `ValidationError` は維持し、SQLite 例外は一括変換しない
- [x] `ui/context.py` に `backup: BackupService` を追加し、`build_context` で生成する。`tests/conftest.py` の共通ヘルパー(`create_test_context`)を更新して各テストへ波及させる
- [x] `tests/test_backup.py`: 追加メソッドが `create_backup` と同じ結果(ファイル名・内容・検査)を返すこと
- [x] `tests/test_backup.py`: ディレクトリ作成・DB コピーをそれぞれ差し替えて `PermissionError` を発生させ、`FileOutputError` への変換、保存先・原因のメッセージ、元例外の保持を検証する。同名ファイルは引き続き `ValidationError` になること

#### G. バックアップ `ui/main_window.py`

- [x] 「ファイル」メニューに「バックアップ」を追加する。`QFileDialog.getExistingDirectory` で保存先を選び(既定は `paths.backup_dir`)、待機カーソルとステータスバー表示のうえで作成する。完了時に作成したファイルのパスを表示する
- [x] バックアップ作成の開始・完了・失敗(保存先・例外情報など)を INFO / ERROR ログに記録する
- [x] 失敗(整合性検査の不合格・同名ファイルの存在・書き込み不可)は `run_guarded` で原因を表示し、アプリは継続する。書き込み不可は `FileOutputError` の業務例外表示とし、保存先・原因を表示する
- [x] `tests/test_main_window.py`(実ファイル DB。`QFileDialog` を差し替え): 作成成功とパスの表示、キャンセル、保存先が書き込み不可の場合の保存先・原因表示と操作継続、WAL 内の未チェックポイントの確定データがバックアップに含まれること、開始・成功・失敗時のログ出力を検証する。書き込み不可は `PermissionError` の差し替えで再現する

#### H. 復元 `ui/main_window.py`

- [x] 「ファイル」メニューに「復元」を追加する(`QFileDialog.getOpenFileName`、`*.db`)
- [x] 検査・準備: 待機カーソルのうえで `prepare_restore` を実行する。`InvalidBackupError` は理由(最大 20 件)を表示して中止し、現行 DB・復元元を変更しない
- [x] 確認ダイアログ(既定ボタンは「いいえ」): 復元元のパス、元の版数(旧版なら「最新版へ移行済み」と明記)、品目数・履歴数、最終履歴日時(`format_local` でローカル時刻)、現行 DB が置き換わること、復元前に自動バックアップを作成すること、完了後に再起動することを表示する
- [x] キャンセル時・例外時・完了時のいずれでも `try/finally` で `prepared.close()` を呼び、一時ディレクトリを残さない
- [x] 復元準備・上書き・自動復旧の各段階での開始・完了・失敗を INFO / ERROR ログに記録する(DB 内容は出力しない)
- [x] 確定後は MainWindow を無効化(`setEnabled(False)`)して操作を停止し、待機カーソルのうえで `apply_restore(prepared, paths.db_path, paths.backup_dir)` を実行する。現行接続は開いたまま操作しない
- [x] `RestoreError` の扱い
  - [x] `stage="pre_backup"`: 保存先と原因を表示し、MainWindow を `setEnabled(True)` に戻して現行 DB は未変更のまま操作を再開する
  - [x] `stage="overwrite"` かつ `recovered=True`: 自動バックアップのパスと原因を表示し、MainWindow を `setEnabled(True)` に戻して `refresh()` して操作を再開する
  - [x] `stage="recovery"`(`recovered=False`): DB 操作を再開せず、自動バックアップの場所・ログの場所・手動復旧の案内(開発計画書 7.2)を表示して、再起動せず終了コード 1 で終了する要求を出す
- [x] 成功時: MainWindow の無効化を解除(または前面表示を維持)して「復元が完了しました。再起動します」と通知し、OK 押下後に終了要求フラグ(再起動)をセットして `self.close()` / `QApplication.quit()` で終了する
- [x] 復元中に他の操作(在庫操作・メニュー)が受け付けられないことを確認する

#### I. 終了・再起動の制御 `app.py`

- [x] `MainWindow` に終了要求(通常 / 再起動 / 異常終了)を保持する属性を追加し、`QApplication.quit()` で終了する
- [x] `build_restart_command()` を追加する(`sys.frozen` なら `[sys.executable]`、そうでなければ `[sys.executable, "-m", "inventory_manager_mini"]`)
- [x] `main()`: イベントループ終了後、既存の `finally`(接続クローズ → ロック解放)を完了してから、再起動要求があれば `started, process_id = QProcess.startDetached(command[0], command[1:])` で戻り値 `(bool, int)` を分解する。先頭の成功フラグだけを判定し、タプル全体を真偽判定しない。`started` が偽なら復元済みである旨と手動再起動の案内を表示し、終了コード 1 を返す。異常終了要求は終了コード 1 を返し再起動しない
- [x] 再起動後の新プロセスがロックを取得できること(ロック解放が先であること)をテストで確認する
- [x] `tests/test_app.py`
  - [x] 再起動要求時に、接続クローズ・ロック解放の後に `startDetached` が呼ばれること(呼び出し順を記録)。成功時の差し替えは `(True, 12345)` を返し、成功扱いになること
  - [x] `startDetached` の差し替えが `(False, 0)` を返した場合、タプル自体は真でも起動失敗と判定され、復元済みである旨・手動起動の案内が表示され、終了コード 1 になること
  - [x] 異常終了要求では再起動しないこと
  - [x] `build_restart_command` が通常実行・凍結実行で正しいこと

#### J. 5b のテスト(実ファイル DB・`tmp_path`)

- [x] 復元の成功: 検査・確認・自動バックアップ・上書き・再起動要求まで通ること。復元後の DB が復元元と一致し、復元元ファイルの内容が変化しないこと
- [x] 確認で「いいえ」: 現行 DB と復元元が変化せず、一時ディレクトリが残らないこと
- [x] 不正ファイル(版数 0・新版・スキーマ不一致・SQLite 破損・業務整合性違反)が拒否され、現行 DB が変化しないこと
- [x] 旧版(v3 など)の復元元が最新版へ移行されて復元されること
- [x] `pre_backup`・`overwrite`(復旧成功)・`recovery`(復旧失敗)の各失敗の表示と、操作再開の有無
- [x] 上書き失敗後の自動復旧に成功した場合、開いたままの現行接続から復旧後のデータを正しく読めること(WAL の別接続からの `backup()` 上書きでも一貫していること)。成立しない場合は、現行接続を閉じて開き直す方式に切り替え、本書と開発計画書 7.2 を同期する
- [x] 復元準備・上書き・自動復旧の各段階でのログ出力(INFO / ERROR)が記録されること
- [x] 復元で `ui_settings.json` が変化しないこと(5c で追加する設定ファイルがあれば同ファイルのバイト列で確認)

#### K. 5b の仕上げ

- [x] 開発計画書 7.2 の復元フロー・失敗時の対応表と実装を照合し、差異があれば同期する
- [x] `uv run ruff check`・`uv run ruff format --check`・`uv run pyright`・`uv run pytest --cov`・カバレッジ(`core/`・`db/` 90%)が成功する
- [x] 5b の PR を作成し、CI 成功後にマージする

### 5c. 表示設定・設定ダイアログ・150% 検証・文書(`feature/phase5-display-settings`)

前提: 5b をマージした最新の `main` から作成する。

#### L. 設定ファイルのパス `config.py`

- [x] `AppPaths` に `ui_settings_path`(`data_dir / "ui_settings.json"`)を追加し、`from_dirs` と `resolve_paths`(環境変数による上書きを含む)で設定する
- [x] `tests/test_config.py`: 既定のパスと `INVENTORY_MANAGER_MINI_DATA_DIR` 指定時のパス

#### M. 表示設定 `ui/display_settings.py`(新規)

- [x] `DisplaySettings`(`font_family: str | None`、`font_scale_percent: int`)と許容倍率 `(90, 100, 125, 150)`、既定値(`None`・`100`)を定義する
- [x] 読み込み: ファイル読み取り・JSON 解析時の `OSError`(ファイルなし・権限不足など)・`UnicodeDecodeError`(不正な UTF-8)・`JSONDecodeError`(構文不正)は、警告ログを残して全項目を既定値へ戻し、起動を継続する
- [x] JSON 解析成功後の検証: トップレベルが辞書でなければ全項目を既定値へ戻す。型不正(`bool` を整数として受けない)・許容外の倍率・空のフォント名は項目ごとに既定値へフォールバックし、他の項目が有効ならその値を維持する。いずれもログに警告を残す
- [x] 保存: 同じディレクトリの一時ファイルへ書いてから `os.replace` で置換する。失敗時は一時ファイルを削除し、例外を呼び出し側へ伝える
- [x] 適用: 起動時に取得した OS 既定フォントを基準(100%)として保持し、`QFontDatabase.families()` に存在しないフォントは OS 既定へフォールバックして `QApplication.setFont()` で反映する。倍率は基準フォントに対して適用し、重ねがけしない。ポイント指定が使えないフォントはピクセルサイズで拡縮する
- [x] 「既定に戻す」相当の関数(OS 既定・100%)を用意する
- [x] `tests/test_display_settings.py`(`tmp_path`)
  - [x] 保存と再読み込みの往復
  - [x] ファイルなし・JSON 不正・不正な UTF-8・読み取り権限エラーで全項目が既定値へ戻り、警告ログが残ること。権限エラーは読み取り処理を差し替えて `PermissionError` で再現する
  - [x] トップレベルが辞書でない・型不正・許容外の倍率・存在しないフォントでの既定値フォールバックと警告ログ
  - [x] 一方が不正でも他方の有効な値が維持されること
  - [x] 保存失敗時に既存ファイルが壊れず、一時ファイルが残らないこと
  - [x] `QApplication.font()` への適用、再適用で倍率が累積しないこと、既定に戻す動作。テスト後は元のフォントへ戻す

#### N. 起動シーケンス `app.py`

- [x] ログ初期化の後、`MainWindow` の生成前に `ui_settings.json` を読み込んで適用する(開発計画書 5.9)。不正でも起動を継続する
- [x] OS 既定フォントの基準を、設定ダイアログからも使える形で保持する(`AppContext` またはモジュール内の状態)
- [x] コード内で `setFont`・フォントサイズを個別指定している箇所を `grep_search` で洗い出し、アプリ全体のフォント変更に追従するようにする
- [x] `tests/test_app.py`: 設定ファイルが欠落・JSON 不正・不正な UTF-8・読み取り権限エラーでも、既定の表示設定で `main()` が起動を継続すること(権限エラーは設定読み取り処理を差し替えて再現する)。正常な設定では保存済みの倍率で起動時にフォントが適用されること

#### O. 設定ダイアログ `ui/dialogs/settings_dialog.py`(新規)・「ツール」メニュー

- [ ] `SettingsDialog(context, display_settings, parent=None)`: 年度開始月(1〜12 のコンボ)、フォント(「OS 既定」+ `QFontDatabase.families()`)、文字サイズ(90 / 100 / 125 / 150%)、「既定に戻す」、OK / キャンセル。保存済みのフォントが存在しない・値が不正な場合は既定値を選択状態にする
- [ ] 「既定に戻す」はフォントと文字サイズのみ戻す(年度開始月は変更しない)。ボタン押下時はダイアログ内のコンボボックス選択を「OS 既定」・「100%」にリセットするのみで、OK 押下時に初めて保存・即時適用される(キャンセル時は元の設定のまま維持される)
- [ ] OK 押下時の処理: `SettingsService.set_fiscal_year_start_month`(`run_guarded`)→ `ui_settings.json` の保存 → 即時に適用。年度開始月の保存後に JSON 保存が失敗した場合は、年度開始月が保存済みであることをメッセージに含める
- [ ] 年度開始月が変更された場合は `data_bus.data_changed` を発火し、開いている画面が再集計できるようにする(ダッシュボードは Phase 6)
- [ ] `MainWindow` に「ツール > 設定」を追加し、`center_dialog` で配置する。文字サイズ変更後も開いているウィンドウ・ダイアログに反映されること
- [ ] `tests/test_settings_dialog.py`: 初期表示(現在値・不正値のフォールバック)、年度開始月の保存と再読込、フォント・倍率の保存と即時反映、「既定に戻す」押下後にキャンセルした場合は設定が変更・保存されないこと、OK 押下時に既定値で保存・適用されること、保存失敗時のメッセージ、1366×768・文字サイズ 150% で作業領域内に収まること

#### P. 150% での表示確認

- [ ] `tests/conftest.py` に、テスト後にアプリ全体のフォントを元へ戻すフィクスチャと、1366×768 の作業領域を前提にした収まり検証の補助関数を追加する
- [ ] 未検証の画面にテストを追加する: `ItemDialog`(新規・編集)、`MasterDialog`(各タブ)、取り消し確認、カテゴリ選択、`SettingsDialog`、復元の確認ダイアログ。文字サイズ 100%・150% でウィンドウ枠を含めて作業領域に収まり、主要ボタンへ到達できること
- [ ] 収まらないものは `QScrollArea` 化または固定寸法の見直しを行い、ボタンが常に操作できる構成にする
- [ ] 既存の `MainWindow`・履歴・在庫操作・低在庫通知の 150% テストが引き続き成功すること

#### Q. 性能・文書・手動確認・仕上げ

- [ ] `uv run pytest --run-perf tests/test_performance.py`: 表示設定の適用を含めても、起動から一覧の初回描画が 3 秒以内であることを再計測して 4.1 に記録する
- [ ] 参考値として、品目 5,000・履歴 100,000 件の DB での CSV 出力(在庫一覧・履歴)・バックアップ作成・復元準備(`prepare_restore`)の所要時間を計測して 4.1 に記録する
- [ ] 開発計画書の同期: 6.2(ツールバーに「販売ページ」、出力グループは CSV のみで印刷は Phase 7)、5.8(履歴 CSV の列順)、5.9・6.3(設定ダイアログ)、7.2(実装との差異があれば)、9 章(フェーズ 5 の欄)
- [ ] README にバックアップ・復元・CSV 出力・設定(フォント・文字サイズ・年度開始月)・販売ページを開く操作を追記する
- [ ] 手動確認(Windows。`INVENTORY_MANAGER_MINI_DATA_DIR` で一時フォルダを指定)
  - [ ] 一覧・メニュー・ツールバー・右クリックから販売ページがブラウザで開くこと。URL なしでは不活性であること
  - [ ] 在庫一覧・履歴の CSV を Excel で開き、文字化けがなく、日時がローカル時刻であり、数式対策の `'` が付くこと
  - [ ] Excel で CSV を開いたまま同名で上書きしようとしたときのエラー表示
  - [ ] バックアップを作成し、別データ領域のアプリから復元して再起動され、データが引き継がれること(PC 移行相当)
  - [ ] 不正ファイル(テキストファイル・破損 DB・新しい版の DB)が拒否されること
  - [ ] 復元後も `ui_settings.json` が変わらないこと
  - [ ] フォントと文字サイズを変更して即時反映され、再起動後も保持されること。`ui_settings.json` を壊しても既定値で起動できること
  - [ ] 1366×768 の画面で文字サイズ 150% のとき、主要画面・ダイアログが作業領域に収まること
- [ ] `uv run ruff check`・`uv run ruff format --check`・`uv run pyright`・`uv run pytest --cov`・カバレッジ(`core/`・`db/` 90%)が成功する
- [ ] 5c の PR を作成し、CI 成功後にマージする
- [ ] 本書のステータスを更新し、Phase 5 完了(開発計画書 9 章)を確認する

## 4. 計測・確認結果

### 4.1 性能計測

(各 PR の実施後に記録する。計測条件は品目 5,000・履歴 100,000 件の DB(`scripts/generate_dummy_data.py`)。実行は `uv run pytest --run-perf tests/test_performance.py`。)

| 項目 | 目標 | 結果 | 実行環境 |
| --- | --- | --- | --- |
| 起動から一覧の初回描画(表示設定の適用を含む) | 3 秒以内 | 未計測 | |
| 在庫一覧 CSV 出力(5,000 件。参考値) | 参考値を記録 | 未計測 | |
| 履歴 CSV 出力(100,000 件。参考値) | 参考値を記録 | 未計測 | |
| バックアップ作成(参考値) | 参考値を記録 | 未計測 | |
| 復元準備 `prepare_restore`(参考値) | 参考値を記録 | 未計測 | |

### 4.2 手動確認

(5c の手動確認の結果を記録する。)

## 5. リスクと対策

| リスク | 影響 | 対策 |
| --- | --- | --- |
| 開いたままの接続への `backup()` 上書き後に、現行接続の読み取りが不整合になる | 復旧成功後の通常操作で古いデータが見える | 5b の J で自動復旧後の読み取りを検証する。成立しない場合は復旧後に接続を閉じて開き直す方式へ切り替え、開発計画書 7.2 と同期する |
| 同期実行の復元・バックアップ中に UI が固まる | 利用者が異常と誤認する | 待機カーソルとステータスバー表示を出す。履歴 100,000 件での所要時間を 4.1 に記録し、許容できない場合は非同期化を別途検討する |
| 復元後の再起動で旧プロセスのロックが残る | 新プロセスが「既に起動しています」で終了する | 接続クローズ・ロック解放の後に `startDetached` を呼ぶ順序をテストで固定する |
| 倍率を基準フォントではなく現在のフォントに重ねがけする | 設定を繰り返すたびに文字が拡大する | 起動時の OS 既定フォントを基準に保持し、適用の累積がないことをテストで確認する |
| 個別に `setFont`・固定寸法を指定している画面が倍率に追従しない | 150% で文字切れ・はみ出し | 5c の N・P で洗い出して修正し、全ダイアログを 150% でテストする |
| `ui_settings.json` の破損・書き込み途中の中断 | 起動不能・設定消失 | 項目ごとのフォールバックと、一時ファイル経由の置換で防ぐ |
| CSV を Excel が開いている状態での上書き | 出力失敗 | `FileOutputError` に変換して表示し、既存ファイルを壊さない |
