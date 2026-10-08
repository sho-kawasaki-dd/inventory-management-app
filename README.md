# 備品在庫管理アプリ

設備管理現場で使う備品の在庫を、ローカル PC で管理する PySide6 デスクトップアプリです。品目・関連マスタ、入出庫履歴、低在庫通知、CSV 出力、ダッシュボード、印刷、バックアップなどを提供する計画です。

## 対象環境

- Windows 10 以上
- Linux (Python 環境を用意して実行)
- Python 3.13 以上
- パッケージ管理に uv を使用

## 開発環境の準備

Python 3.13 と uv をインストールした後、リポジトリのルートで依存関係を同期します。

```sh
uv sync
```

## 検査・テスト

次のコマンドは CI でも実行します。依存関係の再現性を確認する場合は `uv sync --locked` を使います。

```sh
uv sync --locked
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest --cov
uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90
```

## アプリの起動

Python 3.13 と uv をインストールした後、リポジトリのルートで依存関係を同期して起動します。

```sh
uv sync --locked
uv run python -m inventory_manager_mini
```

### Windows

PowerShell で実行します。

```powershell
uv sync --locked
uv run python -m inventory_manager_mini
```

### Linux

Qt の実行に必要な OS パッケージをインストールしてから起動します。

```sh
sudo apt-get update
sudo apt-get install --yes libegl1 libgl1 libxkbcommon0 libdbus-1-3 libfontconfig1 libxcb-cursor0
uv sync --locked
uv run python -m inventory_manager_mini
```

通常起動では `QT_QPA_PLATFORM=offscreen` を設定しません。

### データ保存先

既定では OS ごとのユーザーデータ領域に保存します。保存先を変更する場合は、起動前に `INVENTORY_MANAGER_MINI_DATA_DIR` を設定してください。DB・バックアップ・多重起動ロックは指定したディレクトリ直下に、ログはその `logs/` 配下に作成します。

PowerShell:

```powershell
$env:INVENTORY_MANAGER_MINI_DATA_DIR = "$env:LOCALAPPDATA\InventoryManagerMini"
uv run python -m inventory_manager_mini
```

Linux:

```sh
export INVENTORY_MANAGER_MINI_DATA_DIR="$HOME/.local/share/inventory-manager-mini"
uv run python -m inventory_manager_mini
```

この環境変数は、テストや一時的な動作確認で通常の保存先とデータを分ける場合にも使えます。

### Linux のヘッドレステスト

GUI を表示できない環境でテストする場合は、通常起動とは分けて次を実行します。

```sh
QT_QPA_PLATFORM=offscreen uv run pytest --cov
uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90
```

### 性能テスト

性能テストは既定のテスト実行ではスキップされます。明示的に実行する場合は次を使います。5,000 件の品目と 100,000 件の履歴を使い、別プロセス起動・検索・絞り込み・ソートを計測します。

```sh
uv run pytest --run-perf tests/test_performance.py
```

## ディレクトリ構成

```text
.
├── .github/workflows/ci.yml
├── docs/
├── src/inventory_manager_mini/
│   ├── core/
│   ├── db/
│   └── ui/
└── tests/
```

## 開発ルール

- `main` は常にテスト成功状態を保ち、`feature/<フェーズ>-<内容>` ブランチで作業して PR でマージします。
- コミットメッセージは Conventional Commits に従います (例: `feat:`、`fix:`、`test:`、`docs:`)。
- 層の依存方向やデータベース運用などの設計・業務ルールは[開発計画書](docs/ローカル在庫管理アプリ開発計画書.md)を参照してください。
- Phase 0 の作業項目は[Phase 0 実装計画書](docs/実装計画書_Phase0.md)を参照してください。
