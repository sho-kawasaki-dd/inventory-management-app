# Phase 2 実装計画書(MainWindow・品目 CRUD・マスタ管理・多重起動防止)

- 作成日: 2026-10-04
- ステータス: 案
- 作業ブランチ: `feature/phase2-app-foundation`(2a)→ `feature/phase2-main-window`(2b)→ `feature/phase2-master-dialog`(2c)
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(3.1・3.2・3.5・5.1・5.4・6 章・7.1・7.3・7.5・8・9・10 章)

---

## 1. 目的

- 起動シーケンス(多重起動チェック → ログ初期化 → DB オープン・マイグレーション → MainWindow 表示)を実装し、アプリとして起動・終了できる状態にする。
- Phase 1 の Service API を UI から呼び出し、品目とマスタの登録・編集・廃止(無効化)・削除を画面から行えるようにする。
- 品目 5,000 件規模で一覧表示・検索・絞り込みの性能目標を満たすことを計測で確認する。
- PyInstaller の試験ビルドで、Qt プラグイン(QtCharts・印刷サポート)の同梱を早期に確認する(開発計画書 10 章)。

## 2. 要件

### 2.1 満たすべき仕様

- 開発計画書 3.1(層構成・依存方向)、3.2(`app.py`・`config.py`・`ui/single_instance.py`・`ui/main_window.py`)、3.5(ファイル配置)、5.1(品目)、5.4(マスタ管理)、6 章(Phase 2 対象の画面)、7.1(多重起動防止)、7.3(ログ)、7.5(性能目標のうち一覧・検索・絞り込み)に従う。
- 完了条件(開発計画書 9 章): 品目とマスタの登録・編集・廃止・削除が UI から可能。性能目標(一覧・絞り込み)を達成。
- 開発計画書 8 章の `ui/` 観点のうち Phase 2 対象を自動テストで検証する。
  - 主要画面(MainWindow・ItemDialog・MasterDialog)の起動スモーク
  - 初期数量の記録担当者の必須制御
  - 無効化済みクライアント・発注主体を維持した品目編集
  - 発注主体マスタ 0 件時の ItemDialog(OK 不活性)
  - ItemDialog への発注主体表示、単位の表示のみ
- 多重起動防止の観点(同時起動の拒否(DB を開く前)、正常終了後の再取得、異常終了後のロック回収)を別プロセス + `tmp_path` で検証する。
- 次のコマンドがローカル(Windows)と CI(Windows/Linux)で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`core/`・`db/` は strict、`ui/` ほかは standard でエラー 0 件)
  - `uv run pytest --cov`(Linux は `QT_QPA_PLATFORM=offscreen`)
  - `uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90`
- 依存ルールのテスト(Phase 0 の規則 1〜6)が新規モジュールに対しても成功する。特に `ui/` は `sqlite3`・`inventory_manager_mini.db` を import しない。
- テストで実データの保存先(`%APPDATA%` 等)を使用しない。

### 2.2 決定事項

