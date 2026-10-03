# Phase 1 実装計画書(スキーマ・リポジトリ・サービス層)

- 作成日: 2026-10-02
- ステータス: 承認済
- 作業ブランチ: `feature/phase1-db-foundation`(1a)→ `feature/phase1-services`(1b)→ `feature/phase1-reports-backup`(1c)
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(3・4・5・7.2・7.4・8・9 章)

---

## 1. 目的

- UI を除く全業務ロジック(DB スキーマ、マイグレーション、Repository、Service、集計、CSV、バックアップ・復元)を、PySide6 に依存しない純粋 Python で実装する。
- 開発計画書 5 章の業務ルールと 7.2 の復元手順を、自動テストで網羅的に検証できる状態にする。
- Phase 2 以降の UI が Service を呼ぶだけで全機能を実現できる API を確定する。
- 性能計測(Phase 2・6)に使うダミーデータ生成スクリプトを用意する。

## 2. 要件

### 2.1 満たすべき仕様

- 開発計画書 3.1〜3.4(層構成・モジュール・業務例外・接続/トランザクション)、4 章(スキーマ・カラム運用・マイグレーション)、5 章(業務ルール。5.9 の設定を含む)、7.2(バックアップ・復元。UI・ロック・再起動を除く)、7.4(セキュリティ)、8 章の `db/`・`core/` 観点に従う。
- 完了条件(開発計画書 9 章): 8 章の `db/`・`core/` 観点のテストが成功し、`core/`・`db/` の行カバレッジが 90% 以上。
- 次のコマンドがローカル(Windows)と CI(Windows/Linux)で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`core/`・`db/` は strict でエラー 0 件)
  - `uv run pytest --cov`
  - `uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90`
- 依存ルールのテスト(Phase 0 の規則 1〜6)が新規モジュールに対しても成功する。
- テストで実データの保存先(`%APPDATA%` 等)を使用しない。

### 2.2 決定事項

| 項目 | 決定 |
| --- | --- |
| PR 分割 | 1a(DB 基盤: 接続・スキーマ・マイグレーション・DB コピー)→ 1b(Repository・Service)→ 1c(集計・CSV・バックアップサービス・ダミーデータ)の 3 PR。各 PR は CI 成功後にマージし、次のブランチは最新の `main` から作成する |
| DB コピー基盤の配置 | 1a の `open_database()` における移行前バックアップで排他制御・WAL 含有・失敗時クリーンアップを確実に保証するため、`copy_database` を 1a の `db/backup.py` に前倒しで実装・単体テストする。1c では SQLite 整合性検査 `check_sqlite_integrity` を追加する |
| 版別スキーマ検査 | 宣言的な `SchemaSpec` を `migrations.py` に保持し、PRAGMA の結果と照合する。列(型・NOT NULL・既定値・主キー)、外部キー、インデックス、トリガーは厳密に照合する。CHECK/UNIQUE 制約・式インデックス・トリガーを含む完全な DDL を、文字列リテラルを保持しコメントを除去した正規化 SQL で版別の定義と比較する。必須断片の包含だけでは判定しない |
| カバレッジ 90% | `pytest --cov` の後に CI で `coverage report --include=<core,db> --fail-under=90` を実行する。`ui/` の計測は継続し、閾値は課さない |
| ダミーデータ生成 | `scripts/generate_dummy_data.py` が SQL で一括投入する(過去日付の `moved_at` を設定するため Service を経由しない)。生成後に 7.2 の業務整合性検査で合格を確認する |
| BackupService の範囲 | バックアップ作成、復元元の検査・一時 DB の準備、現行 DB の保全・上書き・再検査・自動復旧、一時ファイルの後始末までを Phase 1 で実装する。ファイル選択・確認・UI 操作の停止・ロック・再起動は Phase 5 |
| Service の構築 | DB を扱う Service は `conn: sqlite3.Connection` を受け取り、Repository を内部で生成する。時刻を扱う Service の `clock: Callable[[], datetime]` の既定は `timeutil.utc_now` とする。`BackupService` の接続はメソッド引数で受け取る |
| トランザクション境界 | 更新系の公開メソッドは最上位の業務操作で 1 回だけ `transaction(conn)` を開始する。読み取りメソッドは開始せず、呼び出し元のトランザクション内でも利用可能とする。複数クエリの一貫性が必要な集計・検査は最上位で `read_transaction(conn)` を使う。バックアップ・復元全体は単一トランザクションの対象外で、`backup()` は書き込みトランザクション外で実行する |
| バックアップの保存先 | 手動・移行前・復元前のいずれも保存先を排他的に新規作成し、同名ファイルがあれば中止する。失敗時に削除するのは今回作成した未完成ファイルだけとし、既存ファイルや完成した自動バックアップは削除しない |
| 品目操作の所属 | 品目の登録・編集・廃止・再有効化・一覧は `InventoryService` に置く(初期数量の履歴を伴うため)。`MasterService` はクライアント・発注主体・担当者・カテゴリ・保管場所を担当する |
| CSV 出力 | `ReportService` が出力先 `Path` を受け取り、`encoding="utf-8-sig"`・`newline=""` で書き込む。同じディレクトリの一時ファイルに書いてから `os.replace` で置換する |
| 検索・絞り込み | `ItemFilter` の全条件(部分一致・クライアント・発注主体・カテゴリ(子孫含む)・保管場所・低在庫のみ・廃止品目を含む)を Repository の SQL で処理する。UI の `QSortFilterProxyModel` はソートを主に担う |
| 業務例外の追加 | 3.3 にない次の 3 クラスを `DomainError` 派生で追加し、開発計画書 3.3 を更新する: `UnsupportedSchemaError`(版数 0・未対応の旧版・スキーマ不一致)、`MigrationError`(自動バックアップのパスを保持)、`RestoreError`(失敗段階・復旧の成否・自動バックアップのパスを保持) |
| `PRAGMA user_version` | プレースホルダを使えないため、`int` であること(`bool` 不可)と 0 以上であることを検証してから SQL に埋め込む。SQL パラメータ化規則の唯一の例外とし、コード内に 1 行コメントで明示する |
| DDL の実行 | `executescript()` を `transaction(conn)` 内で使用する(`autocommit=True` では暗黙 COMMIT が発生しない)。途中失敗時に DDL がロールバックされることをテストで確認する |
| 時刻の注入 | 時刻を扱う Service は `clock` を、`ReportService`・`BackupService` は `tz` を注入可能とする。`timeutil` の変換関数は `now`・`tz` 引数で固定できる。`open_database` は必須キーワード引数 `backup_timestamp: Callable[[], str]` を受け取り、上位層が `timeutil.local_timestamp_for_filename()` を利用したコールバックを渡す。DB 層では現在時刻取得・ローカル変換を行わず、`core.timeutil` も import しない |
| OS ローカルの期間境界 | `tz=None` では開始日・終了日のそれぞれのローカル午前 0 時を個別に UTC へ変換し、対象日ごとの OS のオフセットを適用する。現在時点の `astimezone().tzinfo` を他の日付へ使い回さない |
| 復元元の読み取り失敗 | 非 SQLite ファイル・破損 DB・ファイル不存在・アクセス拒否など入力由来の失敗は、日本語の理由を持つ `InvalidBackupError` へ変換し、元例外を `from` で保持する。プログラム側の SQL 誤りなどは一律に変換しない。スキーマ不一致の場合は、そのテーブルを前提とする業務検査へ進まない |
| dataclass | `frozen=True, slots=True`。日時は DB と同じ UTC 文字列で保持する |

