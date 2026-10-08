# Phase 3 実装計画書(在庫操作・履歴・取り消し)

- 作成日: 2026-10-08
- ステータス: 未着手
- 作業ブランチ: `feature/phase3-stock-move`(3a)→ `feature/phase3-history`(3b)
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(3.1・3.2・4.3・5.2・5.3・6 章・7.5・8・9 章)、[実装計画書_Phase2](実装計画書_Phase2.md)

---

## 1. 目的

- 在庫操作(入庫・出庫・返品・廃棄・棚卸)を画面から記録できるようにする。
- 品目ごとの履歴を閲覧でき、誤操作を逆仕訳で取り消せるようにする。取り消せない行は操作不可とし、理由を表示する。
- 在庫操作の保存と再描画が 0.5 秒以内であることを計測で確認する(開発計画書 7.5)。

## 2. 要件

### 2.1 満たすべき仕様

- 開発計画書 3.1(層構成・依存方向)、4.3(カラム運用ルール)、5.2(在庫操作)、5.3(取り消し)、6.2(メニュー・ツールバー)、6.3(StockMoveDialog・履歴ビュー・取り消し確認)、7.5(性能目標)に従う。
- 完了条件(開発計画書 9 章): 全操作種別と取り消しが UI から可能。不可条件で操作不可。
- `core/`・`db/` の変更は行わない。Phase 1 の `InventoryService`(`receive`・`issue`・`return_to_supplier`・`dispose`・`stocktake`・`reverse`・`reversal_block_reason`・`list_history`)をそのまま使用する。変更が必要と判明した場合は、本書へ追記してから行う。
- 開発計画書 8 章の `ui/` 観点のうち Phase 3 対象を自動テストで検証する。
  - StockMoveDialog・履歴ビュー・取り消し確認の起動スモーク
  - 取り消し操作者の必須制御
  - 取り消し不可条件での「取り消し」不活性とツールチップ表示
  - 無効化済みクライアント・発注主体・担当者を含む履歴の取り消し
- 次のコマンドがローカル(Windows)と CI(Windows/Linux)で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`core/`・`db/` は strict、`ui/` ほかは standard でエラー 0 件)
  - `uv run pytest --cov`(Linux は `QT_QPA_PLATFORM=offscreen`)
  - `uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90`
- 依存ルールのテストが新規モジュールに対しても成功する。特に `ui/` は `sqlite3`・`inventory_manager_mini.db` を import しない。
- テストで実データの保存先(`%APPDATA%` 等)を使用しない。

### 2.2 決定事項