| 項目 | 決定 |
| --- | --- |
| PR 分割 | 2a(起動基盤: `config.py`・ログ・多重起動防止・`app.py`・UI 共通部品)→ 2b(MainWindow・一覧モデル・カテゴリのツリーコンボ・ItemDialog)→ 2c(MasterDialog・性能計測・試験ビルド・README)の 3 PR。各 PR は CI 成功後にマージし、次のブランチは最新の `main` から作成する |
| 後続フェーズのメニュー | 6.2 のメニュー・ツールバーのうち、Phase 2 で実装する項目のみ作成する。在庫操作・履歴・CSV・印刷・バックアップ・復元・アラートパネル・ダッシュボード・設定・販売ページを開く等は、各フェーズで追加する(不活性の仮項目は置かない) |
| 起動時の低在庫通知 | Phase 4 で実装する。Phase 2 では `app.py` の起動シーケンスで MainWindow 表示後に呼ぶ関数 `show_startup_notifications()` を用意し、処理は空とする |
| 一覧のダブルクリック | Phase 2 では何もしない。Phase 3 で履歴ビューに接続する |
| 保存先の差し替え | 環境変数 `INVENTORY_MANAGER_MINI_DATA_DIR` が設定されていれば、DB・バックアップ・ロックをその直下、ログを `logs/` 配下に置く。未設定時は `platformdirs` で解決する。`main(paths: AppPaths \| None = None)` でも注入できる。テスト・別プロセステストはこれらで `tmp_path` を指定する |
| UI への依存注入 | `ui/` は `sqlite3`・`db/` を import できないため、`app.py` が接続と Service 群を構築し、`ui/context.py` の `AppContext` に格納して MainWindow に渡す。スキーマ版数・DB パス・アプリ版数も `AppContext` 経由で渡す |
| データ変更通知 | `ui/signals.py` の `DataBus(QObject)` に `data_changed = Signal()` を 1 つ置く。Service 呼び出しが成功した画面が発火し、MainWindow がマスタ選択肢と一覧を再読込する(選択中の品目 ID を維持)。Phase 4 の低在庫再判定もこの通知に接続する |
| 検索・絞り込み | 絞り込みは `InventoryService.list_items(ItemFilter)` の SQL で行い、`QSortFilterProxyModel` はソートのみを担う。検索欄は入力から 300ms のデバウンス後に再検索する。その他の絞り込み条件は変更時に即時再検索する |
| ソート | 一覧モデルは `Qt.ItemDataRole.UserRole` で生の値(数値・文字列・`None`)を返し、プロキシの `sortRole` に設定する。`None` は昇順で末尾とする |
| 行の表示 | 廃止行はグレー表示(`ForegroundRole`)。低在庫行の着色は Phase 4 |
| カテゴリ選択 UI | `QComboBox` のポップアップを `QTreeView` にしたツリーコンボ `CategoryComboBox` を作成し、MainWindow の絞り込み(先頭「すべて」)、ItemDialog(先頭項目なし)、カテゴリの親変更(先頭「(最上位)」)で共用する |
| 任意の数値入力 | 参考価格(0 以上)・推奨発注数(1 以上)は `QLineEdit` + `QIntValidator` とし、空欄を `None` とする(参考価格 0 円と未登録を区別するため)。初期数量・閾値は `QSpinBox`(0 以上) |
| URL 検証 | ItemDialog は入力変更ごとに `core.services.validate_purchase_url` を呼び、`ValidationError` ならエラー表示と OK 不活性とする |
| 例外の表示 | `ui/error_handling.py` に集約する。`DomainError` は `message` を警告ダイアログで表示し、ダイアログは開いたままにする。それ以外の例外は `logger.exception` で記録して汎用エラーダイアログを表示する。未捕捉例外は `sys.excepthook` で同様に扱う |
| 起動時のエラー | 多重起動は「既に起動しています」を表示して終了コード 0。`SchemaTooNewError`・`UnsupportedSchemaError`・`MigrationError`(自動バックアップのパスがあれば表示)はエラーダイアログを表示して終了コード 1。その他の例外はログ出力・汎用ダイアログ表示の上で終了コード 1。いずれもロックを解放してから終了する |
| 終了処理 | `QApplication.exec()` の終了後(例外時を含む)に DB 接続を閉じ、ロックを解放する(`try`/`finally`) |
| ログ | `logging` のルートロガーに `RotatingFileHandler`(`app.log`、1 MB × 5 世代、UTF-8、INFO 以上)を設定する。時刻は `logging` の既定(OS ローカル) |
| 性能計測 | `tests/test_performance.py` に計測テストを置き、`--run-perf` オプション指定時のみ実行する(既定はスキップ。CI では実行しない)。結果は本書 4 章に記録する |
| 試験ビルド | 手動でローカル(Windows)ビルドし、成果物はコミットしない。`--specpath build` で spec を `build/` に出力する。正式な spec・リリースワークフローは Phase 8 |
| バージョン情報 | ヘルプ → バージョン情報で、アプリ版(`importlib.metadata.version`)・スキーマ版・DB パスを表示する |

### 2.3 対象外

- 在庫操作(StockMoveDialog)・履歴ビュー・取り消し(Phase 3)
- 低在庫の行着色・アラートドック・起動時通知(Phase 4)
- 販売ページを開く導線、CSV 出力、バックアップ・復元、設定ダイアログ、復元後のロック解放・再起動(Phase 5)
- ダッシュボード(Phase 6)、印刷(Phase 7)
- 正式なパッケージング・インストーラー・リリースワークフロー(Phase 8)
- ウィンドウ位置・列幅などの UI 状態の保存

## 3. タスク

### 2a. 起動基盤(`feature/phase2-app-foundation`)

#### A. パス解決 `config.py`

