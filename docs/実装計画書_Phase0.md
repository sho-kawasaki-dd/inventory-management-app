# Phase 0 実装計画書(開発基盤)

- 作成日: 2026-10-01
- ステータス: 案(レビュー待ち)
- 作業ブランチ: `feature/phase0-foundation`
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(2.1・2.2・3.1・3.2・3.6・9 章)

---

## 1. 目的

- Phase 1 以降が、依存関係・静的検査・テスト・CI の整った土台の上で始められる状態にする。
- 層構成と依存ルールを、空パッケージの段階からテストで機械的に守らせる。
- PySide6・QtCharts・QtPrintSupport が Windows/Linux の CI で動くことを早期に確認する(開発計画書 10 章のリスク対応)。

## 2. 要件

### 2.1 満たすべき仕様

- 開発計画書 2.1(ツールチェーン)、2.2 の手順 1〜4(CI)、3.1・3.2(層構成・モジュール構成)、3.6(ディレクトリ構成)に従う。
- 完了条件(開発計画書 9 章): CI が Windows/Linux で成功する。
- 次の 5 コマンドがローカルと CI の両方で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`core/`・`db/` は strict、`ui/` は standard)
  - `uv run pytest --cov`(Linux は `QT_QPA_PLATFORM=offscreen`)

### 2.2 決定事項