### 2.3 対象外

- UI(画面・ダイアログ・Qt シグナル)、`app.py` の起動シーケンス、ログの初期化(Phase 2)
- `config.py` のパス解決(platformdirs。Phase 2)
- 多重起動防止・ロックの保持/解放・復元後の再起動(Phase 2・5)
- 性能目標の計測(Phase 2・6。Phase 1 はダミーデータ生成のみ)
- ダッシュボードの PNG 出力(Phase 6)、印刷(Phase 7)

## 3. タスク

### 1a. DB 基盤(`feature/phase1-db-foundation`)

#### A. 時刻ユーティリティ `core/timeutil.py`

- [x] 定数 `DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"` を定義する
- [x] `utc_now() -> datetime`: UTC の tz 付き現在時刻を返す。Service の既定 `clock` に使い、現在時刻の取得を集約する
- [x] `utc_now_str(now: datetime | None = None) -> str`: `now` は tz 付きのみ許可し(naive は `ValueError`)、UTC に変換して DB 形式で返す。`None` なら `utc_now()` を使う
- [x] `parse_utc(utc_str) -> datetime`: DB 形式を UTC の tz 付き `datetime` に変換する(形式不正は `ValueError`)
- [x] `to_local(utc_str, tz=None)`: tz 付きローカル `datetime` を返す。`tz=None` は OS のローカルタイムゾーン(`astimezone()`)
- [x] `format_local(utc_str, tz=None) -> str`: ローカルの `'YYYY-MM-DD HH:MM:SS'` を返す
- [x] `local_date(utc_str, tz=None) -> date`
- [x] `local_range_to_utc(start: date, end: date, tz=None) -> tuple[str, str]`: ローカル暦の `[start 00:00, end 00:00)` を UTC 文字列の組に変換する(`start >= end` は `ValueError`)
  - [x] `tz=None` は開始日・終了日から作った naive なローカル午前 0 時をそれぞれ UTC に変換する。現在時点の固定オフセットを使い回さず、各境界日の OS の DST 規則を適用する
- [x] `local_today(now=None, tz=None) -> date`: 現在年度の既定値の算出に使う
- [x] `local_timestamp_for_filename(now=None, tz=None) -> str`: `YYYYMMDD_HHMMSS`(ローカル時刻。バックアップのファイル名用)
- [x] `tests/test_timeutil.py`
  - [x] UTC ⇔ ローカルの往復(`ZoneInfo("Asia/Tokyo")`)
  - [x] 日付またぎ: UTC `2026-03-31 15:00:00` が JST `2026-04-01` になる
  - [x] サマータイム開始月・終了月の月境界(`America/New_York`、`Europe/London`)。開始・終了を含む月の区間長が 1 時間短い/長いことを確認する
  - [x] 月初・年度初の境界、`start >= end` の拒否、naive な `now` の拒否、形式不正な文字列の拒否
  - [x] `utc_now()` が UTC の tz 付き `datetime` を返し、`utc_now_str()` がその取得関数を利用する
  - [x] `tz=None` で OS ローカルの表示・期間境界が正しく変換されることを Windows/Linux で確認する
  - [x] Linux の隔離サブプロセスで `TZ` と `time.tzset()` を使い、`tz=None` でも DST 開始月・終了月の UTC 境界と区間長が正しいことを確認する。親プロセスのタイムゾーン設定は変更しない

#### B. 業務例外 `core/errors.py`

- [x] `DomainError(Exception)` を基底とし、`message` 属性(日本語)を持たせる
- [x] 3.3 の 11 クラスを定義する: `ValidationError`、`NegativeStockError`、`InactiveItemError`、`InactiveMasterError`、`AlreadyReversedError`、`ReversalNotAllowedError`、`CategoryCycleError`、`PrefixLockedError`、`MasterInUseError`、`SchemaTooNewError`、`InvalidBackupError`
- [x] 追加の 3 クラスを定義する
  - [x] `UnsupportedSchemaError`: 既存 DB の版数 0、未対応の旧版、スキーマ不一致、マイグレーション経路の欠落
  - [x] `MigrationError`: マイグレーション失敗。`backup_path: Path | None` を保持する
  - [x] `RestoreError`: 復元の失敗。`stage`(`"pre_backup"` / `"overwrite"` / `"recovery"`)、`recovered: bool`、`backup_path: Path | None` を保持する
- [x] `InvalidBackupError` は不合格理由の一覧 `reasons: tuple[str, ...]` を保持する
- [x] 開発計画書 3.3 の表に追加の 3 クラスを追記する

#### C. モデル `core/models.py`

- [x] `Reason(StrEnum)`: `IN="in"`、`OUT="out"`、`RETURN="return"`、`DISPOSE="dispose"`、`ADJUST="adjust"`
- [x] マスタ: `Client`、`Purchaser`、`Staff`(`id`、`name`、`is_active`)、`Category`(`id`、`parent_id`、`name`、`code_prefix`、`next_seq`)、`Location`
- [x] `Item`: `items` の全列
- [x] `StockMovement`: `stock_movements` の全列
- [x] 入力用: `NewItem`(登録項目と `initial_quantity`・`initial_staff_id`)、`ItemUpdate`(管理番号・数量・単位以外の編集可能項目)
- [x] `PurchaseInfo`: `last_purchased_at: str | None`(UTC)、`lot_quantity: int | None`
- [x] 表示用: `ItemRow`(`Item` にクライアント名・発注主体名、カテゴリのフルパス、保管場所名、`is_low_stock`、`PurchaseInfo` を加える)、`MovementRow`(`StockMovement` に管理番号・品名・クライアント名・発注主体名・担当者名・`is_reversed` を加える)
- [x] `ItemFilter`: `text`、`client_id`、`purchaser_id`、`category_id`(子孫を含む)、`location_id`、`low_stock_only`、`include_inactive`(すべて任意。既定は絞り込みなし・廃止を除外)
- [x] `PeriodKind(StrEnum)`: `ANNUAL`、`MONTHLY`
- [x] `GroupBy(StrEnum)`: `NONE`、`CLIENT`、`PURCHASER`、`CLIENT_PURCHASER`(ダッシュボードの内訳軸)
- [x] `DashboardRow`: 期間ラベル・期間開始日(ローカル)、`client_id`・`client_name`、`purchaser_id`・`purchaser_name`(内訳軸で畳み込んだ項目は `None`)、入庫数、出庫数、支出額、単価未登録の出庫件数、廃棄数、廃棄額