- [ ] 定数 `DATA_DIR_ENV = "INVENTORY_MANAGER_MINI_DATA_DIR"` を定義する
- [ ] `AppPaths`(`frozen=True, slots=True` の dataclass): `data_dir`、`db_path`(`inventory.db`)、`backup_dir`(`backups/`)、`lock_path`(`inventory.lock`)、`log_dir`、`log_path`(`app.log`)
- [ ] `AppPaths.from_dirs(data_dir: Path, log_dir: Path) -> AppPaths`
- [ ] `resolve_paths(env: Mapping[str, str] = os.environ) -> AppPaths`
  - [ ] 環境変数が空でなければ `data_dir = Path(値)`、`log_dir = data_dir / "logs"`
  - [ ] 未設定時は `user_data_dir(APP_NAME, appauthor=False, roaming=True)`・`user_log_dir(APP_NAME, appauthor=False)`
- [ ] `ensure_dirs(paths: AppPaths) -> None`: `data_dir`・`backup_dir`・`log_dir` を作成する(`parents=True, exist_ok=True`)
- [ ] 開発計画書 3.5 に環境変数による保存先の上書き(テスト・検証用)を追記する
- [ ] `tests/test_config.py`
  - [ ] 環境変数指定時の各パス
  - [ ] 未指定時に `platformdirs` へ `appauthor=False`・`roaming=True` が渡ること(`monkeypatch` で関数を差し替え、実フォルダを作らない)
  - [ ] 空文字の環境変数は未設定として扱うこと
  - [ ] `ensure_dirs` が `tmp_path` 配下にディレクトリを作成すること

#### B. 多重起動防止 `ui/single_instance.py`

- [ ] `SingleInstanceLock(path: Path)`: 内部で `QLockFile` を生成し `setStaleLockTime(0)` を設定する
- [ ] `try_acquire(timeout_ms: int = 100) -> bool`: `tryLock(timeout_ms)` の結果を返す
- [ ] `release() -> None`: 取得済みの場合のみ `unlock()` する(複数回呼んでも安全)
- [ ] `tests/test_single_instance.py`(`tmp_path` 上のロックファイル)
  - [ ] 取得 → 解放 → 再取得できる
  - [ ] 別プロセス(`sys.executable` で起動するヘルパー。ロック取得後に標準出力へ通知して待機)が保持中は取得できない
  - [ ] ヘルパーが正常終了(解放)した後は取得できる
  - [ ] ヘルパーを強制終了(`kill`)した後は、PID の生存確認によりロックを回収して取得できる(`setStaleLockTime(0)` でも回収されることを Windows/Linux で確認。回収されない場合は本書の方針を見直す)
  - [ ] ヘルパーには環境変数でロックパスを渡し、実データの保存先を使わない

#### C. UI 共通部品

- [ ] `ui/context.py`: `AppContext` dataclass(`inventory: InventoryService`、`master: MasterService`、`settings: SettingsService`、`data_bus: DataBus`、`db_path: Path`、`schema_version: int`、`app_version: str`)
- [ ] `ui/signals.py`: `DataBus(QObject)` に `data_changed = Signal()`
- [ ] `ui/error_handling.py`
  - [ ] `show_domain_error(parent, error: DomainError)`: 警告ダイアログで `error.message` を表示する
  - [ ] `show_unexpected_error(parent, error: BaseException)`: `logger.exception` 相当で記録し、「予期しないエラーが発生しました。詳細はログを確認してください。」とログの場所を表示する
  - [ ] `run_guarded(parent, func) -> 戻り値 | None`: `func()` を実行し、`DomainError` と その他の例外を上記で表示して `None` を返す
  - [ ] `install_excepthook(log_path: Path)`: 未捕捉例外をログ出力し、`QApplication` があれば汎用ダイアログを表示する
- [ ] `tests/test_error_handling.py`: `DomainError`・その他の例外それぞれの表示とログ出力(`QMessageBox` は `monkeypatch` で差し替える)

#### D. 起動シーケンス `app.py`

