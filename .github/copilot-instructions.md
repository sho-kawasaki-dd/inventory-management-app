# Inventory Manager mini (備品在庫管理アプリ) 開発ガイドライン

## プロジェクト概要・仕様の正本
- 本プロジェクトの基本設計・仕様・業務ルールの正本は [docs/ローカル在庫管理アプリ開発計画書.md](docs/ローカル在庫管理アプリ開発計画書.md) です。
- [docs/ローカル在庫管理アプリ企画書.md](docs/ローカル在庫管理アプリ企画書.md) は企画段階の参考記録であり、変更せず参照もしません。設計や仕様の確認はすべて開発計画書を基準とします。
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
  - 日時はすべて `'YYYY-MM-DD HH:MM:SS'` 形式のローカル時刻文字列で保存・扱います。

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
  - WAL、永続化、マイグレーション、バックアップ/復元などの実ファイル挙動検証には `tmp_path` 上の実ファイル DB を使用します。
- **コミット規約**:
  - Conventional Commits（`feat:`, `fix:`, `test:`, `docs:`, `refactor:` など）に従います。
