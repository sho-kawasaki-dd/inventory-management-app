# Phase 8 実装計画書(パッケージング)

- 作成日: 2026-10-13
- ステータス: 計画(未着手)
- 作業ブランチ: `feature/phase8-packaging`(8a)→ `feature/phase8-release-workflow`(8b)
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(2.1・2.2・2.3・3.5・7.2・8・9・9.2・10・11 章)、[実装計画書_Phase2](実装計画書_Phase2.md)(4.2 試験ビルドの結果)、[実装計画書_Phase7](実装計画書_Phase7.md)(試し刷りの未実施項目)
- 改訂履歴: 2026-10-13 初版。発行元・ライセンス・アイコン・インストールモード・タグ規則・成果物・凍結版の検査範囲・PR 分割(2 PR)を決定

---

## 1. 目的

- Windows 現場 PC へ配布できるインストーラー(Inno Setup)を、PyInstaller の onedir 版から再現可能な手順で作成できるようにする。
- タグ push で、検証・ビルド・成果物(インストーラーと SHA256)の保存・GitHub Release(下書き)の作成までを自動化する。
- 完了条件(開発計画書 9 章): フェーズ 5・6・7 がすべて完了し、8 章の配布チェックリストに合格すること。

## 2. 要件

### 2.1 満たすべき仕様

- 開発計画書 2.2 手順 5、9.2、10 章、8 章(配布は手動チェックリスト)に従う。
- PyInstaller は `--onedir --windowed` とし、Phase 2 の試験ビルド(`build/inventory-manager-mini.spec`、コミット対象外)の設定を `packaging/` の正式な spec に移す。
  - `inventory_manager_mini/db` の `schema.sql`・`schema_v3.sql` を同梱する(`importlib.resources` で読み込むため)。
  - アプリの配布メタデータ(dist-info)を同梱し、`importlib.metadata.version("inventory-manager-mini")` を凍結版でも成功させる。
  - `PySide6.QtCharts`・`PySide6.QtPrintSupport` を同梱する。Windows 版 PySide6 には `plugins/printsupport/` がなく、`Qt6PrintSupport.dll` に内蔵されている(Phase 2 4.2)。
  - 動的に import される画面モジュールの取りこぼしを防ぐため、`inventory_manager_mini` の全サブモジュールを収集する。
  - `console=False`。`upx=False`(UPX の有無でビルド結果が変わること、およびウイルス対策ソフトの誤検知を避けるため)。
  - アイコンは `packaging/app.ico` が存在する場合のみ指定する。
- Inno Setup スクリプトは次に従う。
  - アプリ名 `Inventory Manager mini`、発行元 `IKinoCore`。
  - `PrivilegesRequired=admin` を既定とし、`PrivilegesRequiredOverridesAllowed=dialog` でインストール時に「全ユーザー」「自分のみ」を選択できる。
  - 既定のインストール先は `{autopf}\Inventory Manager mini`。スタートメニューに登録する。デスクトップのショートカットは任意タスク(既定オフ)。
  - 64 ビット専用、Windows 10 以上、日本語 UI。ライセンス表示は MIT の `LICENSE`。
  - アンインストール時に `%APPDATA%\inventory-manager-mini`(DB・バックアップ・ロック・`ui_settings.json`)と `%LOCALAPPDATA%` 配下のログを削除しない(`[UninstallDelete]` を記述しない)。
  - 実行中のアプリを検知し、更新・アンインストール時に終了を促す。
  - 同じ `AppId`(固定の GUID)で上書き更新できる。
- 版数: アプリ版数の正本は `pyproject.toml`(初版は `0.1.0` のまま)。スキーマ版数とは独立(開発計画書 2.3)。
- タグ規則: `v<版数>` を正式リリース、`v<版数>-rc<N>` を検証用とする。タグから接尾辞を除いた版数が `pyproject.toml` と一致しない場合はワークフローを失敗させる。
- リリースワークフロー(`.github/workflows/release.yml`):
  - 先に Windows でテスト一式(ruff・pyright・pytest・カバレッジ)を実行する。
  - タグのコミットが `origin/main` に含まれることを確認する。
  - 成果物はインストーラー exe と SHA256 ファイル。workflow artifact に保存し、GitHub Release を**下書き**で作成して添付する。`-rc` タグは pre-release とする。公開は手動。
  - `contents: write` はリリース作成のジョブだけに付与する。
  - ジョブ名を `test (<os>)` にしない(`main` の必須ステータスチェックと衝突させないため。ルールは `ci.yml` のジョブ名に紐づく)。