- [ ] `setup_logging(log_path: Path) -> None`: ルートロガーに `RotatingFileHandler(maxBytes=1_000_000, backupCount=5, encoding="utf-8")` を INFO で設定する。二重登録しない
- [ ] `build_context(conn, paths) -> AppContext`: Service 群・`DataBus`・スキーマ版数(`SCHEMA_VERSION`)・アプリ版数を設定する
- [ ] `show_startup_notifications(context, window) -> None`: Phase 4 で低在庫通知を実装する差し込み口(処理なし)
- [ ] `main(paths: AppPaths | None = None) -> int`
  - [ ] `QApplication.instance()` があれば再利用し、なければ生成する(アプリ名を設定)
  - [ ] `paths` 未指定なら `resolve_paths()`。`ensure_dirs()` を実行する
  - [ ] `SingleInstanceLock(paths.lock_path).try_acquire()` に失敗したら「既に起動しています」を表示して 0 を返す(DB・ログには触れない)
  - [ ] `setup_logging()`・`install_excepthook()` を実行し、起動ログ(アプリ版・DB パス)を出力する
  - [ ] `open_database(paths.db_path, paths.backup_dir, backup_timestamp=local_timestamp_for_filename)` で DB を開く
  - [ ] `SchemaTooNewError`・`UnsupportedSchemaError`・`MigrationError` はエラーダイアログを表示して 1 を返す(`MigrationError.backup_path` があれば表示)。その他の例外はログ出力と汎用ダイアログの上で 1 を返す
  - [ ] MainWindow を生成・表示し、`show_startup_notifications()` を呼んでから `exec()` する
  - [ ] `finally` で DB 接続を閉じ、ロックを解放する
- [ ] `__main__.py` は現状(`raise SystemExit(main())`)を維持する
- [ ] `tests/test_app.py`(保存先はすべて `tmp_path`。`exec()` と `QMessageBox` は `monkeypatch` で差し替える)
  - [ ] 正常起動で DB・ログファイルが作成され、MainWindow が表示され、終了後にロックが解放される
  - [ ] ロックを別プロセスが保持中は「既に起動しています」を表示して 0 を返し、DB ファイルを作成しない
  - [ ] 新版 DB(`user_version` を `SCHEMA_VERSION + 1` にした DB)で `SchemaTooNewError` のダイアログを表示して 1 を返し、ロックが解放される
  - [ ] `user_version = 0` の既存 DB で `UnsupportedSchemaError` のダイアログを表示して 1 を返す
  - [ ] `open_database` が `MigrationError(backup_path=...)` を送出した場合、バックアップのパスを表示する
  - [ ] `setup_logging` を 2 回呼んでもハンドラが重複しない
- [ ] 2a の PR を作成し、CI 成功後にマージする

### 2b. MainWindow・品目 CRUD(`feature/phase2-main-window`)

#### E. カテゴリのツリーコンボ `ui/widgets/category_combo.py`

- [ ] `CategoryComboBox(QComboBox)`: `QStandardItemModel` + `QTreeView` をポップアップに設定する
- [ ] `set_categories(categories: list[Category], leading_label: str | None = None)`: `parent_id` から木を構築し、名前順に並べる。`leading_label` があれば先頭に ID `None` の項目を置く。再設定時は選択中の ID を可能な限り維持する
- [ ] `current_category_id() -> int | None`、`set_current_category_id(category_id: int | None)`
- [ ] 子孫の項目を選択した場合も表示テキストを正しく更新する(選択時に `setRootModelIndex` を親に切り替えて `setCurrentIndex` し、ルートへ戻す)
- [ ] 展開矢印のクリックではポップアップを閉じない(ビューのイベントフィルタで判定)
- [ ] 表示テキストはフルパス(`電球 > LED電球`)とし、ツールチップにも設定する
- [ ] シグナル `category_changed(object)` を発火する
- [ ] `tests/test_category_combo.py`: 木構造の構築、子孫の選択と表示テキスト、先頭項目、再設定時の選択維持、存在しない ID の指定

#### F. 品目一覧モデル `ui/models/item_table_model.py`

- [ ] `ItemTableModel(QAbstractTableModel)`: `set_rows(rows: list[ItemRow])` は `beginResetModel`/`endResetModel` で入れ替える
- [ ] 列: 管理番号、品名、メーカー型番、クライアント、発注主体、カテゴリ(フルパス)、保管場所、数量、単位、閾値、推奨発注数、参考価格、仕入先、最終購入日、状態(有効/廃止)。用途は表示しない
- [ ] `DisplayRole`: 数値は 3 桁区切り、参考価格は「1,234円」、最終購入日は `timeutil.local_date` のローカル日付、`None` は空欄
- [ ] `TextAlignmentRole`: 数値列(数量・閾値・推奨発注数・参考価格)は右寄せ
- [ ] `ForegroundRole`: 廃止行はグレー
- [ ] `UserRole`: ソート用の生の値。`row_at(row) -> ItemRow`、`row_of(item_id) -> int | None`
- [ ] `ItemSortProxyModel(QSortFilterProxyModel)`: `sortRole = UserRole`、`None` を昇順で末尾にする `lessThan`
- [ ] `tests/test_item_table_model.py`: 列見出し、表示書式、右寄せ、廃止行の色、ソート(数値・文字列・`None`)、`row_of`