#### D. 接続 `db/connection.py`

- [x] `connect(path: Path) -> sqlite3.Connection`: `autocommit=True` で開き、`foreign_keys`・`journal_mode = WAL`・`busy_timeout = 5000` を設定する。`journal_mode` の戻り値が `wal` でなければ接続を閉じて例外とする
- [x] `connect_readonly(path: Path)`: `path.resolve().as_uri() + "?mode=ro"` と `uri=True` で開き、`foreign_keys`・`busy_timeout` のみを設定する(ジャーナルモードは変更しない)。ファイルが存在しない場合は接続前に `FileNotFoundError`
- [x] `connect_memory()`: `":memory:"`、`foreign_keys`・`busy_timeout` を設定する
- [x] `transaction(conn)`(`contextmanager`)
  - [x] 開始時に `conn.in_transaction` なら `RuntimeError`(ネスト禁止)
  - [x] `BEGIN IMMEDIATE` → 正常終了で `COMMIT`
  - [x] 本体で例外が起きたら、トランザクションが残っていれば `ROLLBACK` して再送出する
  - [x] `COMMIT` に失敗したら、トランザクションが残っていれば `ROLLBACK` して再送出する
- [x] `read_transaction(conn)`(`contextmanager`): 複数クエリを同一スナップショットで読む集計・検査用
  - [x] 既存トランザクションがあれば参加するだけとし、開始・COMMIT・ROLLBACK は呼び出し元に任せる
  - [x] 既存トランザクションがなければ `BEGIN` → 正常終了で `COMMIT`、例外・COMMIT 失敗時は残っていれば `ROLLBACK`。`BEGIN IMMEDIATE` は使わない
- [x] `tests/test_connection.py`
  - [x] 通常接続で `foreign_keys = 1`・`journal_mode = wal`・`busy_timeout = 5000`(`tmp_path`)
  - [x] 読み取り専用接続で書き込みが失敗し、DELETE モードの DB のジャーナルモードが変わらない
  - [x] 日本語や空白を含むパスでも読み取り専用接続できる
  - [x] メモリ DB で `foreign_keys = 1`
  - [x] `transaction()` の成功時 COMMIT、例外時 ROLLBACK、ネスト時の `RuntimeError`
  - [x] COMMIT 失敗時の ROLLBACK(`execute` で `COMMIT` を失敗させる `sqlite3.Connection` サブクラスを `factory=` で作る)
  - [x] `read_transaction()` の成功・例外時・COMMIT 失敗時の終了処理、既存トランザクションへの参加時に呼び出し元の境界を変更しないこと、読み取り専用接続での使用を確認する

#### E. DB バックアップ基盤 `db/backup.py`

- [x] `copy_database(src_conn, dest_path)`: 保存先を排他的に新規作成してから接続し、`src_conn.backup(dest)` を実行して必ず閉じる。既存ファイルは `FileExistsError` とし、上書きしない。WAL 内の確定データも含まれる
  - [x] コピー失敗時は自身が今回作成した未完成 DB と付随ファイルだけを接続終了後に削除する。既存ファイルは変更・削除しない
  - [x] 呼び出し時に `src_conn.in_transaction` ならコピーを開始せず `RuntimeError` とする。現行 DB への上書き・復旧にはこの新規作成専用関数を使わず、書き込みトランザクション外で `backup()` を呼ぶ
  - [x] Service・UI を import しない(依存ルールのテストで担保する)
- [x] `tests/test_backup.py`(`tmp_path` 上の実ファイル DB。1a では `copy_database` の単体検証)
  - [x] バックアップに WAL 内の未チェックポイントの確定データが含まれる
  - [x] 保存先ファイルの排他的新規作成、既存同名ファイルの拒否(`FileExistsError`)、書き込み不可ディレクトリでのエラー
  - [x] `copy_database()` が書き込みトランザクション外で呼ばれ、トランザクション中のコピー要求を開始前に拒否する(`RuntimeError`)
  - [x] 保存先の確保・コピー失敗時に自身が作成した未完成ファイルのみ削除し、既存ファイルを変更・削除しない

#### F. スキーマ `src/inventory_manager_mini/db/schema.sql`

- [x] 開発計画書 4.2 の DDL を記述する(`settings` の初期行を含む)
- [x] `importlib.resources.files("inventory_manager_mini.db") / "schema.sql"` で読み込み、wheel に含まれることを確認する
- [x] `tests/test_schema_constraints.py`(`:memory:`)
  - [x] CHECK: `is_active`、`code_prefix`(長さ・使用文字)、`unit = '個'`、`quantity >= 0`、`reorder_threshold >= 0`、`reorder_quantity > 0`、`reference_price >= 0`、`unit_price >= 0`、`reason` の値域、`reason` と `delta` の符号(取り消し行は除外)
  - [x] UNIQUE: クライアント名・発注主体名・担当者名・保管場所名・接頭辞・管理番号・`reversal_of`、同一親の下でのカテゴリ名(親が NULL 同士も重複として拒否)
  - [x] FOREIGN KEY: 存在しないクライアント・発注主体・カテゴリ・品目・元行の参照を拒否する
  - [x] NOT NULL: `items.client_id`・`items.purchaser_id`、`stock_movements.client_id`・`stock_movements.purchaser_id` の NULL を拒否する
  - [x] `trg_items_updated_at`: 更新時に `updated_at` が変わる(明示的に指定した場合はその値を維持する)

#### G. マイグレーション `db/migrations.py`

- [x] 定数: `SCHEMA_VERSION = 1`、`MIN_SUPPORTED_SCHEMA_VERSION = 1`、`MIGRATIONS: dict[int, str] = {}`(キーは「その版へ上げる SQL」)
- [x] 検査定義の dataclass: `ColumnSpec`(名前・型・NOT NULL・既定値・主キー順位)、`ForeignKeySpec`、`IndexSpec`(名前・テーブル・UNIQUE・列/式・完全な CREATE INDEX 文)、`TriggerSpec`(名前・テーブル・完全な CREATE TRIGGER 文)、`TableSpec`(列・外部キー・CHECK/UNIQUE を含む完全な CREATE TABLE 文)、`SchemaSpec`
- [x] `SCHEMA_SPECS: dict[int, SchemaSpec]` に版 1 の定義を書く
- [x] `normalize_sql(sql) -> str`: SQLite の引用・エスケープ規則に沿ってトークン化し、文字列リテラル・引用識別子を保持する。コメントを除去し、それ以外のトークンの大小文字・空白を正規化する。単純な SQL 全体の小文字化や正規表現だけのコメント除去は行わない
  - [x] アプリ自身が生成する版別 DDL の表記差に対応する範囲とし、任意の意味的に同等な DDL の受け入れは目的としない