| 項目 | 決定 |
| --- | --- |
| PR 分割 | 3a(在庫操作: `ui/labels.py`・`StockMoveDialog`・MainWindow の在庫操作導線・性能計測の一部)→ 3b(履歴: `MovementTableModel`・`HistoryDialog`・`ReversalDialog`・履歴導線・ダブルクリック接続・性能計測の残り)の 2 PR。各 PR は CI 成功後にマージし、次のブランチは最新の `main` から作成する |
| 在庫操作の導線 | 品目管理と同様、1 つの `QAction` を在庫メニュー・ツールバー・一覧の右クリックメニューで共有する。ツールバーでは区切り線を挟んで「入庫 / 出庫 / 返品・廃棄(ドロップダウン) / 棚卸」のグループを置く。右クリックメニューには品目管理・在庫操作・参照の順で区切り線付きに並べる(開発計画書 6.2)。活性状態の更新は `_update_selection_actions()` に集約する |
| 在庫操作の活性条件 | 有効品目の選択時のみ活性。廃止品目では不活性 |
| 品目選択 | StockMoveDialog 内に検索欄(`QLineEdit`)と候補リスト(`QListView`)を置く。検索欄は最終入力から 300ms のデバウンス後に `list_items(ItemFilter(text=..., include_inactive=False))` を呼ぶ。検索対象は MainWindow と同じ(品名・管理番号・メーカー型番)。選択中の品目は候補リストとは別のラベル(管理番号・品名・現在数量・参考価格)に保持し、検索結果から外れても選択を維持する。MainWindow で選択中の品目があれば初期選択とする |
| 操作種別 | 起動したアクションの種別を初期選択とし、ダイアログ内のコンボボックスでも切り替えられる。切替時に数量・実数・使用先・単価の入力欄の表示と検証を更新する |
| 数量入力 | 入庫・出庫・返品・廃棄は数量 1 以上、棚卸は実数 0 以上(`QSpinBox`)。棚卸の初期値は現在数量。上限は 999,999,999 とする |
| 単価入力 | 入庫のみ表示する。`QLineEdit` + `QIntValidator`(0 以上)とし、初期値は参考価格(未登録なら空欄)。空欄の場合は `unit_price=None` を渡し、Service が参考価格を適用する |
| 参考価格の更新確認 | 入庫で入力した単価が空欄でなく、参考価格(未登録を含む)と異なる場合、OK 押下時に「参考価格を更新しますか」を「はい / いいえ / キャンセル」で確認する。はいは `update_reference_price=True`、いいえは `False` で記録し、キャンセルは記録せずダイアログを開いたままにする |
| 担当者選択 | 有効な担当者のみ。初期値は未選択(毎回明示的に選択する)。未選択では OK 不活性 |
| 使用先 | 出庫時のみ表示し必須(前後空白除去後)。過去入力値の補完は Phase 3 対象外 |
| 操作後数量プレビュー | 操作後の数量を表示し、棚卸は差分(符号付き)も表示する。結果が負なら OK 不活性とエラー表示 |
| 検証 | UI は OK の活性制御とエラー表示のみを担う。業務ルールの最終判定は Service に任せ、`DomainError` はダイアログを閉じずに表示する(`run_guarded`) |
| 例外の扱い | `ui/error_handling.py` の `run_guarded` を経由する。成功フラグが真の場合のみ `data_changed` を発火してダイアログを閉じる |
| 履歴ビュー | 品目ごとのモーダル `HistoryDialog` とする。複数品目の同時表示や常駐ペインは行わない。全品目の履歴ビューは対象外 |
| 履歴の並び順 | 新しい順(`moved_at` 降順、同時刻は ID 降順)に固定し、列ヘッダによるソートは設けない。`list_history` は昇順で返すため、モデル側で逆順にする |
| 履歴の列 | 日時(`timeutil.format_local`)、種別、数量(符号付き)、単価(「1,234円」)、クライアント、発注主体、担当者、使用先、メモ、取り消し状態(「取り消し済み」/「#ID の取り消し」/ 空欄)。履歴 ID は取り消し状態の参照のため先頭列に表示する |
| 履歴の表示形式 | 数値は右寄せ。取り消し行と取り消された元の行はグレー表示。種別の表示名は `ui/labels.py` に集約し、StockMoveDialog と共用する |
| 取り消しの活性制御 | 行選択ごとに `reversal_block_reason(movement_id)` を呼び、`None` なら「取り消し…」を活性、文字列なら不活性にしてツールチップに表示する。未選択時は不活性。確定時は `reverse` が同じ条件を再検証する |
| 取り消し確認 | `ReversalDialog` に元操作の内容(日時・種別・数量・単価・使用先)と取り消し後数量を表示し、担当者(有効のみ・初期は未選択・必須)と任意メモを入力する。元行のクライアント・発注主体・担当者が無効化済みでも取り消せる(開発計画書 5.3) |
| 取り消し後の再読込 | 取り消し成功時に `data_changed` を発火し、`HistoryDialog` 内の履歴・品目情報を再読込して取り消された元の行を選択し直す。ダイアログは閉じない |
| 履歴の導線 | 品目メニュー・ツールバー(区切り線を挟んだ「参照」グループ)・一覧の右クリックメニューに 1 つの共有 `QAction`「履歴」を置く。品目選択時に活性(廃止品目でも可)。一覧のダブルクリックでも開く(編集には割り当てない) |
| 選択の維持 | 在庫操作・取り消し後は `data_changed` による再読込で選択中の品目 ID を維持する(Phase 2 の仕組みをそのまま使う) |
| 性能計測 | `tests/test_performance.py` に、在庫操作(入庫)の保存と一覧の再描画完了、取り消しの保存と一覧の再描画完了が 0.5 秒以内であることを測る計測テストを追加する(`--run-perf` 指定時のみ実行)。結果は本書 4.1 に記録する |

### 2.3 対象外

- 低在庫の行着色・アラートドック・起動時通知(Phase 4)
- 販売ページを開く導線、CSV 出力(履歴 CSV を含む)、バックアップ・復元、設定ダイアログ(Phase 5)
- ダッシュボード(Phase 6)、印刷(Phase 7)、パッケージング(Phase 8)
- 使用先の過去入力値の補完
- 全品目をまたぐ履歴ビュー、履歴の絞り込み・ソート・期間指定
- 開いている HistoryDialog・StockMoveDialog の他画面変更に対する自動更新
- 入庫時の発注主体の上書き(開発計画書 1.6)

## 3. タスク

### 3a. 在庫操作(`feature/phase3-stock-move`)

#### A. 共通表示名 `ui/labels.py`

- [ ] `REASON_LABELS: dict[Reason, str]`(入庫・出庫・返品・廃棄・棚卸)を定義する
- [ ] `tests/test_labels.py`: 全 `Reason` が定義済みであること

#### B. StockMoveDialog `ui/dialogs/stock_move_dialog.py`

- [ ] `StockMoveDialog(context, reason: Reason, item_id: int | None = None, parent=None)`
- [ ] フォームを `QScrollArea` に配置し、1366×768 の作業領域に収まるようにする
- [ ] 品目選択部
  - [ ] 検索欄(プレースホルダ「品名・管理番号・メーカー型番」)と候補リスト(`QListView`、管理番号・品名・数量を表示)
  - [ ] 検索欄は `QTimer`(単発、300ms)でデバウンスして `list_items(ItemFilter(text=..., include_inactive=False))` を呼ぶ
  - [ ] 選択中の品目を別のラベル(管理番号・品名・現在数量・参考価格)に保持し、候補が変わっても維持する
  - [ ] `item_id` 指定時は初期選択する。品目が取得できない・廃止品目の場合は選択なしで開く
- [ ] 操作種別コンボボックス(`REASON_LABELS`)。`reason` を初期選択とし、切替で入力欄を更新する
- [ ] 数量入力(`QSpinBox`)。入庫・出庫・返品・廃棄は 1 以上、棚卸は「実数」ラベルで 0 以上・初期値は現在数量
- [ ] 使用先(出庫のみ表示・必須)、単価(入庫のみ表示。`QLineEdit` + `QIntValidator`、初期値は参考価格)
- [ ] 担当者コンボボックス(有効のみ、先頭は「(選択してください)」で初期未選択)、メモ(`QPlainTextEdit`、任意)
- [ ] 操作後数量プレビュー(棚卸は差分も表示)
- [ ] 入力検証と OK の活性制御(エラー内容はダイアログ下部の `error_label` に表示)
  - [ ] 品目未選択、担当者未選択、出庫で使用先が空、操作後数量が負、のいずれかで OK 不活性
  - [ ] 品目選択・種別・数量・担当者の変更ごとに再検証する
  - [ ] 有効な担当者が 0 件の場合は「先に担当者マスタを登録してください」を表示する
- [ ] OK 押下
  - [ ] 入庫で単価が空欄でなく参考価格と異なる場合、「はい / いいえ / キャンセル」の確認を表示する(キャンセルはダイアログを開いたまま)
  - [ ] 種別に応じて `receive`・`issue`・`return_to_supplier`・`dispose`・`stocktake` を `run_guarded` 経由で呼ぶ(メモの空欄は `None`)
  - [ ] 成功フラグが真のときのみ `data_changed` を発火して `accept()`。`DomainError` はダイアログを閉じずに表示する
- [ ] `tests/test_stock_move_dialog.py`(`seeded_conn` 相当の DB から `AppContext` を構築)
  - [ ] 起動スモーク、指定した種別・品目が初期選択されること、種別ごとの入力欄の表示切替
  - [ ] 入庫・出庫・返品・廃棄・棚卸の記録内容(`delta`・`reason`・`unit_price`・`used_for`・担当者・メモ)と `items.quantity` の更新
  - [ ] 棚卸の初期値が現在数量であること、差分 0 の記録、差分の表示
  - [ ] 担当者未選択・使用先空欄・操作後数量が負・品目未選択で OK 不活性。条件を満たすと活性
  - [ ] 検索のデバウンス、候補の絞り込み、検索結果から外れても選択が維持されること、廃止品目が候補に出ないこと
  - [ ] 参考価格の確認: 単価が異なる場合の「はい」(参考価格更新)・「いいえ」(更新なし)・「キャンセル」(記録なし・ダイアログ維持)、単価が同じ・空欄の場合は確認が出ないこと
  - [ ] 送信直前に担当者が無効化された場合など Service の `DomainError` でダイアログが閉じずメッセージが表示されること
  - [ ] 成功時に `data_changed` が発火すること(失敗時は発火しないこと)
  - [ ] `sizeHint` が 1366×768 の作業領域に収まること

#### C. MainWindow の在庫操作導線 `ui/main_window.py`

- [ ] 「在庫」メニュー(入庫・出庫・返品・廃棄・棚卸)を品目メニューの次に追加する
- [ ] ツールバーに区切り線を挟んで「入庫 / 出庫 / 返品・廃棄(`QToolButton` のドロップダウン)/ 棚卸」を追加する(メニューと同じ `QAction` を共有)
- [ ] 一覧の右クリックメニューに、品目管理の後ろへ区切り線付きで在庫操作を追加する
- [ ] `_update_selection_actions()` で在庫操作の活性を更新する(有効品目の選択時のみ活性)
- [ ] アクション実行で `StockMoveDialog(context, reason, item_id=選択中の品目)` を開く。成功後は `data_changed` による再読込で選択中の品目を維持する
- [ ] `tests/test_main_window.py`(追記)
  - [ ] メニュー・ツールバー・右クリックメニューが同じ `QAction` を共有すること
  - [ ] 品目未選択・廃止品目の選択時に在庫操作が不活性、有効品目で活性
  - [ ] 在庫操作の完了後に一覧の数量が更新され、選択が維持されること

#### D. 性能計測(3a 分)

- [ ] `tests/test_performance.py` に、入庫の OK 押下から一覧の再描画完了までが 0.5 秒以内であることを測るテストを追加する(品目 5,000・履歴 100,000 件の DB。終了条件は Phase 2 と同じく viewport の描画完了)
- [ ] ローカル(Windows)で実行し、本書 4.1 に記録する
- [ ] 未達の場合は `InventoryService` の処理・`MovementRepository` の SQL とインデックス・再読込処理を見直す(スキーマ変更が必要ならマイグレーション規約に従う)

#### E. 3a の仕上げ

- [ ] 開発計画書 6.2・6.3 に差異があれば同方針で改訂する(StockMoveDialog の入力補完なし、参考価格確認の 3 択など)
- [ ] `uv run ruff check`・`uv run ruff format --check`・`uv run pyright`・`uv run pytest --cov` が成功する
- [ ] 3a の PR を作成し、CI 成功後にマージする

### 3b. 履歴・取り消し(`feature/phase3-history`)

#### F. 履歴モデル `ui/models/movement_table_model.py`

- [ ] `MovementTableModel(QAbstractTableModel)`: `set_rows(rows: list[MovementRow])` は `beginResetModel`/`endResetModel` で入れ替える。行は新しい順に並べ替えて保持する
- [ ] 列: 履歴 ID、日時、種別、数量、単価、クライアント、発注主体、担当者、使用先、メモ、取り消し状態
- [ ] `DisplayRole`: 日時は `timeutil.format_local`、種別は `REASON_LABELS`、数量は符号付き 3 桁区切り、単価は「1,234円」、`None` は空欄、取り消し状態は「取り消し済み」/「#ID の取り消し」/ 空欄
- [ ] `TextAlignmentRole`: 履歴 ID・数量・単価は右寄せ
- [ ] `ForegroundRole`: 取り消し行(`reversal_of` あり)と取り消し済み(`is_reversed`)の行はグレー
- [ ] `row_at(row) -> MovementRow`、`row_of(movement_id) -> int | None`
- [ ] `tests/test_movement_table_model.py`: 列見出し、新しい順、表示書式(ローカル日時・符号・円)、取り消し状態の表示、右寄せ、グレー表示、`row_of`

#### G. HistoryDialog・ReversalDialog

- [ ] `ui/dialogs/history_dialog.py`: `HistoryDialog(context, item_id: int, parent=None)`
  - [ ] 上部に品目情報(管理番号・品名・現在数量・状態)を表示する
  - [ ] `QTableView` + `MovementTableModel`(行単位・単一選択、ソートなし)
  - [ ] 「取り消し…」「閉じる」ボタン。行選択ごとに `reversal_block_reason` を呼び、`None` なら活性、文字列なら不活性にしてツールチップへ表示する。未選択時は不活性
  - [ ] 「取り消し…」で `ReversalDialog` を開き、成功時に履歴と品目情報を再読込して、取り消された元の行を選択し直す
  - [ ] 読み込みは `run_guarded` を経由する
  - [ ] 1366×768 に収まるサイズ
- [ ] `ui/dialogs/reversal_dialog.py`: `ReversalDialog(context, movement: MovementRow, parent=None)`
  - [ ] 元操作の内容(日時・種別・数量・単価・使用先)と取り消し後数量を表示する
  - [ ] 担当者(有効のみ、初期は未選択、必須)・メモ(任意)。担当者未選択で OK 不活性。有効な担当者が 0 件ならその旨を表示する
  - [ ] OK 押下で `reverse(movement_id, staff_id, note)` を `run_guarded` 経由で呼ぶ。成功時のみ `data_changed` を発火して `accept()`。`DomainError` はダイアログを閉じずに表示する
- [ ] `tests/test_history_dialog.py`
  - [ ] 起動スモーク、新しい順の表示、品目情報の表示
  - [ ] 取り消し不可条件ごとに「取り消し…」が不活性でツールチップに理由が表示されること(取り消し行・取り消し済み・差分 0 の棚卸・廃止品目・取り消し後に在庫が負になる行)。可能な行では活性
  - [ ] 未選択時の不活性
  - [ ] 取り消しの成功: 取り消し行の追加、数量の更新、取り消し行の `unit_price`・`used_for`・クライアント・発注主体が元行と一致すること、再読込後に元行が選択されグレー表示になること、`data_changed` が発火すること
  - [ ] ReversalDialog で担当者未選択のとき OK 不活性、選択で活性
  - [ ] 元行のクライアント・発注主体・担当者が無効化済みでも取り消せること
  - [ ] 入庫で参考価格を更新した後に取り消しても、参考価格が戻らないこと
  - [ ] Service の `DomainError`(例: 履歴表示後に別操作で取り消し済みになった)でダイアログが閉じずメッセージが表示されること
  - [ ] `sizeHint` が 1366×768 の作業領域に収まること

#### H. MainWindow の履歴導線 `ui/main_window.py`

- [ ] 共有 `QAction`「履歴」を作成し、品目メニュー・ツールバー(区切り線を挟んだ「参照」グループ)・一覧の右クリックメニュー(在庫操作の後ろに区切り線付き)へ追加する
- [ ] `_update_selection_actions()` で品目選択時に活性にする(廃止品目でも可)
- [ ] 一覧のダブルクリックで履歴を開く(編集には割り当てない)
- [ ] `tests/test_main_window.py`(追記)
  - [ ] 履歴アクションがメニュー・ツールバー・右クリックメニューで共有され、品目選択時のみ活性(廃止品目でも活性)
  - [ ] ダブルクリックで HistoryDialog が開き、編集ダイアログは開かないこと
  - [ ] 履歴ダイアログでの取り消し後に一覧の数量が更新されること

#### I. 性能計測(3b 分)

- [ ] `tests/test_performance.py` に、取り消しの OK 押下から一覧の再描画完了までが 0.5 秒以内であることを測るテストを追加する
- [ ] ローカル(Windows)で実行し、本書 4.1 に記録する

#### J. ドキュメント・仕上げ

- [ ] 開発計画書 6.2・6.3 の履歴導線(ダブルクリック・ツールバーの参照グループ)に差異があれば改訂する。Phase 2 実装計画書 2.2 の「一覧のダブルクリック」の記載は変更しない(Phase 2 時点の決定のため)
- [ ] README に在庫操作・履歴・取り消しの基本操作を追記する(必要な場合のみ)
- [ ] 手動確認(Windows。`INVENTORY_MANAGER_MINI_DATA_DIR` で一時フォルダを指定)
  - [ ] 全種別(入庫・出庫・返品・廃棄・棚卸)の記録と、一覧への反映
  - [ ] 入庫時の参考価格更新確認の 3 択
  - [ ] 履歴の閲覧、取り消し、取り消し不可行の不活性とツールチップ
  - [ ] 一覧のダブルクリックで履歴が開くこと
  - [ ] 1366×768 での全ダイアログの表示
- [ ] `uv run ruff check`・`uv run ruff format --check`・`uv run pyright`・`uv run pytest --cov`・カバレッジ(`core/`・`db/` 90%)が成功する
- [ ] 3b の PR を作成し、CI 成功後にマージする
- [ ] 本書のステータスを更新する

## 4. 計測・確認結果

### 4.1 性能計測

計測条件: 品目 5,000・履歴 100,000 件の DB(`scripts/generate_dummy_data.py`)。実行は `uv run pytest --run-perf tests/test_performance.py`。

| 項目 | 目標 | 結果 | 実行環境 |
| --- | --- | --- | --- |
| 入庫の保存と一覧の再描画 | 0.5 秒以内 | 未計測 | |
| 取り消しの保存と一覧の再描画 | 0.5 秒以内 | 未計測 | |