#### G. MainWindow `ui/main_window.py`

- [ ] `MainWindow(context: AppContext)`: タイトル「Inventory Manager mini」、最小サイズは 1366×768 の画面に収まる値
- [ ] 絞り込み欄
  - [ ] 検索欄(プレースホルダ「品名・管理番号・メーカー型番」)。`QTimer`(単発、300ms)でデバウンスして再検索する
  - [ ] クライアント・発注主体(先頭「すべて」。無効化済みも「(無効)」付きで含める)、カテゴリ(`CategoryComboBox`、先頭「すべて」)、保管場所(先頭「すべて」)
  - [ ] 「低在庫のみ」「廃止品目を含む」チェックボックス。「廃止品目を含む」は表示メニューの同名アクションと同期する
  - [ ] 条件から `ItemFilter` を組み立てて `list_items` を呼び、件数をステータスバーに表示する
- [ ] 一覧: `QTableView` + `ItemSortProxyModel`。行単位・単一選択、ソート有効、ダブルクリックは接続しない
- [ ] メニュー
  - [ ] ファイル: 終了
  - [ ] 品目: 新規、編集、廃止/再有効化
  - [ ] マスタ: クライアント、発注主体、担当者、カテゴリ、保管場所(2c で MasterDialog に接続。2b ではメニュー項目を作らない)
  - [ ] 表示: 廃止品目を含む
  - [ ] ヘルプ: バージョン情報(アプリ版・スキーマ版・DB パス)
- [ ] ツールバー: 新規
- [ ] 「編集」「廃止/再有効化」は品目選択時のみ活性。選択品目の状態に応じてラベルを「廃止」/「再有効化」に切り替える
- [ ] 廃止は確認ダイアログ(在庫が残っている場合は数量も表示)の上で `deactivate_item`、再有効化は確認なしで `reactivate_item`。成功時に `data_changed` を発火する
- [ ] `data_changed` 受信時: マスタ選択肢(選択中の値を維持)と一覧を再読込し、選択中の品目 ID の行を再選択する。絞り込みで消えた場合は選択解除
- [ ] Service 呼び出しは `run_guarded` を経由する
- [ ] `app.py` から MainWindow を生成するよう接続する
- [ ] `tests/test_main_window.py`(`seeded_conn` 相当の DB から `AppContext` を構築)
  - [ ] 起動スモーク、初期表示で廃止品目が非表示
  - [ ] 検索のデバウンス(`qtbot.waitUntil`)と、品名・管理番号・メーカー型番の部分一致
  - [ ] クライアント・発注主体・カテゴリ(子孫含む)・保管場所・低在庫のみ・廃止品目を含むの絞り込み
  - [ ] 「廃止品目を含む」のチェックボックスとメニューの同期
  - [ ] 品目未選択時の編集・廃止の不活性、選択時の活性とラベル切替
  - [ ] 廃止・再有効化後の一覧更新と選択維持
  - [ ] 無効化済みマスタが絞り込み選択肢に「(無効)」付きで含まれる

#### H. ItemDialog `ui/dialogs/item_dialog.py`

- [ ] `ItemDialog(context, item_id: int | None = None, parent=None)`: `item_id` が `None` なら新規、指定時は編集
- [ ] フォームを `QScrollArea` に配置し、1366×768 で画面内に収まるようにする
- [ ] 項目(6.3 の順): 管理番号(表示のみ。新規時は「登録時に自動採番」)、クライアント、品名、メーカー型番、用途、カテゴリ(`CategoryComboBox`)、保管場所(先頭「(なし)」)、単位(「個」表示のみ)、初期数量(新規時)/現在数量(編集時、表示のみ)、初期数量の記録担当者(新規時)、閾値、推奨発注数、発注主体と販売ページ URL(並べて配置)、仕入先、参考価格、備考(`QPlainTextEdit`)、最終購入日・購入ロット数(編集時、表示のみ)
- [ ] 選択肢
  - [ ] クライアント・発注主体: 有効なもののみ。編集時、現在の値が無効化済みなら「(無効)」付きで先頭に加え、「現在の○○は無効化されています。変更する場合は有効なものを選択してください」を表示する
  - [ ] 担当者: 有効なもののみ
