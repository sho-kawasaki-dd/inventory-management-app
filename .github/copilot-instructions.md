# Inventory Manager mini (備品在庫管理アプリ) 開発ガイドライン

## プロジェクト概要・仕様の正本
- 本プロジェクトの基本設計・仕様・業務ルールの正本は [docs/ローカル在庫管理アプリ開発計画書.md](docs/ローカル在庫管理アプリ開発計画書.md) です。
- [docs/ローカル在庫管理アプリ企画書.md](docs/ローカル在庫管理アプリ企画書.md) は企画段階の参考記録であり、変更せず内容の確認には使用しません。設計や仕様の確認はすべて開発計画書を基準とします。
- UIテキスト、ドキュメント、コミットメッセージ、コード内コメントは日本語を使用します。

## アーキテクチャと依存関係ルール
- **層構成と依存方向**: `ui/ (PySide6)` → `core/ (純粋Python)` → `db/ (sqlite3)`
  - 業務処理の呼び出しは上記の一方向を厳守します。
  - `db/` から Service や UI を参照することは禁止します。
- **PySide6 非依存の徹底**:
  - `core/` および `db/` のモジュールは `PySide6` を絶対に import しません（UIなしで高速かつ軽量に単体テストを実行可能にするため）。
  - `core/` と `db/` の非依存性はテストで自動検証します。
- **共通契約モジュール (`models.py` / `errors.py`)**:
  - `core/models.py`（dataclass）および `core/errors.py`（`DomainError` 基底）は独立した契約とし、`db/`・Service・UI に依存させません（循環参照防止）。
  - `db/` から `core/models.py` および `core/errors.py` への import のみ許可します。
- **レイヤ境界の厳守**:
  - UI は Service のみを経由してデータ操作を行い、Repository や SQL を直接扱いません。
- **トランザクション境界**:
  - Service 層がトランザクション境界を持ちます（1つの業務操作 = 1トランザクション）。

## データベース・トランザクション規約
- **明示的なトランザクション制御**:
  - `sqlite3.connect(path, autocommit=True)` を使用します。
  - 暗黙の `conn.commit()` や `with conn:` は使用せず、`conn.execute("BEGIN IMMEDIATE")`、`conn.execute("COMMIT")`、`conn.execute("ROLLBACK")` による明示制御（`transaction(conn)` コンテキストマネージャ）に統一します。
- **接続時 PRAGMA**:
  - 接続直後に必ず `PRAGMA foreign_keys = ON; PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000;` を実行します。
- **二重防衛と例外ハンドリング**:
  - 業務整合性は Service で事前検査し、DB 制約（CHECK, UNIQUE, FOREIGN KEY）を最終防衛線とします。
  - `sqlite3.IntegrityError` 等の低レベル例外は、Repository 層で対応する業務例外（`DomainError` 派生クラス）に変換して上位に伝播させます。
- **日時表現**:
  - 日時は `'YYYY-MM-DD HH:MM:SS'` 形式の UTC 文字列で DB に保存します。
  - 表示（UI・CSV・印刷）のみ OS のローカルタイムゾーンへ変換します。CSV の日時列はローカル時刻のみで出力します。
  - 現在時刻の取得・変換・ローカル暦の期間境界計算は `core/timeutil.py` に集約し、それ以外で `datetime.now()` や SQLite の `'localtime'` を使いません。
  - 月・年度などの期間集計は、Python 側でローカル暦の境界を UTC に変換し、半開区間 `[start, end)` で絞ります（SQL 側でタイムゾーン補正しない）。
- **SQL のパラメータ化**:
  - SQL はすべてプレースホルダでパラメータ化し、f-string 等での文字列組み立ては禁止します。

## 在庫整合性ルール
- `items.quantity` は Service の在庫操作経由でのみ更新します（品目編集では変更不可）。
- 履歴 (`stock_movements`) の INSERT と `items.quantity` の UPDATE は同一トランザクションで行います。
- 履歴は物理削除せず、取り消しは `reversal_of` を持つ逆仕訳（元行の `delta` を符号反転）で行います。
- 負の在庫を禁止します（`NegativeStockError`）。
- 品目・所有者・担当者は物理削除せず `is_active` による論理削除とします（未使用マスタの物理削除可否は計画書 5.4 に従う）。
- `moved_at` は常に記録時刻（UTC）とし、遡り入力は不可とします。

## スキーマ変更・マイグレーション規約
- スキーマを変更する際は、`db/schema.sql`、`db/migrations.py` の `{版数: SQL}`、`SCHEMA_VERSION`、版別スキーマ検査定義を同時に更新します。
- 各版の適用と `PRAGMA user_version` の更新は同一トランザクションで行い、適用後にその版のスキーマを検査してから COMMIT します。
- アプリ版数（`pyproject.toml`）とスキーマ版数（`SCHEMA_VERSION`）は独立して管理します。