- 凍結版の自動検査は同梱ファイル検査のみとし、CI で exe は起動しない。アプリのコードは変更しない。
- ライセンス: プロジェクトは MIT(著作権者 `IKinoCore`)。PySide6・Qt(LGPL-3.0)ほかの第三者ライセンス表記を配布物に同梱する。onedir 構成のため Qt の DLL は利用者が差し替え可能である。
- ネットワーク通信を新たに追加しない(開発計画書 7.4)。インストーラー・アプリとも自動更新は持たない。
- 次のコマンドがローカル(Windows)と CI(Windows/Linux)で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`scripts/` を含む。`core/`・`db/` は strict、それ以外は standard でエラー 0 件)
  - `uv run pytest --cov`(Linux は `QT_QPA_PLATFORM=offscreen`)
  - `uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90`
- テストで実データの保存先(`%APPDATA%` 等)を使用せず、疑似ディレクトリは `tmp_path` に作る。

### 2.2 既存実装の状況(調査結果)

| 項目 | 状況 |
| --- | --- |
| 試験ビルドの spec | `build/inventory-manager-mini.spec`(`build/`・`dist/` は `.gitignore` 済み)。`collect_data_files('inventory_manager_mini.db')`、`copy_metadata('inventory-manager-mini')`、hiddenimports(QtCharts・QtPrintSupport・`item_dialog`・`master_dialog`)、`upx=True`、`console=False` |
| `packaging/`・`.iss`・アイコン・`LICENSE` | いずれも存在しない。`pyproject.toml` に license の設定もない |
| SQL の読み込み | `db/migrations.py` が `importlib.resources.files("inventory_manager_mini.db")` で `schema.sql`・`schema_v3.sql` を読む。他に非 `.py` の資源はない |
| 版数の取得 | `__init__.py` が `importlib.metadata.version("inventory-manager-mini")` で `__version__` を設定。`app.py` は `PackageNotFoundError` 時に `__version__` へフォールバック。バージョン情報は `AppContext.app_version` を表示 |
| 再起動 | `app.py` の `build_restart_command()` が `sys.frozen` を判定し、凍結版は `[sys.executable]`、それ以外は `[sys.executable, "-m", "inventory_manager_mini"]`。`QProcess.startDetached` で起動 |
| ログ・例外 | `RotatingFileHandler`(1 MB × 5 世代)に出力。`sys.excepthook` が記録してダイアログ表示。`--windowed` で `sys.stdout` が `None` でも、アプリは標準出力を使っていない |
| CI | `.github/workflows/ci.yml` のジョブ `test (<os>)`(windows-latest・ubuntu-latest)。リリースワークフローは未作成 |
| 試験ビルドの確認結果 | Phase 2 4.2: QtCharts・QtPrintSupport・SQL・メタデータの同梱、起動・DB 作成、多重起動の拒否を確認済み。品目登録の DB 保存とバージョン情報の表示内容は未確認 |
| `scripts/` | `generate_dummy_data.py` のみ。pyright の対象(`include` に `scripts`) |
| Phase 7 の未了事項 | 実用紙への試し刷り(手動確認 #1・#6・#7)が未実施。`LabelSpec` の寸法は推定値のまま |

### 2.3 決定事項

| 項目 | 決定 |
| --- | --- |
| 正式リリースの時期 | Phase 8 の実装と rc 検証は先に進める。正式タグ `v0.1.0` は Phase 7 の試し刷り完了後に作る |
| ライセンス・著作権者 | MIT、`Copyright (c) 2026 IKinoCore`。`LICENSE` を追加し、`pyproject.toml` に `license`・`license-files`・`authors` を設定する |
| 発行元 | `IKinoCore`(インストーラーの `AppPublisher`) |
| アイコン | 利用者が `packaging/app.ico` を用意する。未配置の場合は既定のアイコンでビルドする(ビルドは失敗させない)。ウィンドウアイコンの設定(`QApplication.setWindowIcon`)は行わない(アプリ変更なし) |
| インストールモード | ダイアログで選択。既定は全ユーザー(管理者権限、`{autopf}`)で、「自分のみ」も選択可 |
| 初版の版数 | `0.1.0`(変更しない) |
| 成果物と公開 | インストーラー exe と SHA256 を workflow artifact に保存し、GitHub Release(下書き)に添付。公開は手動 |
| タグ規則 | `v<版数>` = 正式、`v<版数>-rc<N>` = 検証用(pre-release の下書き)。版数は `pyproject.toml` と一致必須 |
| 凍結版の検査 | 同梱ファイル検査のみ。起動確認は手動チェックリスト |
| デスクトップのショートカット | 任意タスク(既定オフ) |
| UPX | 使用しない |
| 出力ファイル名 | `inventory-manager-mini-<版数>[-rc<N>]-setup.exe`、SHA256 は同名に `.sha256` を付ける。rc の接尾辞は `build.ps1` の `-Suffix` で渡す |

### 2.4 対象外

- コード署名(SmartScreen の警告は README で案内する)。
- Linux 向けパッケージ(README の `uv sync` 手順のみ。9.2)。
- 自動アップデート、MSI・MSIX 形式、`winget` 等への登録。
- CI での exe 起動テスト、アプリへのスモークテスト用引数の追加。
- アプリ本体のコード変更(ウィンドウアイコンを含む)。
- Phase 7 の実用紙試し刷りと `LabelSpec` の確定(別途実施。11 章 #1)。

## 3. タスク

### 3.1 PR 分割

| PR | ブランチ | 内容 |
| --- | --- | --- |
| 8a | `feature/phase8-packaging` | 文書・LICENSE・spec・Inno Setup・ビルドスクリプト・検査スクリプトとテスト・README・ローカルでのビルド確認 |
| 8b | `feature/phase8-release-workflow` | `release.yml`・文書更新。マージ後に rc タグで検証 |

各 PR は CI 成功後にマージし、次のブランチは最新の `main` から作成する。`main` へは直接 push しない。

### 3.2 8a. パッケージング基盤(`feature/phase8-packaging`)

#### A. ライセンス・メタデータ

- [ ] `LICENSE`(MIT、`Copyright (c) 2026 IKinoCore`)を追加する
- [ ] `pyproject.toml` に `license = "MIT"`・`license-files = ["LICENSE"]`・`authors = [{ name = "IKinoCore" }]` を追加し、`uv lock` で `uv.lock` を更新して `uv sync --locked` が成功することを確認する
- [ ] `packaging/THIRD_PARTY_NOTICES.txt` に PySide6・Qt・shiboken6(LGPL-3.0)、Python(PSF)、platformdirs(MIT)の名称・ライセンス・入手先を記載する。LGPL の再配布条件(利用者による Qt ライブラリの差し替えが可能であること)も明記する
- [ ] 同梱されるライセンスファイル(PySide6 の dist-info 等)の実体を確認し、不足があれば spec で追加する

#### B. PyInstaller spec

- [ ] `packaging/inventory-manager-mini.spec` を作成する(パスは `SPECPATH` 基準)
  - [ ] エントリは `src/inventory_manager_mini/__main__.py`
  - [ ] `collect_data_files('inventory_manager_mini.db')` と `copy_metadata('inventory-manager-mini')`
  - [ ] hiddenimports: `PySide6.QtCharts`・`PySide6.QtPrintSupport`・`collect_submodules('inventory_manager_mini')`
  - [ ] `console=False`・`upx=False`・`name='inventory-manager-mini'`
  - [ ] `LICENSE` と `THIRD_PARTY_NOTICES.txt` を `datas` でインストール先直下(`_internal` ではなく exe と同じ階層)へ配置する方法を確認し、決める(onedir の `contents_directory` の扱い)
  - [ ] `packaging/app.ico` が存在する場合のみ `icon` を指定する
- [ ] 試験ビルドと同等の出力(`dist/inventory-manager-mini/`)が得られることをローカルで確認する

#### C. Inno Setup スクリプト

- [ ] `packaging/inventory-manager-mini.iss` を作成する
  - [ ] `AppId` の GUID を新規生成して固定する(以後変更しない。11 章 #2)
  - [ ] `AppName`・`AppPublisher=IKinoCore`・`AppVersion`(`/DAppVersion` で受け取る)・`OutputBaseFilename`(`/DOutputName` で受け取る)
  - [ ] `PrivilegesRequired=admin`・`PrivilegesRequiredOverridesAllowed=dialog`・`DefaultDirName={autopf}\Inventory Manager mini`
  - [ ] `ArchitecturesAllowed=x64compatible`・`ArchitecturesInstallIn64BitMode=x64compatible`・`MinVersion=10.0`
  - [ ] `[Languages]` に `Japanese.isl`、`LicenseFile` に `LICENSE`
  - [ ] `[Files]` に `dist\inventory-manager-mini\*`(`recursesubdirs`)
  - [ ] `[Icons]` にスタートメニュー(`{autoprograms}`)と、`[Tasks]` の `desktopicon`(`Flags: unchecked`)
  - [ ] `[Run]` にインストール後の起動(`postinstall skipifsilent nowait`)
  - [ ] `CloseApplications=yes`・`RestartApplications=no`
  - [ ] `SetupIconFile`・`UninstallDisplayIcon` は `app.ico` の有無で `#ifexist` により切り替える
  - [ ] `[UninstallDelete]` を記述しない(ユーザーデータとログを残す)
- [ ] 日本語の言語ファイルが Inno Setup 6 の標準に含まれることを確認する(含まれない場合は代替を計画書へ追記)

#### D. ビルドスクリプト

- [ ] `packaging/build.ps1` を作成する(引数: `-Version`、`-Suffix`(既定は空)、`-IsccPath`(既定は標準のインストール先))
  - [ ] `pyproject.toml` の版数と `-Version` の一致を `scripts/check_release_version.py` で検査する
  - [ ] `uv run pyinstaller --noconfirm --clean packaging/inventory-manager-mini.spec`
  - [ ] `uv run python scripts/check_bundle.py dist/inventory-manager-mini` で同梱検査
  - [ ] `ISCC.exe /DAppVersion=<版数> /DOutputName=inventory-manager-mini-<版数><接尾辞>-setup packaging/inventory-manager-mini.iss`
  - [ ] SHA256 を `<成果物>.sha256`(`ハッシュ  ファイル名` 形式)として出力する
  - [ ] 各手順の失敗で `$ErrorActionPreference = 'Stop'` により中断し、非 0 で終了する
- [ ] 成果物の出力先を `.gitignore` 済みのディレクトリ(`dist/`)に限定し、コミット対象に含めない

#### E. 検査スクリプトとテスト

- [ ] `scripts/check_release_version.py`: 引数のタグ(または版数)が `^v?(\d+\.\d+\.\d+)(-rc\d+)?$` に一致し、`tomllib` で読んだ `pyproject.toml` の `project.version` と一致することを検査する。不一致・形式不正は非 0 で終了し理由を表示する。`-rc` の有無を GitHub Actions が使えるよう標準出力で返せる形にする
- [ ] `scripts/check_bundle.py`: 引数の onedir 出力に対し、次のファイルが存在し空でないことを検査する。欠落を全件列挙して非 0 で終了する
  - [ ] `inventory-manager-mini.exe`
  - [ ] `PySide6/QtCharts.pyd`・`PySide6/Qt6Charts.dll`
  - [ ] `PySide6/QtPrintSupport.pyd`・`PySide6/Qt6PrintSupport.dll`
  - [ ] `PySide6/plugins/platforms/qwindows.dll`
  - [ ] `inventory_manager_mini/db/schema.sql`・`schema_v3.sql`
  - [ ] `inventory_manager_mini-<版数>.dist-info/METADATA`(版数が `pyproject.toml` と一致)
  - [ ] `LICENSE`・`THIRD_PARTY_NOTICES.txt`
  - [ ] 実体の配置は `_internal` 配下を基準とし、ビルド結果に合わせて検査パスを確定する
- [ ] `tests/test_check_release_version.py`: 正式タグ・rc タグの合格、版数不一致・形式不正(接尾辞違い・`v` なし・桁不足)の不合格、rc 判定の出力を検証する(実ファイルの代わりに `tmp_path` の `pyproject.toml` を使う)
- [ ] `tests/test_check_bundle.py`: `tmp_path` に疑似 onedir を作り、全件そろう場合の合格、1 件ずつ欠落・空ファイル・版数不一致の dist-info での不合格と、欠落の全件列挙を検証する
- [ ] `scripts/` のモジュールをテストから読み込む方法は既存の `tests/test_dummy_data.py` に合わせる
- [ ] 依存ルールのテスト(`tests/test_dependency_rules.py`)に影響しないこと(`scripts/` は `src/` から参照されない)を確認する

#### F. README・文書

- [ ] `README.md` に次を追記する
  - [ ] Windows のインストール手順(インストールモードの選択、署名なしのため SmartScreen が警告する場合は「詳細情報」から実行すること、SHA256 の確認方法)
  - [ ] アンインストール後もデータ(`%APPDATA%\inventory-manager-mini`)とログが残ること、完全に消す場合の手動削除手順
  - [ ] ローカルでのビルド手順(Inno Setup 6 の導入、`packaging/build.ps1` の使い方)
  - [ ] アイコン `packaging/app.ico` の配置先と、未配置時は既定アイコンになること
  - [ ] ライセンス(MIT)と第三者ライセンス
- [ ] 本書(Phase 8)のステータスを更新する

#### G. ローカルでの確認

- [ ] `packaging/build.ps1 -Version 0.1.0` が成功し、`check_bundle.py` が合格する
- [ ] 生成したインストーラーで、一時的な保存先(`INVENTORY_MANAGER_MINI_DATA_DIR`)を使わずに実際のインストールと起動を確認する(開発 PC のデータに影響しないよう、確認後にアンインストールし、必要なら `%APPDATA%` のデータを退避する)
- [ ] Phase 2 で未確認だった「品目登録の DB 保存」と「バージョン情報の表示内容(アプリ版・スキーマ版・DB パス)」を凍結版で確認する
- [ ] 検証結果(ビルド環境・成果物サイズ・問題点と対処)を本書 4 章に記録する

### 3.3 8b. リリースワークフロー(`feature/phase8-release-workflow`)

#### H. `.github/workflows/release.yml`

- [ ] トリガー: `push: tags: ['v*']`。ワークフロー全体の `permissions` は `contents: read`、`concurrency` はタグごと
- [ ] `verify` ジョブ(`windows-latest`。ジョブ名は `verify`)
  - [ ] `actions/checkout`、`astral-sh/setup-uv`(`ci.yml` と同じ版・Python 3.13)
  - [ ] `uv sync --locked`、`ruff check`、`ruff format --check`、`pyright`、`pytest --cov`、カバレッジ検査(`ci.yml` と同じコマンド)
  - [ ] `scripts/check_release_version.py "$GITHUB_REF_NAME"` でタグと版数を検査し、rc 判定を `GITHUB_OUTPUT` に出力する
- [ ] `build` ジョブ(`needs: verify`、`windows-latest`)
  - [ ] `fetch-depth: 0` で取得し、`git merge-base --is-ancestor $GITHUB_SHA origin/main` でタグが `main` に含まれることを検査する(含まれなければ失敗)
  - [ ] Inno Setup 6 の有無を確認し、なければ `choco install innosetup --version <固定版> -y` で導入する(版は着手時点の最新 6 系に固定。11 章 #3)
  - [ ] `packaging/build.ps1 -Version <版数> -Suffix <-rcN または空>`
  - [ ] `actions/upload-artifact` でインストーラーと `.sha256` を保存する(保持期間は既定)
- [ ] `release` ジョブ(`needs: build`、`permissions: contents: write`)
  - [ ] `actions/download-artifact` で成果物を取得する
  - [ ] `gh release create "$GITHUB_REF_NAME" --draft [--prerelease] --generate-notes` で下書きを作成し、インストーラーと `.sha256` を添付する(`GH_TOKEN` は `github.token`)
  - [ ] 同名の Release が既にある場合は失敗させる(上書きしない)
- [ ] ワークフロー内で使うアクションの版は `ci.yml` に合わせ、シークレットを追加しない
- [ ] `release.yml` の構文を `actionlint` 等で確認できる場合は確認する

#### I. 文書更新

- [ ] 開発計画書 2.2 手順 5・9.2・11 章 #3 に、発行元(`IKinoCore`)、MIT、アイコン(`packaging/app.ico`、利用者提供)、インストールモード、タグ規則、成果物(下書き Release)を反映する
- [ ] 開発計画書 3.6 のディレクトリ構成に `LICENSE` と `packaging/` の実ファイルを反映する
- [ ] `.github/copilot-instructions.md` に、ビルドコマンド(`packaging/build.ps1`)とタグ規則・リリースの手順、`main` を直接 push しない運用との関係を追記する
- [ ] 本書のステータスを更新する

### 3.4 リリース検証(8b マージ後)

- [ ] `v0.1.0-rc1` を `main` のコミットに作成して push する(push 前に利用者へ確認する)
- [ ] `verify`・`build`・`release` が成功し、下書きの pre-release にインストーラーと `.sha256` が添付される
- [ ] ダウンロードしたインストーラーの SHA256 が添付ファイルと一致する
- [ ] 版数不一致のタグ(例: `v0.1.1-rc1`)がワークフローで失敗することを確認する(確認後にタグ・下書きを削除。削除は利用者の確認を得て行う)
- [ ] 不具合があれば修正して `v0.1.0-rc2` を作成し、上書き更新(rc1 → rc2)の確認も行う
- [ ] 配布チェックリスト(5 章)を実施し、結果を本書に記録する

### 3.5 正式リリース

- [ ] Phase 7 の実用紙試し刷り(実装計画書_Phase7 の手動確認 #1・#6・#7)が完了し、`LabelSpec` と開発計画書 9.1・11 章 #1 が確定している
- [ ] 配布チェックリストがすべて合格している
- [ ] `v0.1.0` を作成して push し(利用者へ確認)、下書きの Release を確認して公開する(公開は手動)
- [ ] 本書のステータスを「実装完了」に更新し、開発計画書 9 章の記載を整合させる

## 4. 検証結果の記録(実施時に記入)

| 項目 | 結果 | 備考 |
| --- | --- | --- |
| ビルド環境(PyInstaller・Python・Qt・Inno Setup の版) | 未実施 | |
| `check_bundle.py` の合格 | 未実施 | |
| インストーラーのサイズ | 未実施 | |
| 凍結版での品目登録・バージョン情報 | 未実施 | |
| rc タグでのワークフロー結果 | 未実施 | |

## 5. 配布チェックリスト(手動)

クリーンな Windows 環境(Windows Sandbox・新規の仮想マシン・Python 未導入の PC 等)で、rc 版のインストーラーを使って実施する。実データの保存先を使うため、開発 PC では行わない。

| # | 確認項目 | 方法 | 結果 |
| --- | --- | --- | --- |
| 1 | インストール(全ユーザー) | 管理者権限でインストール。`C:\Program Files\Inventory Manager mini` への配置、スタートメニューの登録、デスクトップのショートカットが既定で作られないこと | |
| 2 | インストール(自分のみ) | ダイアログで「自分のみ」を選択して別のユーザーで実施。`%LOCALAPPDATA%\Programs` 配下への配置、管理者権限が不要なこと | |
| 3 | 任意タスク | デスクトップのショートカットを選んだ場合のみ作成されること | |
| 4 | 初回起動・DB 作成 | `%APPDATA%\inventory-manager-mini\inventory.db` とログ(`%LOCALAPPDATA%` 配下)が作成され、MainWindow が表示される。バージョン情報にアプリ版・スキーマ版・DB パスが表示される | |
| 5 | 主要操作 | マスタ登録、品目登録、入庫・出庫・返品・廃棄・棚卸、取り消し、履歴、低在庫の着色・ドック・起動時通知 | |
| 6 | 多重起動の拒否 | 起動中にもう一度起動すると「既に起動しています」が表示され、2 つ目だけが終了する | |
| 7 | ダッシュボード(QtCharts) | グラフが表示され、棒のホバーでツールチップが出る。PNG・CSV を保存できる | |
| 8 | 印刷(QtPrintSupport) | 棚ラベル・在庫表のプレビュー表示、PDF への出力。プリンタが未接続でも異常終了しない | |
| 9 | CSV 出力 | 在庫一覧・履歴を出力し、Excel で文字化けしないこと | |
| 10 | バックアップ・復元 | バックアップ取得、復元後に凍結版が自動で再起動すること(`sys.executable` による再起動) | |
| 11 | 設定 | フォント・文字サイズ(150% を含む)が即時反映され、再起動後も保持される。1366×768 で画面が崩れない | |
| 12 | オフライン動作 | ネットワークを切断した状態で、販売ページを開く操作以外の全機能が動作する | |
| 13 | 上書き更新 | rc1 の上に rc2 をインストールして、データ・設定が保持され、バージョン情報が更新される | |
| 14 | 起動中の更新・アンインストール | アプリの起動中にインストーラー・アンインストーラーを実行すると終了を促される | |
| 15 | アンインストール | アプリが削除され、`%APPDATA%\inventory-manager-mini` のデータとログは残る。再インストールで既存データを読み込める | |
| 16 | 署名なしの警告 | SmartScreen の警告が表示された場合、README の案内で実行できる | |
| 17 | アイコン | `app.ico` を配置した場合、exe・インストーラー・ショートカットに反映される(未配置なら既定アイコン) | |

## 6. リスクと対策

| リスク | 影響 | 対策 |
| --- | --- | --- |
| 版数不一致のままタグを作成 | 誤った版数のインストーラーを配布 | `check_release_version.py` でタグと `pyproject.toml` を照合し、不一致なら失敗させる |
| `main` 以外のコミットのタグ | 未レビューのコードを配布 | `build` ジョブでタグのコミットが `origin/main` に含まれることを検査する |
| windows-latest の Inno Setup が未導入・版が変わる | ビルド失敗・再現性の低下 | 有無を確認し、なければ版を固定して導入する |
| 日本語の言語ファイルがない | インストーラーが英語になる | 着手時に `Japanese.isl` の有無を確認し、結果を記録する |
| 動的 import の取りこぼし・Qt プラグインの欠落 | 凍結版でのみ機能不全 | `collect_submodules` と `check_bundle.py` で同梱を検査し、配布チェックリストで実機確認する |
| UPX・署名なし exe がウイルス対策ソフトに誤検知される | 配布・実行の阻害 | UPX を使わない。README に警告時の対処と SHA256 の確認方法を記載する |
| LGPL の表記漏れ | ライセンス違反 | `THIRD_PARTY_NOTICES.txt` と dist-info を同梱し、`check_bundle.py` で存在を検査する |
| Release の誤公開・上書き | 未検証の版が公開 | 下書きで作成し、公開は手動。同名 Release がある場合は失敗させる |
| rc 検証の中のタグ・Release の後始末 | 不要な成果物の残存 | 削除は利用者の確認を得て行う |
| Phase 7 の試し刷り未了のまま正式リリース | ラベル位置ずれ | 正式タグの前提条件を 3.5 に明記し、rc 版は補正値で調整できる旨を README に記載する |

## 7. 未決事項

| # | 項目 | 期限 | 回答 |
| --- | --- | --- | --- |
| 1 | Phase 7 の実用紙試し刷りと `LabelSpec` の確定(開発計画書 11 章 #1) | 正式タグ `v0.1.0` の作成前 | 未実施 |
| 2 | Inno Setup の `AppId`(GUID)の生成と固定 | 8a の C 完了まで | 着手時に生成して本書に記録する |
| 3 | Inno Setup の版の固定(choco の版指定) | 8b の H 完了まで | 着手時点の最新 6 系を確認して記録する |
| 4 | `packaging/app.ico` の用意 | rc 検証前(未配置でもビルドは可能) | 利用者が用意する |