- [ ] 入力検証と OK の活性制御(エラー内容はダイアログ下部に表示)
  - [ ] クライアント・品名(前後空白除去後)・カテゴリ・発注主体が必須
  - [ ] 発注主体マスタ(有効)が 0 件なら発注主体未選択とし、「先に発注主体マスタを登録してください」を表示する。クライアント・カテゴリが 0 件の場合も同様に案内する
  - [ ] 初期数量が 1 以上なら担当者必須。0 なら担当者欄を不活性化し、選択を解除する
  - [ ] 販売ページ URL を `validate_purchase_url` で検証する
  - [ ] 参考価格・推奨発注数の範囲(`QIntValidator`)と空欄 = `None`
- [ ] OK 押下: 新規は `NewItem` で `create_item`、編集は `ItemUpdate` で `update_item`(空欄の任意文字列は `None`)。`DomainError` はダイアログを閉じずに表示する。成功時に `data_changed` を発火して `accept()` する
- [ ] 最終購入日は `timeutil.local_date` で表示し、`get_purchase_info` の結果がなければ「-」
- [ ] MainWindow の新規・編集から開く。編集後は当該品目を選択状態にする
- [ ] `tests/test_item_dialog.py`
  - [ ] 新規・編集の起動スモーク。単位が「個」の表示のみで入力欄がないこと
  - [ ] 必須未入力・URL 不正(`javascript:`、ホストなし)で OK 不活性とエラー表示
  - [ ] 初期数量 0 で担当者欄不活性、1 以上で担当者未選択なら OK 不活性、選択後に活性。登録後に初期数量の履歴と担当者が記録される
  - [ ] 発注主体マスタ 0 件で OK 不活性と案内表示
  - [ ] 無効化済みクライアント・発注主体を維持した編集が成功し、選択肢に他の無効化済みマスタが出ないこと
  - [ ] 発注主体と販売ページ URL の表示、編集時の最終購入日・購入ロット数の表示
  - [ ] 参考価格 0 と空欄(`None`)の区別
  - [ ] Service の `DomainError`(例: 送信直前に担当者が無効化された)でダイアログが閉じず、メッセージが表示される
  - [ ] ダイアログの `sizeHint` が 1366×768 の作業領域に収まる
- [ ] 2b の PR を作成し、CI 成功後にマージする

### 2c. マスタ管理・性能計測・試験ビルド(`feature/phase2-master-dialog`)

#### I. MasterDialog `ui/dialogs/master_dialog.py`

- [ ] `MasterDialog(context, initial_tab: MasterTab, parent=None)`: タブはクライアント/発注主体/担当者/カテゴリ/保管場所。`MasterTab` は `ui/` 内の `StrEnum`
- [ ] クライアント・発注主体・担当者タブ(共通ウィジェット `SimpleMasterTab` に種別ごとの Service メソッドを渡す)
  - [ ] 一覧は無効化済みを含め、無効行はグレー・「(無効)」表示
  - [ ] ボタン: 追加、名称変更(`QInputDialog`)、無効化、再有効化、削除
  - [ ] 活性制御: 無効化は有効行のみ、再有効化は無効行のみ、削除は `can_delete_*` が真の場合のみ(偽の場合は「使用中のため削除できません。無効化してください」をツールチップ表示)
  - [ ] 削除は確認ダイアログの上で実行する
- [ ] カテゴリタブ
  - [ ] `QTreeWidget` で名称・接頭辞・次番号を表示する
  - [ ] ボタン: 追加(最上位)、子カテゴリ追加、名称変更、親変更、接頭辞変更、削除
  - [ ] 追加は名称・接頭辞を入力する小ダイアログ(接頭辞は英大文字・数字 2〜5 文字の入力補助。最終検証は Service)
  - [ ] 親変更は `CategoryComboBox`(先頭「(最上位)」)で選択し、`CategoryCycleError` を表示する
  - [ ] 接頭辞変更は `next_seq > 1` で不活性(「採番済みのため変更できません」)
  - [ ] 削除は `can_delete_category` が真の場合のみ活性