## セキュリティ規約
- 販売ページ URL は `http`/`https` かつホスト名ありのみ許可し、`QDesktopServices.openUrl` に渡す前に再検証します。
- CSV 出力では、`=` `+` `-` `@` タブ・CR で始まる文字列セルの先頭に `'` を付与します（数値列は対象外）。
- ネットワーク通信はブラウザ起動以外に行いません。

## 例外・ログ規約
- 業務例外は `core/errors.py` の `DomainError` 派生クラスとして追加します。
- UI は `DomainError` を捕捉してメッセージを表示します。それ以外の例外はログ出力の上で汎用エラーダイアログを表示します。
- ログ（`logging`）は障害調査用途に限定します。在庫操作の記録は DB 履歴を正とします。

## 起動シーケンス
- 起動順は「多重起動チェック (`QLockFile`) → ログ初期化 → DB オープン・マイグレーション → MainWindow 表示 → 低在庫通知」とします。
- DB を開く前にロックを取得します（マイグレーション・復元との競合防止）。

## 開発ルール
- `main` は常にテスト成功状態を保ち、作業は `feature/<フェーズ>-<内容>` ブランチで行い PR でマージします。
- `main` には GitHub のリポジトリルールがあり、直接 push は `GH013`(必須ステータスチェック `test (windows-latest)`・`test (ubuntu-latest)` 未達)で拒否されます。`main` へ push せず、feature ブランチを push して PR を作成し、CI 成功後にマージします。
- `main` 上にコミットしてしまった場合は、`git switch -c <ブランチ名>` でブランチへ移した後、`git branch -f main origin/main` でローカル `main` を戻します。
- CI のジョブ名(`test (<os>)`)を変更するときは、リポジトリルールの必須チェック名も同時に更新します。

## コマンド体系（uv）
開発作業・テスト・静的検査はすべて `uv` 経由で実行します。
- **依存関係同期**: `uv sync --locked`
- **コード整形・静的検査**: `uv run ruff check` / `uv run ruff format --check`
- **型検査**: `uv run pyright`
  - `core/` および `db/` は `strict`
  - `ui/` は `standard`
- **テスト実行**: `uv run pytest --cov`
  - Linux 環境では `QT_QPA_PLATFORM=offscreen` を設定
- **アプリ起動**: `uv run python -m inventory_manager_mini`

## 実装計画書のルール

- 実装計画書を作成する際は、以下のルールを守ること
  - **目的**: 何のために実装するのかを明確にする。
  - **要件**: 満たすべき仕様を明確にする。
  - **タスク**: 実装者が理解できるよう、具体的かつ段階的に記述し、完了確認用のチェックボックス（- [ ]）を配置する。

## 実装時のルール

- **重要**: 実装計画書に従って実装するときは、各タスクの完了確認後、実装計画書のチェックボックスを埋めること。

## 実装・テスト方針
- **テストカバレッジ目標**: `core/` および `db/` で行カバレッジ 90% 以上を維持します。
- **テスト DB 選定**:
  - ロジック検証には `:memory:` DB を優先して高速化します。
  - WAL、永続化、マイグレーション、バックアップ/復元などの実ファイル挙動検証には `tmp_path` 上の実ファイル DB を使用します（`:memory:` では WAL を使用できないため）。
  - テストで実データの保存先（`%APPDATA%` 等）を使用しません。
- **依存ルールのテスト**:
  - `core/`・`db/` の PySide6 非依存、`db/` から Service/UI への import 禁止、`core/models.py`・`core/errors.py` から `db/`・Service/UI への import 禁止をテストで検証します。
- **時刻のテスト**:
  - サマータイム境界は `tz` 引数に `ZoneInfo` を注入して検証します（現在時刻は `utc_now_str(now=...)` で固定）。
  - `core/timeutil.py` 以外で `datetime.now`・`'localtime'` を使っていないことを依存ルールのテストで検査します。
- **共通フィクスチャ**:
  - Service テスト用のマスタ投入済み DB などの共通フィクスチャは `tests/conftest.py` に置きます。
- **トランザクションの検証**:
  - 成功時の永続化、途中例外・COMMIT 失敗時のロールバックを検証します（履歴 INSERT 後・数量 UPDATE 前の失敗でも両方が元に戻ること）。
- **コミット規約**:
  - Conventional Commits（`feat:`, `fix:`, `test:`, `docs:`, `refactor:` など）に従います。