- [x] `inspect_schema(conn, version, specs=SCHEMA_SPECS) -> list[str]`: 不一致を日本語の理由一覧で返す(空なら合格)
  - [x] テーブルの集合が一致する(`sqlite_` で始まる内部テーブルを除く)
  - [x] 列の集合と各属性が一致する(`PRAGMA table_xinfo`)
  - [x] 外部キーが一致する(`PRAGMA foreign_key_list`)
  - [x] インデックス・トリガーの集合と定義が一致する(自動インデックスを除く。`PRAGMA index_list`・`index_xinfo`、`sqlite_master`)
  - [x] テーブル・明示インデックス・トリガーの完全な `sqlite_master.sql` を版別の DDL と正規化後に比較し、CHECK/UNIQUE・式・トリガー本体の改変を拒否する。コメント内に残った断片や弱められた制約を存在の証明としない
- [x] `check_migration_path(from_version, to_version, migrations) -> None`: 途中の版の SQL が欠けていれば `UnsupportedSchemaError`
- [x] `create_schema(conn, ...)`: 1 トランザクションで DDL 適用 → 検査 → `PRAGMA user_version` 設定を行う
- [x] `open_database(path, backup_dir, *, backup_timestamp: Callable[[], str], schema_version=..., min_supported=..., migrations=..., specs=...) -> sqlite3.Connection`
  - [x] 上位層が `timeutil.local_timestamp_for_filename()` を利用した日時コールバックを渡す。戻り値は `YYYYMMDD_HHMMSS`。DB 層では現在時刻取得・タイムゾーン変換・`core.timeutil` の import を行わない
  - [x] 接続前に `path.exists()` を確認する
  - [x] ファイルがなければ新規作成する。失敗時は接続を閉じ、作成した DB と `-wal`・`-shm` を削除して例外を再送出する
  - [x] 既存 DB で版数 > `schema_version` なら `SchemaTooNewError`
  - [x] 版数 0、または版数 < `min_supported` なら `UnsupportedSchemaError`
  - [x] その版の検査定義で検査し、不一致なら `UnsupportedSchemaError`
  - [x] 旧版の場合は経路を検査し、`backup_dir/pre-migrate_v{N}_{ローカル日時}.db` を `copy_database()` で作成してから、版ごとに 1 トランザクションで「SQL 適用 → その版の検査 → `user_version` 更新」を行う。失敗時はロールバックし、`MigrationError(backup_path=...)` を送出する
  - [x] 移行前バックアップは書き込みトランザクション外で `copy_database()` により保存先を排他的に新規作成する。同名・確保・コピーの失敗は `MigrationError(backup_path=None)` とし、移行を開始しない。今回作成した未完成 DB と付随ファイルだけを後始末し、既存ファイル・完成したバックアップは保持する
  - [x] 失敗時は開いた接続を必ず閉じる
- [x] `tests/test_migrations.py`(`tmp_path` 上の実ファイル DB)
  - [x] 新規作成: `user_version = 1`、検査に合格、再接続後も永続化されている、WAL で動作する
  - [x] 新規作成の途中失敗(壊れた DDL を注入)で、DDL・`user_version` が残らずファイルも削除される
  - [x] 版数 0 の既存ファイル(空ファイルを含む)を `UnsupportedSchemaError` で拒否し、新規作成と区別する
  - [x] 新版(`user_version = 2`)を `SchemaTooNewError` で拒否する
  - [x] 同名テーブルで列・CHECK・UNIQUE・FK・インデックス・トリガーのいずれかが欠けた DB を拒否する(項目ごとにパラメータ化する)
  - [x] 接頭辞の GLOB パターンを `'*[^A-Z0-9]*'` から `'*[^a-z0-9]*'` へ変更した DB、必須制約をコメント内にだけ残した DB、`OR 1` で制約を弱めた DB を拒否する
  - [x] SQL 正規化がリテラル内の大小文字・空白・コメント記号・エスケープを保持し、リテラル外のコメント・表記差だけを正規化する
  - [x] 試験用の v2(列追加の SQL・v2 の検査定義)を注入し、v1 → v2 の移行が成功し、`pre-migrate_v1_*.db` が作成される
  - [x] 注入した日時がファイル名に使われる。固定日時と既存の同名バックアップで `MigrationError` となり、既存バックアップのハッシュ・現行 DB のデータ・`user_version` が変わらない
  - [x] 試験用の v2 で SQL を途中失敗させ、DDL・データ・`user_version` がロールバックされ、`MigrationError.backup_path` のバックアップが残る
  - [x] 適用後の検査が不合格の場合もロールバックされる
  - [x] 未対応の旧版(`min_supported = 2` を注入)と経路の欠落を拒否する
  - [x] 依存ルール R2・R6 が成功し、DB 層に日時処理のための禁止 import・現在時刻取得がない

#### H. CI・文書(1a の最後)

- [ ] `.github/workflows/ci.yml` の `pytest --cov` の後に、カバレッジ閾値のステップを追加する
- [ ] README のコマンド一覧にカバレッジ閾値のコマンドを追記する
- [ ] ローカルで全コマンドが成功する
- [ ] PR を作成し、Windows/Linux の CI 成功後にマージする

### 1b. Repository・Service(`feature/phase1-services`。1a に依存)

#### I. Repository `db/repositories.py`

- [ ] 共通: すべての SQL をプレースホルダでパラメータ化する。行を dataclass に変換する関数を持つ
- [ ] `IntegrityError` の変換: `sqlite_errorname`(`SQLITE_CONSTRAINT_UNIQUE`・`_CHECK`・`_FOREIGNKEY`・`_NOTNULL`)とメッセージ中の対象列で判定し、呼び出し箇所ごとの対応表で業務例外に変換する。対応外の制約違反は `ValidationError` とし、元の例外を `from` で連結する
  - [ ] マスタ名・接頭辞の重複 → `ValidationError`
  - [ ] `reversal_of` の重複 → `AlreadyReversedError`
  - [ ] `quantity >= 0` 違反 → `NegativeStockError`
  - [ ] 削除時の外部キー違反 → `MasterInUseError`