- [ ] 保管場所タブ: 追加、名称変更、削除(`can_delete_location` で活性制御)
- [ ] 各操作の成功時に `data_changed` を発火し、タブの一覧を再読込する(選択を維持)。Service 呼び出しは `run_guarded` を経由する
- [ ] MainWindow のマスタメニュー 5 項目を追加し、該当タブで MasterDialog を開く
- [ ] `tests/test_master_dialog.py`
  - [ ] 起動スモークと初期タブ
  - [ ] クライアント・発注主体・担当者の追加・名称変更・無効化・再有効化、未使用の削除、使用中の削除ボタン不活性
  - [ ] カテゴリの追加・子追加・親変更(循環はエラー表示)・接頭辞変更(採番済みで不活性)・削除(採番済み・子あり・使用中で不活性)
  - [ ] 保管場所の追加・名称変更・削除(使用中で不活性)
  - [ ] マスタ変更後に MainWindow の絞り込み選択肢と一覧の名称が更新される
  - [ ] 発注主体 0 件 → MasterDialog で登録 → ItemDialog の OK が活性になる流れ

#### J. 性能計測 `tests/test_performance.py`

- [ ] `tests/conftest.py` に `--run-perf` オプションと `perf` マーカーを追加し、未指定時は `perf` テストをスキップする(`pyproject.toml` の `markers` に登録)
- [ ] `scripts/generate_dummy_data.py` の `generate_database()` で、`tmp_path` 上に品目 5,000・履歴 100,000 件の DB を生成するモジュールスコープのフィクスチャ
- [ ] 起動から一覧表示: `open_database` → `AppContext` 構築 → MainWindow 生成・表示 → 一覧の初回描画完了までが 3 秒以内
- [ ] 検索・絞り込み: 検索文字列、クライアント、カテゴリ(子孫含む)、低在庫のみ、廃止品目を含むの各変更で、`list_items` とモデル更新の合計が 0.3 秒以内(デバウンス時間は除く)
- [ ] 一覧のソート(数量列・品名列)が 0.3 秒以内
- [ ] 計測値を出力し、ローカル(Windows)で実行して本書 4.1 に記録する
- [ ] 未達の場合は `ItemRepository.list` の SQL(最終購入日の相関サブクエリ等)・インデックスを見直し、スキーマ変更が必要ならマイグレーション規約に従う

#### K. 試験ビルド(手動)

- [ ] `uv run pyinstaller --noconfirm --onedir --windowed --name inventory-manager-mini --specpath build --hidden-import PySide6.QtCharts --hidden-import PySide6.QtPrintSupport src/inventory_manager_mini/__main__.py` を実行する
- [ ] `dist/inventory-manager-mini/` に QtCharts の DLL と印刷サポートのプラグイン(`printsupport`)が含まれることを確認する
- [ ] 環境変数 `INVENTORY_MANAGER_MINI_DATA_DIR` で一時フォルダを指定して exe を起動し、DB 作成・品目登録・多重起動拒否を確認する
- [ ] 結果(PyInstaller 版・問題点・対処)を本書 4.2 に記録する。成果物(`build/`・`dist/`)はコミットしない

#### L. ドキュメント・仕上げ

- [ ] README に起動手順(Windows/Linux)、環境変数 `INVENTORY_MANAGER_MINI_DATA_DIR`、性能テストの実行方法(`uv run pytest --run-perf tests/test_performance.py`)を追記する
- [ ] 手動確認(Windows): 一時フォルダを指定して起動し、品目・全マスタの登録・編集・廃止(無効化)・削除、二重起動の拒否、1366×768 での全ダイアログの表示を確認する
- [ ] 2c の PR を作成し、CI 成功後にマージする
- [ ] 本書のステータスを更新する

## 4. 計測・確認結果

### 4.1 性能計測

| 項目 | 目標 | 結果 | 実行環境 |
| --- | --- | --- | --- |
| 起動から一覧表示 | 3 秒以内 | | |
| 検索・絞り込み(最大) | 0.3 秒以内 | | |
| ソート(最大) | 0.3 秒以内 | | |

### 4.2 試験ビルド

| 確認項目 | 結果 | 備考 |
| --- | --- | --- |
| QtCharts の同梱 | | |
| 印刷サポートプラグインの同梱 | | |
| exe の起動・DB 作成・品目登録 | | |
| 多重起動の拒否 | | |