| 項目 | 決定 |
| --- | --- |
| 依存ルールのテスト | Phase 0 から導入する |
| PySide6・pytest-qt | Phase 0 から依存に含め、Linux CI に Qt 用 OS パッケージと offscreen を整備する |
| 開発依存 | pytest、pytest-qt、pytest-cov、ruff、pyright、pyinstaller、tzdata(開発計画書 2.1 のとおり) |
| 追加の基盤整備 | `.gitignore`・`.gitattributes`・README の開発手順を整備する |
| version / license | version = `0.1.0`、license は未設定(開発計画書 11 章 #3 は Phase 8 前に決定) |
| ruff | line-length = 100、select = `E,W,F,I,UP,B,SIM` |
| カバレッジ | `fail_under`(core/db 90%)は Phase 1 で導入する |
| `config.py` | `APP_NAME` のみ。パス解決は Phase 1・2 で実装する |
| `app.main()` | 0 を返すスタブ。起動シーケンスは Phase 2 で実装する |
| `ui/` の import 制限 | UI は Service 経由のみとするため、`sqlite3`・`db` の import を禁止する(規則 5) |

### 2.3 対象外

- 業務ロジック、DB スキーマ、画面
- 開発計画書 2.2 の手順 5(リリースワークフロー、Phase 8)
- `packaging/`・`scripts/` ディレクトリ
- Dependabot、PR テンプレート、pre-commit
- カバレッジの fail_under

## 3. タスク

### A. プロジェクト設定

- [x] `.python-version` に `3.13` を記載する
- [x] `pyproject.toml` を作成する
  - [x] `[project]`: name = `inventory-manager-mini`、version = `0.1.0`、`requires-python = ">=3.13"`、license 未設定
  - [x] 依存: PySide6、platformdirs
  - [x] ビルドバックエンド: hatchling。wheel の対象は `src/inventory_manager_mini`
  - [x] `[dependency-groups] dev`: pytest、pytest-qt、pytest-cov、ruff、pyright、pyinstaller、tzdata
  - [x] ruff: line-length = 100、target = py313、select = `E,W,F,I,UP,B,SIM`
  - [x] pyright: pythonVersion = 3.13、include = `src`・`tests`、typeCheckingMode = `standard`、strict = `src/inventory_manager_mini/core`・`src/inventory_manager_mini/db`
  - [x] pytest: testpaths = `tests`、`qt_api = "pyside6"`、addopts = `-ra --strict-markers --strict-config`
  - [x] coverage: source = `inventory_manager_mini`(行カバレッジのみ)
- [x] `.gitignore` を作成する(`.venv/`、`__pycache__/`、各種キャッシュ、`.coverage`、`htmlcov/`、`build/`、`dist/`、`*.db*`、`.idea/`、`.vscode/`)。`uv.lock` を除外しないよう `*.lock` は指定しない
- [x] `.gitattributes` を作成する(`* text=auto eol=lf`、`*.png`・`*.ico`・`*.db` は binary)

### B. 空パッケージ・依存同期(A に依存)

- [x] `src/inventory_manager_mini/__init__.py`: `__version__` を `importlib.metadata` から取得して定義する
- [x] `src/inventory_manager_mini/__main__.py`: `app.main()` を呼び、戻り値で終了する
- [x] `src/inventory_manager_mini/app.py`: `main() -> int`(0 を返すスタブ)
- [x] `src/inventory_manager_mini/config.py`: `APP_NAME = "inventory-manager-mini"`
- [x] `core/__init__.py`・`db/__init__.py`・`ui/__init__.py` を空で作成する(strict/standard の適用先を実在させる)
- [x] `tests/__init__.py`・`tests/conftest.py`(共通フィクスチャの置き場。現時点は空)を作成する
- [x] 上記ファイルの作成後に `uv sync` を実行し、自プロジェクトの editable インストールと依存同期を行って `uv.lock` を生成する(コミット対象)。ソースが存在しない段階では実行しない

### C. テスト(B に依存)

- [x] `tests/test_smoke.py`
  - [x] パッケージを import でき、`__version__` が `pyproject.toml` の version と一致する
  - [x] `main()` が 0 を返す
  - [x] `python -m inventory_manager_mini` をサブプロセスで実行し、終了コード 0 になる
- [x] `tests/test_qt_smoke.py`
  - [x] `qtbot` で `QWidget` を生成できる
  - [x] `PySide6.QtCharts`(`QChartView`)と `PySide6.QtPrintSupport`(`QPrinter`)を import できる
  - [x] `QApplication` 下でデータ系列を持つ最小の `QChart`・`QChartView` を生成し、描画した結果を `tmp_path` に画像として保存できる(画像が空でなく、チャートが描画されていることを確認する)
  - [x] `QPrinter` を PDF 出力に設定し、`QPainter` で最小の描画を行って `tmp_path` に空でない PDF を生成できる(実プリンタは使用しない。実用紙での確認は Phase 7)
- [x] `tests/test_dependency_rules.py`: src を AST 解析して次の規則を検査する(相対 import は絶対モジュール名へ解決する)
  - [x] 規則 1: `core/`・`db/` は PySide6 を import しない
  - [x] 規則 2: `db/` から import してよい `core` は `core.models`・`core.errors` のみ(`core.services`・`core.reports`・`ui` は禁止)
  - [x] 規則 3: `core/models.py`・`core/errors.py` は `db`・`core.services`・`core.reports`・`ui` を import しない
  - [x] 規則 4: `core/` は `ui` を import しない
  - [x] 規則 5: `ui/` は `sqlite3`・`inventory_manager_mini.db` を import しない
  - [x] 規則 6: `core/timeutil.py` 以外で `datetime.now`・`utcnow`・`today`、`date.today`、文字列 `'localtime'` を使わない(`.py` と `.sql` が対象)
- [x] 規則ごとに自己検証テストを書く。`tmp_path` に違反コードを置き「違反を検出する」と「適合なら通る」の両方を確認する(空パッケージで検査が空振りしないようにするため)

### D. CI(A・B・C に依存)

- [x] `.github/workflows/ci.yml` を作成する
  - [x] トリガー: `push`(main)と `pull_request`
  - [x] `permissions: contents: read`、`concurrency` で同一ブランチの旧実行をキャンセルする
  - [x] matrix: `windows-latest`・`ubuntu-latest`、`fail-fast: false`、ジョブ名は `test (<os>)`
  - [x] uv の導入は `astral-sh/setup-uv`(キャッシュ有効)。各 Action は実装時に最新の安定メジャー版を確認して固定する
  - [x] Linux のみ Qt 用 OS パッケージを apt で導入し、`QT_QPA_PLATFORM=offscreen` を設定する
    - 候補: `libegl1`、`libgl1`、`libxkbcommon0`、`libdbus-1-3`、`libfontconfig1`、`libxcb-cursor0`
    - Qt スモークテストの import・チャート描画・PDF 出力がすべて通る最小構成を初回 CI の結果で確定する
  - [x] ステップ: `uv sync --locked` → `uv run ruff check` → `uv run ruff format --check` → `uv run pyright` → `uv run pytest --cov`

### E. README(A・B と並行可)

- [ ] 概要・対象 OS を記載する
- [ ] `uv sync` と 5 コマンド、アプリ起動のコマンド一覧を記載する
- [ ] Linux の通常起動手順(`uv sync` → `uv run python -m inventory_manager_mini`、Qt 用 OS パッケージ)を記載する。通常起動には `QT_QPA_PLATFORM=offscreen` を設定しない
- [ ] Linux のヘッドレステスト手順を通常起動と分離し、`QT_QPA_PLATFORM=offscreen uv run pytest --cov` を記載する
- [ ] Phase 0 の起動コマンドは終了コード 0 で正常終了するだけで画面を表示せず、画面の起動は Phase 2 で実装する旨を記載する
- [ ] ディレクトリ構成、開発ルール(ブランチ・Conventional Commits)、開発計画書へのリンクを記載する

### F. 検証・完了処理(A〜E に依存)

- [ ] ローカル(Windows)で 5 コマンドがすべて成功する
- [ ] 下記の未注釈・既定値なしの引数を持つ関数を検証専用ファイルとして `core/`・`db/` にそれぞれ一時配置し、`uv run pyright` が非ゼロ終了し、`reportUnknownParameterType`・`reportMissingParameterType` を報告することを確認する。戻り値だけが未注釈の関数は推論で通るため検証例にしない
- [ ] 同じ検証例を `ui/` に一時配置し、standard では上記の診断が出ず `uv run pyright` が成功することを確認する。各確認後に検証専用ファイルを削除し、最終状態で型検査が成功することを確認する

検証例:

```python
def probe(value):
    return value
```

- [ ] リポジトリの公開設定・GitHub プラン・管理権限を確認し、branch protection で必須チェックを設定できるか確認する。利用不可の場合は代替運用と完了条件を利用者と合意し、本計画書へ反映する(未合意のまま完了扱いにしない)
- [ ] ブランチを push して PR を作成し、Windows/Linux 両方の CI が成功する(Linux の OS パッケージは必要に応じて調整)
- [ ] マージ後、GitHub の branch protection で `test (windows-latest)`・`test (ubuntu-latest)` を必須チェックに設定する(手動作業。利用不可の場合は事前に合意した代替運用を適用する)
- [ ] 本計画書のチェックボックスをすべて埋める

## 4. 成果物一覧

| 区分 | ファイル |
| --- | --- |
| 新規 | `.python-version`、`pyproject.toml`、`uv.lock`、`.gitignore`、`.gitattributes` |
| 新規 | `.github/workflows/ci.yml` |
| 新規 | `src/inventory_manager_mini/{__init__,__main__,app,config}.py`、`core/__init__.py`、`db/__init__.py`、`ui/__init__.py` |
| 新規 | `tests/{__init__,conftest,test_smoke,test_qt_smoke,test_dependency_rules}.py` |
| 更新 | `README.md` |

## 5. 完了条件

- 上記タスクのチェックボックスがすべて埋まっている
- ローカル(Windows)と CI(Windows/Linux)で 5 コマンドがすべて成功する
- `main` ブランチで CI が必須チェックとして設定されている。設定機能を利用できない場合は、事前に利用者と合意して本計画書に反映した代替運用・完了条件を満たしている