- [ ] `ItemRepository`
  - [ ] `allocate_code(category_id) -> str`: `UPDATE categories SET next_seq = next_seq + 1 WHERE id = ? RETURNING code_prefix, next_seq - 1` で連番を確保し、`接頭辞-%04d` を生成する
  - [ ] `insert`、`update`(数量・単位・管理番号は対象外)、`get`、`set_active`
  - [ ] `add_quantity(item_id, delta)`: `UPDATE items SET quantity = quantity + ? WHERE id = ?`
  - [ ] `list(filter: ItemFilter) -> list[ItemRow]`: 品名・管理番号・メーカー型番を `LIKE ? ESCAPE '\'` で部分一致させる(`%`・`_`・`\` をエスケープ)。カテゴリは再帰 CTE で子孫を含める。クライアント名・発注主体名・カテゴリのフルパス(` > ` 区切り)・保管場所名・低在庫・最終購入情報を 1 クエリで取得する
  - [ ] `list_low_stock() -> list[ItemRow]`(有効品目で `quantity <= reorder_threshold`)
  - [ ] `get_purchase_info(item_id) -> PurchaseInfo`(開発計画書 5.6 の SQL)
  - [ ] 業務整合性検査用(1b 単体テストおよび 1c BackupService で使用): `find_quantity_mismatches()`(全品目で `quantity` と `COALESCE(SUM(delta), 0)` が不一致の行)
- [ ] `MovementRepository`
  - [ ] `insert`、`get`、`is_reversed(movement_id)`
  - [ ] `list_by_item(item_id) -> list[MovementRow]`、`list_all() -> list[MovementRow]`(日時・ID の昇順)
  - [ ] 業務整合性検査用(1b 単体テストおよび 1c BackupService で使用): `find_invalid_reversals()`(元行の不在、元行が取り消し行、元行が差分 0 の棚卸、`item_id` の不一致、`delta` が符号反転でない、`reason`・`client_id`・`purchaser_id`・`unit_price`・`used_for` の不一致(`IS NOT` で NULL 同士を一致扱い))
- [ ] `MasterRepository`
  - [ ] クライアント・発注主体・担当者: 一覧(無効化を含むか指定)・取得・追加・名称変更・有効/無効の切替・削除・使用中の判定(クライアント・発注主体は `items` と `stock_movements` の参照、担当者は `stock_movements` の参照)
  - [ ] カテゴリ: 一覧・取得・追加・名称変更・親変更・接頭辞変更・削除・子孫 ID の取得(再帰 CTE)・フルパスの取得・使用中の判定(品目参照・子カテゴリ・`next_seq > 1`)
  - [ ] 保管場所: 一覧・取得・追加・名称変更・削除・使用中の判定
- [ ] `SettingsRepository`: `get(key)`、`set(key, value)`、`all() -> dict[str, str]`

#### J. Service `core/services.py`

- [ ] 共通の補助
  - [ ] `validate_purchase_url(url: str | None) -> str | None`: 前後の空白を除去し、空なら `None`。`urllib.parse.urlsplit` でスキームが `http`/`https` かつホスト名ありの場合のみ許可し、それ以外は `ValidationError`(UI が `openUrl` の前に再利用する)
  - [ ] 任意文字列の正規化(前後の空白を除去し、空文字は `None`)と必須文字列の検証
  - [ ] 整数の検証(`bool` を拒否し、下限を確認する)
  - [ ] 接頭辞の検証(`^[A-Z0-9]{2,5}$`)
- [ ] 更新操作の業務検証は `transaction(conn)` の内側(`BEGIN IMMEDIATE` 後)で最新の行を読んで行う。更新系公開メソッド同士を呼んでトランザクションを二重に開始しない
- [ ] 読み取り専用の取得・一覧・検査メソッドはトランザクションを開始せず、既存トランザクション内でも利用可能とする。複数クエリの一貫性が必要な最上位の集計・検査だけ `read_transaction(conn)` を使う
- [ ] `InventoryService(conn, clock=...)`
  - [ ] `create_item(new: NewItem) -> Item`: クライアント・発注主体が有効(発注主体の未指定は `ValidationError`、無効は `InactiveMasterError`)、品名・カテゴリが必須、単位は「個」を固定で設定、URL を検証する。採番と INSERT を同一トランザクションで行う。初期数量が 1 以上なら有効な担当者を必須とし、`reason='adjust'`・`delta=初期数量`・`unit_price=参考価格` の履歴を記録する。0 なら担当者不要で履歴は作らない
  - [ ] `update_item(update: ItemUpdate) -> Item`: 管理番号・数量・単位は変更しない。クライアント・発注主体の変更先は有効なもののみ(発注主体を空にはできない)。無効化済みの現在の値を維持する編集は許可する。廃止品目の編集も許可する
  - [ ] `deactivate_item`・`reactivate_item`(在庫残があっても廃止可能)
  - [ ] `get_item`、`list_items(filter)`、`list_low_stock()`、`get_purchase_info(item_id)`
  - [ ] 在庫操作 5 種: `receive(item_id, staff_id, quantity, unit_price, update_reference_price, note)`、`issue(item_id, staff_id, quantity, used_for, note)`、`return_to_supplier(...)`、`dispose(...)`、`stocktake(item_id, staff_id, actual_quantity, note)`
    - [ ] 共通: 品目が有効(違反は `InactiveItemError`)、担当者が有効(違反は `InactiveMasterError`)、数量は 1 以上(棚卸の実数は 0 以上)
    - [ ] 出庫は使用先が必須
    - [ ] 減少後の在庫が負なら `NegativeStockError`
    - [ ] `client_id`・`purchaser_id` は品目の現在の値をコピーする(無効化済みでも拒否しない)
    - [ ] `unit_price`: 入庫は入力値(`None` の場合は参考価格)、それ以外は操作時点の参考価格
    - [ ] 入庫で `update_reference_price=True` かつ実単価が参考価格と異なる場合は、同一トランザクションで `reference_price` を更新する
    - [ ] 棚卸は差分 0 でも記録する
    - [ ] 処理順: 検証 → 履歴 INSERT → 数量 UPDATE。`moved_at = utc_now_str(clock())`
  - [ ] `reverse(movement_id, staff_id, note) -> StockMovement`
    - [ ] 取り消し操作者は有効な担当者が必須
    - [ ] 元行が取り消し行 → `ReversalNotAllowedError`、取り消し済み → `AlreadyReversedError`、差分 0 の棚卸 → `ReversalNotAllowedError`、品目が廃止 → `InactiveItemError`、取り消し後の在庫が負 → `NegativeStockError`
    - [ ] `reason`・`client_id`・`purchaser_id`・`unit_price`・`used_for` は元行をコピーし、`delta = -元delta`、`reversal_of = 元行 ID` とする。参考価格は戻さない
  - [ ] `list_history(item_id)`、`list_all_history()`
  - [ ] `reversal_block_reason(movement_id) -> str | None`: 取り消し不可の理由(UI のツールチップ用。担当者の条件を除く)
- [ ] `MasterService(conn)`
  - [ ] クライアント・発注主体・担当者: 追加・名称変更・無効化・再有効化・削除(使用中なら `MasterInUseError`。無効化のみ可)
  - [ ] カテゴリ: 追加(親・名称・接頭辞)・名称変更・親変更(自身または子孫なら `CategoryCycleError`)・接頭辞変更(`next_seq > 1` なら `PrefixLockedError`)・削除(品目参照・子カテゴリ・`next_seq > 1` のいずれかで `MasterInUseError`)
  - [ ] 保管場所: 追加・名称変更・削除(使用中なら `MasterInUseError`)
  - [ ] 一覧(無効化を含むか指定)、使用中・削除可否の判定(UI の活性制御用)
- [ ] `SettingsService(conn)`
  - [ ] `get_fiscal_year_start_month() -> int`: 読み取り専用とし、トランザクションを開始しない
  - [ ] `set_fiscal_year_start_month(month)`: `transaction(conn)` 内で検証・更新する(1〜12 以外は `ValidationError`)
  - [ ] `validate_all() -> list[str]`: 必須キーの存在、値が正規の整数表記で 1〜12、未知のキーがないことを検査し、不合格の理由一覧を返す。読み取り専用とし、トランザクションを開始しない

#### K. テスト(1b)

- [ ] `tests/conftest.py` に共通フィクスチャを置く
  - [ ] `memory_conn`: `connect_memory()` と `create_schema()` で作成する
  - [ ] `seeded_conn`: 有効なクライアント 2・無効なクライアント 1、有効な発注主体 2・無効な発注主体 1、有効な担当者 2・無効な担当者 1、親子のカテゴリ(接頭辞付き)、保管場所 2 を投入する
  - [ ] `fixed_clock`: 時刻を任意に進められる `clock`
  - [ ] Service 群(`inventory`・`master`・`settings`)
- [ ] `tests/test_repositories.py`: 採番の連番と 10000 以降の 5 桁化、`ItemFilter` の各条件と組み合わせ(LIKE の特殊文字を含む)、カテゴリのフルパス、最終購入情報(取り消し済みの入庫を除外)、`IntegrityError` の変換、業務整合性検査用クエリの単体検証(`find_quantity_mismatches()` の一致・不一致・履歴なし、`find_invalid_reversals()` の正常行・元行不在・取り消し行の取り消し・差分 0 棚卸・品目不一致・delta 符号不正・属性不一致の各パターン)
- [ ] `tests/test_inventory_service.py`
  - [ ] 品目の登録: 単位の固定、初期数量の adjust と担当者の記録、初期数量が正で担当者が未指定/無効の場合に品目・履歴・連番がいずれも変わらない
  - [ ] 採番: 品目登録 → 別カテゴリへ変更 → 元カテゴリの削除を拒否 → 元カテゴリで追加登録し、連番が継続する
  - [ ] 品目の編集: 数量・単位・管理番号が変わらない、無効化済みクライアント・発注主体の維持は可、無効なクライアント・発注主体への変更は拒否、発注主体の未指定・登録時の無効な発注主体を拒否、過去の履歴の `client_id`・`purchaser_id` は不変
  - [ ] URL の検証: `http`/`https` かつホストありのみ許可(`javascript:`・`file:`・`ftp:`・ホストなしを拒否)
  - [ ] 在庫操作 5 種の `reason`・`delta`・`unit_price`・`used_for` と数量の更新、負の在庫の拒否、廃止品目・無効な担当者の拒否、無効化済みクライアント・発注主体の品目への通常操作の許可
  - [ ] 入庫で参考価格を更新した後にその入庫を取り消しても参考価格が戻らず、取り消し行の単価が元行と一致する。入庫後に参考価格を再変更した場合も最新値が維持される
  - [ ] 取り消しの全不可条件、初期数量の adjust の取り消し、元行のクライアント・発注主体・担当者が無効化済みでも取り消せる、クライアント・発注主体変更後の取り消しで元行の `client_id`・`purchaser_id` がコピーされる
  - [ ] トランザクション: 成功時の永続化。履歴 INSERT 後・数量 UPDATE 前の失敗(`add_quantity` を monkeypatch で例外にする)で、履歴と数量の両方が元に戻る。COMMIT 失敗(`factory=` で作った Connection サブクラス)で両方が元に戻る
- [ ] `tests/test_master_service.py`: 5.4 の表の全パターン(未使用は物理削除、使用中は無効化のみ/削除不可)、循環参照の拒否(自身・子・孫)、接頭辞の形式とロック、同一親での名称重複の拒否、名称変更
- [ ] `tests/test_settings_service.py`: 取得・設定・範囲外の拒否、`validate_all()` の各不合格パターン
  - [ ] 取得・検査メソッドが既存トランザクション内でも成功し、呼び出し元のトランザクションを終了しない
- [ ] ローカルで全コマンドが成功し、PR の CI 成功後にマージする

### 1c. 集計・CSV・バックアップ・ダミーデータ(`feature/phase1-reports-backup`。1b に依存)

#### L. 集計・CSV `core/reports.py`

- [ ] `ReportService(conn, tz=None, clock=...)`
- [ ] `fiscal_year_of(local_date, start_month) -> int`(`年 − (月 < 開始月 ? 1 : 0)`)、`current_fiscal_year()`、`available_fiscal_years()`(履歴の最古・最新から算出し、現在の年度を含める)
- [ ] `dashboard(fiscal_year, kind, client_id=None, purchaser_id=None, group_by=GroupBy.NONE) -> list[DashboardRow]`
  - [ ] 最上位で `read_transaction(conn)` を使い、年度開始月と全期間の集計を同一スナップショットで読む。内部の読み取り Service は新しいトランザクションを開始しない
  - [ ] 年度開始月は `SettingsService` から取得する
  - [ ] 期間境界は `local_range_to_utc()` で算出する(月次は 12 区間、年次は 1 区間)
  - [ ] `stock_movements m LEFT JOIN stock_movements o ON m.reversal_of = o.id` で `COALESCE(o.moved_at, m.moved_at) >= ? AND < ?` を条件とし、`(:client_id IS NULL OR m.client_id = :client_id)` と `(:purchaser_id IS NULL OR m.purchaser_id = :purchaser_id)` で絞り込んで、期間・`client_id`・`purchaser_id` ごとに 5.7 の指標を集計する(SQL は 1 本。列名を文字列で組み立てない)
  - [ ] `group_by` に応じて、全指標が加算可能であることを利用して Python 側で畳み込む(`NONE` はクライアントも発注主体も `None`、`CLIENT` は発注主体を `None`、`PURCHASER` はクライアントを `None`)。`NONE` は履歴がなくても期間数分の行を 0 値で返し、それ以外は履歴のある組のみ返す。無効化済みのマスタも履歴があれば含める
  - [ ] 単価未登録の出庫件数は、有効な操作(取り消し行でも取り消された行でもない)のみを数える
  - [ ] 返品・棚卸(初期数量を含む)はいずれの指標にも含めない
- [ ] `escape_csv_cell(value: str) -> str`: `=` `+` `-` `@` タブ・CR で始まる文字列の先頭に `'` を付与する
- [ ] CSV の共通処理: `csv.writer(quoting=csv.QUOTE_ALL, lineterminator="\r\n")`、UTF-8(BOM 付き)。同じディレクトリの一時ファイルに書いてから `os.replace` で置換し、失敗時は一時ファイルを削除する。文字列列のみエスケープし、数値列はそのまま、`None` は空文字とする
- [ ] `export_items_csv(path, filter)`: 5.8 の列。最終購入日はローカル日付、低在庫は `○`/空、状態は `有効`/`廃止`
- [ ] `export_history_csv(path, item_id=None)`: 5.8 の列。日時は `format_local`、操作種別は `入庫`/`出庫`/`返品`/`廃棄`/`棚卸`、取り消し済みは `○`/空
- [ ] `export_dashboard_csv(path, fiscal_year, kind, client_id=None, purchaser_id=None, group_by=GroupBy.NONE)`: 5.8 の列(畳み込んだ軸の列は「(すべて)」)
- [ ] `tests/test_reports.py`(`tz` に `ZoneInfo` を注入し、`clock` で `moved_at` を固定する)
  - [ ] `dashboard()` が実際の `SettingsService.get_fiscal_year_start_month()` を呼び、ネストエラーなく正常終了する
  - [ ] 月またぎ・年度またぎの入庫がローカル暦の正しい区間に集計される(UTC 15:00 = JST 翌日 0:00 の境界を含む)
  - [ ] 月・年度をまたぐ取り消しが元行の期間で相殺される(タイムゾーン境界付近を含む)
  - [ ] サマータイムの切替月の境界(`America/New_York`)
  - [ ] 年度開始月の変更で区切りが変わる
  - [ ] クライアント変更・発注主体変更の後も、過去の費用が元のクライアント・発注主体に帰属する
  - [ ] 内訳軸 `NONE`・`CLIENT`・`PURCHASER`・`CLIENT_PURCHASER` の各合計が一致する
  - [ ] クライアント・発注主体の 2 軸同時絞り込みが `CLIENT_PURCHASER` 内訳の該当行と一致する。無効化済みのマスタでも絞り込める
  - [ ] 絞り込み未指定(`None`)は全件を対象とし、ID を指定したときのみ絞り込まれる
  - [ ] 支出額・廃棄額は単価未登録を除外し、単価未登録の出庫件数は有効な操作のみ数える。返品・棚卸を含めない
  - [ ] 最終購入日がローカル日付になる
- [ ] `tests/test_csv.py`: BOM・CRLF・全項目のクォート、数式インジェクション対策(各先頭文字、数値列は対象外)、日時のローカル表記(オフセットなし)、列の順序、書き込み失敗時に既存ファイルが壊れず一時ファイルが残らない

#### M. DB 整合性検査 `db/backup.py`

- [ ] 1a で作成した `db/backup.py` に `check_sqlite_integrity(conn) -> list[str]` を追加する: `PRAGMA integrity_check` が `ok`、`PRAGMA foreign_key_check` が結果なしであることを検査し、不合格の理由を返す
- [ ] 引き続き Service・UI を import しない(依存ルールのテストで担保する)

#### N. BackupService `core/services.py`

- [ ] `BackupService(clock=..., tz=None)`(DB のパス・接続は引数で受け取る)
- [ ] `create_backup(conn, dest_dir) -> Path`: `inventory_{ローカル日時}.db` を作成する。保存先がなければ作成し、`copy_database` の排他的な新規作成で同名を拒否する。同名の `FileExistsError` は `ValidationError` に変換する
- [ ] `inspect_database(conn, version) -> list[str]`: `read_transaction(conn)` 内で版別スキーマ・SQLite 整合性を検査し、合格した場合だけ業務整合性(`find_quantity_mismatches`・`find_invalid_reversals`)・設定値(`SettingsService(conn).validate_all()`)を検査する。内部の読み取りメソッドはトランザクションを開始しない。不合格の理由一覧を返す
- [ ] `prepare_restore(source_path) -> PreparedRestore`
  - [ ] `connect_readonly` で開き、版数(`MIN_SUPPORTED_SCHEMA_VERSION <= v <= SCHEMA_VERSION`)、その版のスキーマ、SQLite 整合性を検査する。不合格なら `InvalidBackupError(reasons)`
  - [ ] 復元元のオープン・版数取得・PRAGMA 実行で、内容不正による `SQLITE_NOTADB`・`SQLITE_CORRUPT`(拡張コードを含む)、ファイル不存在・アクセス拒否など入力由来の失敗が起きた場合も、日本語の理由を持つ `InvalidBackupError` に変換し、元例外を `from` で保持する
  - [ ] 入力由来かどうかは失敗した処理と SQLite のエラーコードで判定する。プログラム側の SQL 誤り・引数誤りなどを含む `sqlite3.DatabaseError` 全般を一律に `InvalidBackupError` へ変換しない
  - [ ] `tempfile.TemporaryDirectory` に `copy_database` で一時 DB を作り、復元元の接続を閉じる
  - [ ] 一時 DB が旧版なら版ごとのトランザクションで最新版へ移行する(失敗は `InvalidBackupError`)
  - [ ] 一時 DB を `inspect_database(conn, SCHEMA_VERSION)` で再検査する(不合格は `InvalidBackupError`)
  - [ ] `PreparedRestore` は一時 DB のパスと確認ダイアログ用の要約(元の版数、品目数、履歴件数、最終の操作日時)を持ち、コンテキストマネージャとして後始末(接続を閉じ、一時ディレクトリを削除)を保証する
  - [ ] 失敗時も一時ファイルを後始末する。復元元のファイルは変更・削除しない
- [ ] `apply_restore(prepared, current_db_path, backup_dir) -> Path`(呼び出し側が現行 DB の全接続を閉じた後に呼ぶ。戻り値は pre-restore のバックアップ)
  - [ ] 現行 DB を `backup_dir/pre-restore_{ローカル日時}.db` へ `copy_database` で排他的に新規作成して保全する。同名・確保・コピーの失敗時は現行 DB を変更せず `RestoreError(stage="pre_backup", recovered=True, backup_path=None)`。今回作成した未完成ファイルだけを後始末する
  - [ ] 一時 DB から現行 DB へ `backup()` で上書きし、`inspect_database` で再検査する
  - [ ] 上書きまたは再検査に失敗したら、pre-restore のバックアップから `backup()` で復旧し、同じ検査を行う。合格なら `RestoreError(stage="overwrite", recovered=True, backup_path=...)`、不合格・失敗なら `RestoreError(stage="recovery", recovered=False, backup_path=...)`。自動バックアップは削除しない
  - [ ] 成功・失敗のいずれでも、自身が開いた接続をすべて閉じる
- [ ] `cancel(prepared)`: 後始末のみ行う
- [ ] `tests/test_backup.py`(`tmp_path` 上の実ファイル DB。1c で `check_sqlite_integrity` と `BackupService` の総合テストを拡充)
  - [ ] バックアップに WAL 内の未チェックポイントの確定データが含まれる。ファイル名がローカル日時になる。保存先の作成、同名の拒否、書き込みできない保存先でのエラー
  - [ ] `copy_database()` が書き込みトランザクション外で呼ばれ、トランザクション中のコピー要求を開始前に拒否する。保存先の確保・コピー失敗時に既存ファイルを変更・削除しない
  - [ ] `inspect_database()` が実際の `SettingsService.validate_all()` を呼び、ネストエラーなく正常終了する。スキーマ不一致時は業務・設定クエリへ進まない
  - [ ] 正常な復元(DELETE モードの復元元を含む)で、現行 DB が置き換わり、pre-restore のバックアップが作成される
  - [ ] 検査・成功・キャンセル・失敗のいずれでも、復元元のファイルのハッシュとジャーナルモードが変わらず、一時ファイルが残らない
  - [ ] 拒否: 版数 0・未対応の旧版・新版、同名テーブルで列/制約が欠けた DB、`integrity_check`/`foreign_key_check` の不合格、数量と履歴合計の不一致、取り消し行の品目・符号・コピー属性の不正、取り消し元が取り消し行・差分 0 の棚卸、設定値の不正。いずれも現行 DB と復元元が変化しない
  - [ ] 非 SQLite ファイル・物理破損 DB・不存在の復元元・アクセス拒否を `InvalidBackupError` で拒否し、日本語の理由と元例外を保持する。現行 DB は変わらず、存在する復元元のハッシュも変わらない。失敗後に開いた接続が閉じられ、一時ファイルが残らない
  - [ ] プログラム側の SQL 誤りは `InvalidBackupError` に変換されず、接続・一時ファイルの後始末は同様に保証される
  - [ ] 許容: 正常な初期数量・棚卸・取り消し、履歴なしの数量 0、コピー属性の NULL 同士、クライアント・発注主体・担当者の無効化、品目の廃止、クライアント・発注主体・参考価格の変更
  - [ ] 試験用の v2 を注入した更新経路: v1 の復元元が一時 DB で v2 に移行されて復元される。移行失敗時は `InvalidBackupError` で、現行 DB と復元元が変化しない
  - [ ] pre-restore のバックアップの失敗(保存先を書き込み不可にする)で、現行 DB が変化しない
  - [ ] 固定時計と既存の同名 pre-restore バックアップで `RestoreError(stage="pre_backup", recovered=True)` となり、既存バックアップのハッシュ・現行 DB のデータ・`user_version` が変わらない
  - [ ] 上書き後の再検査の失敗(検査を monkeypatch で不合格にする)で復旧され、`recovered=True` になる
  - [ ] 復旧の失敗で `recovered=False` となり、自動バックアップが残る

#### O. ダミーデータ生成 `scripts/generate_dummy_data.py`

- [ ] 引数: `--db`(必須)、`--items`(既定 5000)、`--movements`(既定 100000)、`--years`(既定 3)、`--seed`(既定 0)、`--force`(既存ファイルの上書きを許可)
- [ ] 既存ファイルは `--force` がなければ拒否する。上書き時は DB と `-wal`・`-shm` を削除してから作成する
- [ ] `open_database()` に `timeutil.local_timestamp_for_filename()` を利用する `backup_timestamp` コールバックを渡して新規作成し、1 トランザクションで投入する
  - [ ] マスタ(クライアント・発注主体・担当者(一部は無効化)、2 階層のカテゴリ、保管場所)
  - [ ] 品目(一部は廃止・参考価格なし・URL あり)。管理番号は採番と同じ規則にし、`next_seq` を整合させる
  - [ ] 履歴: 過去 `--years` 年に時系列で分布させ、在庫が負にならないように生成する。全 `reason`、取り消し行、単価未登録を含める
  - [ ] `items.quantity` を履歴の合計に一致させる
- [ ] 終了時に `BackupService.inspect_database()` で合格を確認し、件数と所要時間を表示する
- [ ] `pyproject.toml` の pyright の `include` に `scripts` を追加する
- [ ] `tests/test_dummy_data.py`: 小規模(品目 50・履歴 500)で生成し、検査に合格する。既存ファイルの拒否

#### P. 検証・完了処理(1c の最後)

- [ ] ローカル(Windows)で全コマンドが成功し、`core/`・`db/` の行カバレッジが 90% 以上
- [ ] ダミーデータ(品目 5,000・履歴 100,000)を一時ディレクトリに生成し、検査に合格する(所要時間を PR に記載する)
- [ ] 実データの保存先(`%APPDATA%\inventory-manager-mini`)にファイルが作られていない
- [ ] PR を作成し、Windows/Linux の CI 成功後にマージする
- [ ] 本計画書のチェックボックスをすべて埋める

## 4. 成果物一覧

| 区分 | ファイル |
| --- | --- |
| 新規 | `src/inventory_manager_mini/core/{timeutil,errors,models,services,reports}.py` |
| 新規 | `src/inventory_manager_mini/db/{connection,migrations,repositories,backup}.py`、`src/inventory_manager_mini/db/schema.sql` |
| 新規 | `scripts/generate_dummy_data.py` |
| 新規 | `tests/test_{timeutil,connection,backup,schema_constraints,migrations,repositories,inventory_service,master_service,settings_service,reports,csv,dummy_data}.py` |
| 更新 | `tests/conftest.py`、`pyproject.toml`、`.github/workflows/ci.yml`、`README.md`、`docs/ローカル在庫管理アプリ開発計画書.md`(3.3) |

## 5. 完了条件

- 上記タスクのチェックボックスがすべて埋まっている
- 開発計画書 8 章の `db/`・`core/` 観点のテストが、ローカル(Windows)と CI(Windows/Linux)で成功する
- `core/`・`db/` の行カバレッジが 90% 以上(CI の閾値ステップで確認)
- `core/`・`db/` の pyright strict がエラー 0 件で、依存ルールのテストが成功する
- 1a・1b・1c の 3 PR が `main` にマージされている
