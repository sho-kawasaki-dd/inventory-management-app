# Phase 8 実装計画書(パッケージング)

- 作成日: 2026-10-13
- ステータス: 実装中(8b)
- 作業ブランチ: `feature/phase8-packaging`(8a)→ `feature/phase8-release-workflow`(8b)
- 基盤とする文書: [ローカル在庫管理アプリ開発計画書](ローカル在庫管理アプリ開発計画書.md)(2.1・2.2・2.3・3.5・7.2・8・9・9.2・10・11 章)、[実装計画書_Phase2](実装計画書_Phase2.md)(4.2 試験ビルドの結果)、[実装計画書_Phase7](実装計画書_Phase7.md)(試し刷りの未実施項目)
- 改訂履歴: 2026-10-13 初版。発行元・ライセンス・アイコン・インストールモード・タグ規則・成果物・凍結版の検査範囲・PR 分割(2 PR)を決定
  - 2026-10-13 レビュー反映。Qt Charts を含むアプリ全体の GPLv3 配布、対応ソース提供、法的通知の最小限の UI 変更、実データ保護、外部コマンドの失敗検出、Inno Setup・CI・版数検査・更新検証の計 11 論点を修正
  - 2026-10-13 8a F 対応。README に Windows 配布・ビルド・ライセンス・対応ソース・検証データ保護の手順を追加
  - 2026-10-13 8a 残務対応。不要な Qt モジュール・プラグイン・翻訳を spec で除外して対応ソースの範囲を絞り(2.3)、依存ソースを git 管理せず取得スクリプトと SHA256 検証で確保する方式に決定。棚卸し・通知・ソース案内・失敗系検証を実施

---

## 1. 目的

- Windows 現場 PC へ配布できるインストーラー(Inno Setup)を、PyInstaller の onedir 版から再現可能な手順で作成できるようにする。
- タグ push で、検証・ビルド・成果物(インストーラー・対応ソース・SHA256)の保存・GitHub Release(下書き)の作成までを自動化する。
- Qt Charts と組み合わせたアプリ全体を GPLv3 で配布し、利用者が対応ソースを取得して変更・ビルド・インストールできる状態を維持する。
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
  - 64 ビット専用、Windows 10 以上、日本語 UI。ライセンス画面は `packaging/DISTRIBUTION_LICENSE.txt` を表示し、アプリ全体の GPLv3 配布と自作コードの MIT を区別する。GPLv3 の英語原文を省略せず含める。
  - アンインストール時に `%APPDATA%\inventory-manager-mini`(DB・バックアップ・ロック・`ui_settings.json`)と `%LOCALAPPDATA%` 配下のログを削除しない(`[UninstallDelete]` を記述しない)。
  - インストーラー側で対象 exe・DLL の使用中プロセスを Windows Restart Manager API により検知し、更新・アンインストール時に終了を促す。強制終了せず、終了確認前および検出失敗時は更新・削除を開始しない。QLockFile と Inno Setup の `AppMutex` を同一視しない。
  - 同じ `AppId`(固定の GUID)で上書き更新できる。
- 版数: アプリ版数の正本は `pyproject.toml`(初版は `0.1.0` のまま)。スキーマ版数とは独立(開発計画書 2.3)。
- タグ規則: `v<版数>` を正式リリース、`v<版数>-rc<N>` を検証用とする(`v` 必須、N は正の整数)。タグから接尾辞を除いた版数が `pyproject.toml` と一致しない場合はワークフローを失敗させる。ローカルビルドは `v` のない版数と接尾辞を別引数で検査する。
- リリースワークフロー(`.github/workflows/release.yml`):
  - 先に Windows でテスト一式(ruff・pyright・pytest・カバレッジ)を実行する。
  - タグのコミットが `origin/main` に含まれることを確認する。
  - 成果物はインストーラー exe、アプリと必要な依存部分の対応ソース、構成一覧、各配布ファイルの SHA256。workflow artifact に保存し、GitHub Release を**下書き**で作成して添付する。`-rc` タグは pre-release とする。公開は手動。
  - ジョブは独立した環境としてセットアップし、`verify` の版数・接尾辞・rc 判定を job outputs で受け渡す。Windows のシェルは `pwsh` とし、環境変数は `$env:GITHUB_REF_NAME` 等で参照する。
  - 外部コマンドの非ゼロ終了を直後に検査し、中断する。今回の全ビルド工程が成功した成果物だけを保存・添付し、過去の出力を流用しない。
  - `contents: write` はリリース作成のジョブだけに付与する。
  - ジョブ名を `test (<os>)` にしない(`main` の必須ステータスチェックと衝突させないため。ルールは `ci.yml` のジョブ名に紐づく)。
- 凍結版の自動検査は同梱ファイル検査のみとし、CI で exe は起動しない。アプリのコード変更は、既存のバージョン情報画面への法的通知と同梱ライセンスの参照機能に限定する。
- ライセンス: 自作コードは MIT(著作権者 `IKinoCore`)を維持する。Qt Charts は GPLv3 を選択し、組み合わせたアプリ全体は GPLv3 で配布する。第三者コードの元の通知・ライセンスを保持し、Qt・PySide6 を一律に LGPL と記載しない。DLL の差し替えが可能なだけでは配布条件を満たしたと扱わない。
  - 正式版・rc 版とも GPLv3 の英語原文、第三者通知、対応ソースの取得案内を同梱する。実配布構成を基準に必要なソースと条件を確定する。
  - GPLv3 第 6 条(d)に基づき、バイナリと同じ Release から追加費用なしで対応ソースを取得可能にする。アプリのソース、必要資産、依存部分の対応ソース、変更・ビルド・インストールのスクリプトと手順を含める。GitHub の自動生成ソース ZIP、wheel、上流リンクだけを完全な対応ソースとみなさない。
  - バイナリの提供中は対応ソースも提供し、rc を含む検証版を個別に渡す場合もソースの取得方法を添える。公開・削除時にバイナリとソースの対応を確認する。
- ネットワーク通信を新たに追加しない(開発計画書 7.4)。インストーラー・アプリとも自動更新は持たない。
- 次のコマンドがローカル(Windows)と CI(Windows/Linux)で成功する。
  - `uv sync --locked`
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run pyright`(`scripts/` を含む。`core/`・`db/` は strict、それ以外は standard でエラー 0 件)
  - `uv run pytest --cov`(Linux は `QT_QPA_PLATFORM=offscreen`)
  - `uv run coverage report --include="src/inventory_manager_mini/core/*,src/inventory_manager_mini/db/*" --fail-under=90`
- テストで実データの保存先(`%APPDATA%` 等)を使用せず、疑似ディレクトリは `tmp_path` に作る。
- 開発 PC の手動確認では `INVENTORY_MANAGER_MINI_DATA_DIR` に一時保存先を指定して exe を直接起動する。インストーラーからの起動は選択せず、その経路と復元後の再起動への継承確認・既定保存先の確認は実データのない Sandbox・VM 等で実施する。継承失敗の検証で実データに触れない。
- 同版数の再インストール・異ビルドへの置換と異版数への更新を別々に検証する。rc 同士で表示版数の変更を要求しない。

### 2.2 既存実装の状況(調査結果)

| 項目 | 状況 |
| --- | --- |
| 試験ビルドの spec | `build/inventory-manager-mini.spec`(`build/`・`dist/` は `.gitignore` 済み)。`collect_data_files('inventory_manager_mini.db')`、`copy_metadata('inventory-manager-mini')`、hiddenimports(QtCharts・QtPrintSupport・`item_dialog`・`master_dialog`)、`upx=True`、`console=False` |
| `packaging/`・`.iss`・アイコン・`LICENSE` | いずれも存在しない。`pyproject.toml` に license の設定もない |
| SQL の読み込み | `db/migrations.py` が `importlib.resources.files("inventory_manager_mini.db")` で `schema.sql`・`schema_v3.sql` を読む。他に非 `.py` の資源はない |
| 版数の取得 | `__init__.py` が `importlib.metadata.version("inventory-manager-mini")` で `__version__` を設定。`app.py` は `PackageNotFoundError` 時に `__version__` へフォールバック。バージョン情報は `AppContext.app_version` を表示 |
| 法的通知・起動中検出 | バージョン情報画面はアプリ版・スキーマ版・DB パスのみ表示。多重起動制御は QLockFile であり、Windows 名前付き mutex は作成していない |
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
| ライセンス・著作権者 | 自作コードは MIT、`Copyright (c) 2026 IKinoCore` を維持し、Qt Charts を含むアプリ全体は GPLv3 で配布する。`LICENSE`・GPLv3 原文・配布条件の説明・第三者通知を追加する。`pyproject.toml` の MIT 表記は自作パッケージの条件とし、依存込みの配布条件と区別する |
| 発行元 | `IKinoCore`(インストーラーの `AppPublisher`) |
| アイコン | 利用者が `packaging/app.ico` を用意する。未配置の場合は既定のアイコンでビルドする(ビルドは失敗させない)。ウィンドウアイコンの設定(`QApplication.setWindowIcon`)は行わない |
| インストールモード | ダイアログで選択。既定は全ユーザー(管理者権限、`{autopf}`)で、「自分のみ」も選択可 |
| 初版の版数 | `0.1.0`(変更しない) |
| 成果物と公開 | インストーラー exe、対応ソース、構成一覧、各ファイルの SHA256 を workflow artifact と GitHub Release(下書き)に保存する。公開は手動。バイナリとソースは同時に提供する |
| タグ規則 | `v<版数>` = 正式、`v<版数>-rc<N>` = 検証用(pre-release の下書き)。版数は `pyproject.toml` と一致必須 |
| 凍結版の検査 | 同梱ファイル検査のみ。起動確認は手動チェックリスト |
| アプリ変更の例外 | 既存のバージョン情報画面に著作権、無保証、GPLv3 での再配布、同梱ライセンスの参照方法を追加する最小限の変更のみ許可する |
| 起動中の更新・削除 | インストーラー側の Restart Manager API による検出で対応する。強制終了せず、検出失敗・実行中は処理を開始しない。アプリへの名前付き mutex 追加は行わない |
| 検証データ | 開発 PC は一時保存先を使用する。既定保存先の確認は実データのない隔離環境で実施する |
| 更新検証 | 同版数の再インストールと異版数の更新を必須とし、rc2 を作成した場合は rc1 からの異ビルド置換も確認する |
| デスクトップのショートカット | 任意タスク(既定オフ) |
| UPX | 使用しない |
| 同梱の絞り込み | アプリが使わない Qt6Pdf・Qml/Quick/QmlModels/QmlMeta/QmlWorkerScript・VirtualKeyboard・Network(QtNetwork.pyd・TLS プラグイン)・opengl32sw.dll・qtimageformats 由来と qpdf のプラグイン・platforms の qdirect2d/qminimal/qoffscreen・translations を spec の `is_bundled` で除外する(232 件・約 127.7 MiB → 111 件・約 78.5 MiB。ライセンス文書の追加前)。`scripts/check_bundle.py` が範囲外の Qt ファイルを検出して失敗させる。対応ソースは qtbase・qtsvg・qtcharts・pyside-setup・PyInstaller・hooks-contrib に絞る |
| 依存ソースの置き場 | Git 管理せず `.gitignore`(`manifest.json` だけコミット)。`scripts/fetch_dependency_sources.py` が `manifest.json` の https URL から取得し SHA256 を検証する。Release ビルドは取得済みのソースを `-sources.zip` に同梱する(8b の CI でも同スクリプトを実行する) |
| 出力ファイル名 | `inventory-manager-mini-<版数>[-rc<N>]-setup.exe`、アプリソースは同じ版数・接尾辞の `-sources.zip`、構成一覧は `-manifest.json`。必要な依存ソースも添付する。各ファイルの SHA256 は同名に `.sha256` を付ける。rc の接尾辞は `build.ps1` の `-Suffix` で渡す |

### 2.4 対象外

- コード署名(SmartScreen の警告は README で案内する)。
- Linux 向けパッケージ(README の `uv sync` 手順のみ。9.2)。
- 自動アップデート、MSI・MSIX 形式、`winget` 等への登録。
- CI での exe 起動テスト、アプリへのスモークテスト用引数の追加。
- 法的通知と同梱ライセンスの参照機能以外のアプリ本体のコード変更(ウィンドウアイコンを含む)。
- Phase 7 の実用紙試し刷りと `LabelSpec` の確定(別途実施。11 章 #1)。

## 3. タスク

### 3.1 PR 分割

| PR | ブランチ | 内容 |
| --- | --- | --- |
| 8a | `feature/phase8-packaging` | 文書・ライセンス・対応ソース提供・法的通知 UI・spec・Inno Setup・ビルドスクリプト・検査スクリプトとテスト・README・ローカルでのビルド確認 |
| 8b | `feature/phase8-release-workflow` | `release.yml`・文書更新。マージ後に rc タグで検証 |

各 PR は CI 成功後にマージし、次のブランチは最新の `main` から作成する。`main` へは直接 push しない。

### 3.2 8a. パッケージング基盤(`feature/phase8-packaging`)

#### A. ライセンス・メタデータ

- [x] `LICENSE`(MIT、`Copyright (c) 2026 IKinoCore`)を追加する
- [x] `packaging/licenses/GPL-3.0.txt` に GPLv3 の英語原文を追加する。`packaging/DISTRIBUTION_LICENSE.txt` に自作コードの MIT とアプリ全体の GPLv3 配布の関係、無保証・再配布条件を日本語で説明し、GPLv3 原文を含める
- [x] `pyproject.toml` に `license = "MIT"`・`license-files`・`authors = [{ name = "IKinoCore" }]` を追加する。自作コードの `LICENSE` と配布条件・必要なライセンス文書を `license-files` に含め、`uv lock`・`uv sync --locked` を確認する
- [x] 実配布ファイルを棚卸しする。Qt Charts、他の Qt モジュール・プラグイン、PySide6・shiboken6、Python と同梱ライブラリ、platformdirs、PyInstaller の bootloader と例外条項、アイコン等について名称・実版・著作権・ライセンス・変更有無・対応ソースの要否と入手先を記録する(結果は `packaging/THIRD_PARTY_NOTICES.txt`。アイコンは未配置のため対象外)
- [x] `packaging/THIRD_PARTY_NOTICES.txt` を棚卸し結果に基づいて作成し、各ライセンス本文を `packaging/licenses/` にそろえる。元の通知と例外条項を保持し、GPLv3 との互換性と同梱条件を確認する(`Qt-third-party-licenses.txt`・`Apache-2.0.txt`・`Python-LICENSE.txt`・`libffi-LICENSE.txt` を追加。Microsoft ランタイムの System Libraries 該当性は法務的確認が未了と明記)
- [x] `packaging/SOURCE_CODE.txt` に版別の対応ソースの取得方法を記載する。アプリのソース・SQL・必要資産・`pyproject.toml`・`uv.lock`・spec・Inno Setup・ビルドスクリプト・ビルド/インストール手順、および必要な依存部分の対応ソースを提供対象にする。System Libraries 等の除外は理由を記録する
- [x] Qt・PySide6 等の実配布バイナリに対応するソースと必要なビルド設定・変更内容を確保する。上流 URL の案内だけに依存せず、自管理の Release に必要な依存ソースを添付する。ソースと wheel の版対応を確認する(qtbase・qtsvg・qtcharts・pyside-setup 6.11.2、PyInstaller 6.22.3、hooks-contrib 2026.8 を `packaging/dependency-sources/manifest.json` に URL・SHA256 付きで記録。Qt のビルド設定は qtbase の `coin/` 等の上流公開情報に依存し、Qt・PyInstaller は改変なし)
- [x] 配布構成一覧(`-manifest.json`)にアプリ版・タグ・コミット SHA、依存の実版、各ソース成果物との対応、ビルドツールの実版を記録する。実データ・ログ・秘密情報は成果物に含めない
- [x] `ui/main_window.py` のバージョン情報画面に著作権、無保証、GPLv3 で再配布可能なこと、同梱ライセンスの参照方法を追加する。通常実行・凍結版でローカルの本文を参照できる配置を決め、`tests/test_main_window.py` に表示と参照の検証を追加する。業務処理や多重起動制御は変更しない
- [ ] 法的通知の表示とオフラインでの本文参照を確認し、ソースを取得して変更・ビルド・インストールできる手順まで検証する。バイト単位で同一のバイナリを生成することは要件にしない
  - 実施済み: 提供ソース(`-sources.zip`)を一時ディレクトリへ展開し、`uv sync --locked`・依存ソースの SHA256 検証・`build.ps1` でインストーラーまで生成できることを確認。未実施: 法的通知 UI の表示とオフライン参照(実機の手動確認)、そのインストーラーでのインストール(隔離環境)

ライセンス判断の根拠は [Qt Charts のライセンス](https://doc.qt.io/qt-6/qtcharts-index.html#licenses)、[Qt for Python のライセンス](https://doc.qt.io/qtforpython-6/licenses.html)、[GPLv3 第 1・5・6 条](https://www.gnu.org/licenses/gpl-3.0.html)を確認する。対応完了は通知ファイルの存在だけで判断せず、実配布構成・適用条件・対応ソース提供を確認して判定する。

#### B. PyInstaller spec

- [x] `packaging/inventory-manager-mini.spec` を作成する(パスは `SPECPATH` 基準)
  - [x] エントリは `src/inventory_manager_mini/__main__.py`
  - [x] `collect_data_files('inventory_manager_mini.db')` と `copy_metadata('inventory-manager-mini')`
  - [x] hiddenimports: `PySide6.QtCharts`・`PySide6.QtPrintSupport`・`collect_submodules('inventory_manager_mini')`
  - [x] `console=False`・`upx=False`・`name='inventory-manager-mini'`
  - [x] 通常実行・凍結版の法的通知 UI が参照する資源を同梱する。onedir の `_internal` 構造は維持し、exe と同じ階層に置く `LICENSE`・`DISTRIBUTION_LICENSE.txt`・`THIRD_PARTY_NOTICES.txt`・`SOURCE_CODE.txt`・`licenses/` はビルド後に D のスクリプトでコピーする
  - [x] `packaging/app.ico` が存在する場合のみ `icon` を指定する
- [x] 試験ビルドと同等の onedir 構成が `dist/staging/<ビルドID>/inventory-manager-mini/` に得られることをローカルで確認する

#### C. Inno Setup スクリプト

- [x] `packaging/inventory-manager-mini.iss` を作成する
  - [x] `AppId` の GUID を新規生成して固定する(以後変更しない。本書 7 章 #2)
  - [x] `AppName`・`AppPublisher=IKinoCore`・`AppVersion`(`/DAppVersion` で受け取る)・`OutputBaseFilename`(`/DOutputName` で受け取る)。onedir の入力先は `/DBundleDir`、出力先は `/O` で D の今回のビルド用パスを受け取る
  - [x] `PrivilegesRequired=admin`・`PrivilegesRequiredOverridesAllowed=dialog`・`DefaultDirName={autopf}\Inventory Manager mini`
  - [x] `ArchitecturesAllowed=x64compatible`・`ArchitecturesInstallIn64BitMode=x64compatible`・`MinVersion=10.0`
  - [x] `[Languages]` に `Japanese.isl`、`LicenseFile` に `packaging/DISTRIBUTION_LICENSE.txt`(Inno Setup は BOM なしの UTF-8 を ANSI と解釈するため、`.iss` と `DISTRIBUTION_LICENSE.txt` は UTF-8 BOM 付きで保存する)
  - [x] `[Files]` に `/DBundleDir` で指定された onedir の全ファイル(`recursesubdirs`、ライセンス・ソース案内を含む)
  - [x] `[Icons]` にスタートメニュー(`{autoprograms}`)と、`[Tasks]` の `desktopicon`(`Flags: unchecked`)
  - [x] `[Run]` にインストール後の起動(`postinstall skipifsilent nowait`。管理者権限のインストーラーからアプリが管理者で起動しないよう `runasoriginaluser` も付与)
  - [x] 更新前の `PrepareToInstall` と `InitializeUninstall` で、対象 exe・DLL を `RmRegisterResources` に登録して `RmGetList` で使用中のプロセスを取得する。検出失敗・実行中は処理を開始せず、終了後に再確認する。セッションは必ず解放する。アプリへの名前付き mutex 追加や `AppMutex` による代用は行わない
  - [x] 手動終了を促し、キャンセルできるようにする。サイレント実行では実行中・検出失敗を非ゼロ終了とログで通知する。`CloseApplications=no`・`RestartApplications=no` とし、独自の検出で確認してから処理する。強制終了と自動再起動は行わない
  - [x] `SetupIconFile` は `#if FileExists(...)` で `app.ico` の存在を判定し、存在する場合だけ設定する。`UninstallDisplayIcon={app}\inventory-manager-mini.exe` はアイコンの有無によらず設定する
  - [x] `[UninstallDelete]` を記述しない(ユーザーデータとログを残す)
- [x] 日本語の言語ファイルが Inno Setup 6 の標準に含まれることを確認する(含まれない場合は代替を計画書へ追記)。6.7.3 の `Languages\Japanese.isl` を確認済み
- [ ] アイコンあり・なしの両方でコンパイルする。全ユーザー・自分のみの各モードで、同じユーザー・別ユーザーによる起動中の更新/削除、終了後の再試行、検出失敗、サイレント実行を隔離環境で確認する。検知できない条件があれば完了扱いにせず方式を再検討する
  - 実施済み(開発 PC・一時ディレクトリ・`ping.exe` の複製を疑似 exe に使用): アイコンあり・なし・必須定義欠落のコンパイル、自分のみ(同じユーザー)でのサイレント更新(終了コード 7)・アンインストール(終了コード 1)の拒否と、終了後の更新・削除の成功、`RmGetList` による PID・名称の取得
  - 未実施(隔離環境で実施): 全ユーザーモード、別ユーザーの起動中プロセスの検出可否、対話時の再試行・キャンセル、検出失敗(Restart Manager のエラー)

#### D. ビルドスクリプト

- [x] `packaging/build.ps1` を作成する(引数: `-Version`、`-Suffix`(既定は空)、`-IsccPath`(既定は標準のインストール先))
  - [x] `uv run python scripts/check_release_version.py --version <版数> --suffix=<接尾辞>` で入力形式と `pyproject.toml` との一致を検査する。空接尾辞も明示して扱う
  - [x] `-IsccPath` が指す ISCC の実版を本書 7 章 #3 の固定版と照合し、不一致なら失敗させる。Python・PyInstaller・Qt・uv・ISCC の使用パスと実版を記録する
  - [x] 呼び出しごとに新しいビルドIDを生成し、`dist/staging/<ビルドID>/` に隔離して出力する。PyInstaller の `--distpath` と `--workpath` を今回用に指定し、既存の成果物を入力にしない
  - [x] `uv run pyinstaller --noconfirm --clean --distpath <今回のステージング先> --workpath build/<ビルドID> packaging/inventory-manager-mini.spec`
  - [x] A のライセンス・通知・ソース案内を onedir の exe と同じ階層にコピーし、`uv run python scripts/check_bundle.py <今回のonedir>` で同梱検査する
  - [x] `ISCC.exe /DAppVersion=<版数> /DOutputName=inventory-manager-mini-<版数><接尾辞>-setup /DBundleDir=<今回のonedir> /O<今回のステージング先> packaging/inventory-manager-mini.iss`
  - [x] 今回のアプリソースを `-sources.zip` として作成し、必要な依存ソースと構成一覧をそろえる。依存ソースは `packaging/dependency-sources/manifest.json` (`schema_version=1`、`audit_status=complete`、`sources` の `component`・`version`・`path`・`sha256`) で列挙し、パス・版・ハッシュを検査する。Release ビルドではタグのコミットとソースが一致することを確認し、未コミットの変更を混入させない。ローカル検証版は検証用と明記する(Release 経路の git 検査は 8b の rc タグで検証する。依存ソースの実体は取得スクリプト `scripts/fetch_dependency_sources.py` で用意する)
  - [x] 各配布ファイルの SHA256 を `<成果物>.sha256`(`ハッシュ  ファイル名` 形式)として出力し、構成一覧とソースの対応・必須ファイル・空ファイル・秘密情報の非混入を確認する
  - [x] `$ErrorActionPreference = 'Stop'` に加え、すべての外部コマンド(uv・ISCC・git 等)の直後に `$LASTEXITCODE` を確認して非ゼロなら中断する。後続ビルド・ハッシュ作成・成果物確定を実行しない
  - [x] 全工程成功後だけ `dist/releases/<ビルドID>/` に配布対象を確定し、成功した出力先を呼び出し元へ返す。失敗時のステージング先と過去の配布先はアップロード対象にしない
- [x] 成果物の出力先を `.gitignore` 済みのディレクトリ(`dist/`)に限定し、コミット対象に含めない
- [x] 正常終了と、版数検査・PyInstaller・同梱検査・ISCC・ソース準備の各失敗を検証する。失敗後に後続処理や成果物確定が走らず、古い成功成果物が残っていても配布対象にならないことを確認する(提供ソースを展開した一時コピーで実施。結果は 4 章)

#### E. 検査スクリプトとテスト

- [x] `scripts/check_release_version.py`: `--tag` と `--version` を排他的な入力モードとする。タグは `^v([0-9]+\.[0-9]+\.[0-9]+)(-rc[1-9][0-9]*)?$`、ローカル版数は `^[0-9]+\.[0-9]+\.[0-9]+$`、`--suffix` は空または `^-rc[1-9][0-9]*$` とする。接尾辞はローカルモードだけで指定し、`tomllib` で読んだ `project.version` と基本版数を照合する
- [x] 版数検査の成功時は標準出力に JSON(`version`・`suffix`・真偽値の `is_prerelease`)を返し、失敗時は標準エラーに理由を表示して非ゼロ終了する
- [ ] CI は JSON を解析し、`$env:GITHUB_OUTPUT` に UTF-8 で step outputs を書く。真偽値は `true`/`false` の文字列にし、式では文字列比較で判定する(8b H)
- [x] `scripts/check_bundle.py`: 引数の onedir 出力に対し、次のファイルが存在し空でないことを検査する。欠落を全件列挙して非 0 で終了する
  - [x] `inventory-manager-mini.exe`
  - [x] `PySide6/QtCharts.pyd`・`PySide6/Qt6Charts.dll`
  - [x] `PySide6/QtPrintSupport.pyd`・`PySide6/Qt6PrintSupport.dll`
  - [x] `PySide6/plugins/platforms/qwindows.dll`
  - [x] `inventory_manager_mini/db/schema.sql`・`schema_v3.sql`
  - [x] `inventory_manager_mini-<版数>.dist-info/METADATA`(版数が `pyproject.toml` と一致。実ビルドの正規化名)
  - [x] exe と同じ階層の `LICENSE`・`DISTRIBUTION_LICENSE.txt`・`THIRD_PARTY_NOTICES.txt`・`SOURCE_CODE.txt`・`licenses/GPL-3.0.txt` と `packaging/licenses/` の各本文、および UI が参照する資源
  - [x] ライブラリ・SQL・dist-info は `_internal`、利用者向け文書は exe と同じ階層を基準に検査する
- [x] 実ビルドで配置を確定し、必要なら検査パスを調整する(G)
- [x] `tests/test_check_release_version.py`: 正式/rc タグ、`v` なしのローカル版数と空/rc 接尾辞の合格、版数不一致・タグの `v` なし・ローカル版数の `v` あり・桁不足・接尾辞違い・rc0・入力モード競合の不合格、および JSON 出力を検証する(`tmp_path` の `pyproject.toml` を使う)
- [x] `tests/test_check_bundle.py`: `tmp_path` に疑似 onedir を作り、全件そろう場合の合格、1 件ずつ欠落・空ファイル・版数不一致の dist-info での不合格、通知/本文の欠落、および欠落の全件列挙を検証する
- [x] `scripts/` のモジュールをテストから読み込む方法は既存の `tests/test_dummy_data.py` に合わせる
- [x] 依存ルールのテスト(`tests/test_dependency_rules.py`)に影響しないこと(`scripts/` は `src/` から参照されない)を確認する

#### F. README・文書

- [x] `README.md` に次を追記する
  - [x] Windows のインストール手順(インストールモードの選択、署名なしのため SmartScreen が警告する場合は「詳細情報」から実行すること、SHA256 の確認方法)
  - [x] アンインストール後もデータ(`%APPDATA%\inventory-manager-mini`)とログが残ること、完全に消す場合の手動削除手順
  - [x] ローカルでのビルド手順(固定版 Inno Setup 6 の導入と照合、`packaging/build.ps1` の使い方、今回の出力先と異常終了の扱い)
  - [x] アイコン `packaging/app.ico` の配置先と、未配置時は既定アイコンになること
  - [x] 自作コードの MIT、Qt Charts と結合したアプリ全体の GPLv3、第三者ライセンスの区別、無保証・再配布条件、同梱本文の参照方法
  - [x] Release ごとの対応ソース・構成一覧の取得方法、依存を含む変更/ビルド/インストール手順、バイナリ提供中のソース保持方針。実データは公開対象ではないこと
  - [x] 開発 PC での一時保存先の指定と exe の直接起動、インストーラーの起動チェックを外す手順。インストーラー・復元後の起動への継承確認と既定保存先の検証を隔離環境で行う手順
- [x] 本書(Phase 8)のステータスを更新する

#### G. ローカルでの確認

- [x] `packaging/build.ps1 -Version 0.1.0` が成功し、`check_bundle.py` が合格する(実ビルドで dist-info 名が `inventory_manager_mini-<版数>.dist-info` と判明したため検査パスを修正、構成一覧の uv 版の抽出を修正)
- [ ] 開発 PC ではインストーラーの起動チェックを外し、`INVENTORY_MANAGER_MINI_DATA_DIR` に一時ディレクトリを指定した同じシェルから exe を直接起動する。起動前に環境変数を確認し、起動後にバージョン情報の DB パスを照合する。インストーラーからの起動と復元後の再起動の検証は開発 PC で行わない
  - 実施済み: 一時保存先を指定して exe を直接起動し、MainWindow の表示と一時保存先への `inventory.db`・`logs`・`backups` の作成を確認。未実施: バージョン情報の DB パス照合(手動)
- [ ] 実データのない隔離環境で、一時保存先を指定したシェルからインストーラーを実行し、インストール後の起動・復元後の再起動に一時保存先が継承されることを DB パスで確認する。継承失敗時は原因を記録し、起動経路を修正・再検証する
- [ ] 保存先上書きなしのインストールと初回起動は、実データのない Sandbox・VM 等でのみ確認する。起動前に検証環境と保存先を確認し、DB マイグレーションによる実データ変更を防ぐ
- [ ] Phase 2 で未確認だった「品目登録の DB 保存」と「バージョン情報の表示内容(アプリ版・スキーマ版・DB パス)」を凍結版で確認する
- [ ] ソース成果物と構成一覧を検査し、法的通知 UI・同梱本文・ソース取得案内を確認する。提供ソースから隔離した開発環境でビルドし、インストールできることを記録する
- [ ] 隔離したソースコピーの `pyproject.toml` を更新検証専用の `0.0.0` にし、メタデータと lock を整合させて旧版をビルドする。同じ固定 `AppId`・インストールモードで `0.0.0` から `0.1.0` へ更新し、データ・設定と版数表示を確認する。検証用旧版は公開せず、本番の `pyproject.toml`・公開タグは変更しない
- [ ] `0.1.0` の同版数再インストールも確認する。rc 同士の異ビルド置換は rc2 を作成した場合に構成一覧・変更された同梱ファイルのハッシュで確認する
- [ ] 検証結果(ビルド環境・成果物サイズ・問題点と対処)を本書 4 章に記録する

### 3.3 8b. リリースワークフロー(`feature/phase8-release-workflow`)

#### H. `.github/workflows/release.yml`

- [x] トリガー: `push: tags: ['v*']`。ワークフロー全体の `permissions` は `contents: read`、`concurrency` はタグごと
- [x] Windows の `run` は `shell: pwsh` に統一する。外部コマンドの `$LASTEXITCODE` を直後に検査し、版数・main 包含・ビルド・成果物取得・Release 操作の失敗で後続処理を止める
- [x] `verify` ジョブ(`windows-latest`。ジョブ名は `verify`)
  - [x] `actions/checkout`、`astral-sh/setup-uv`(`ci.yml` と同じ版・Python 3.13)
  - [x] `uv sync --locked`、`ruff check`、`ruff format --check`、`pyright`、`pytest --cov`、カバレッジ検査(`ci.yml` と同じコマンド)
  - [x] `uv run python scripts/check_release_version.py --tag "$env:GITHUB_REF_NAME"` でタグと版数を検査する。JSON を解析して `version`・`suffix`・`is_prerelease` を `$env:GITHUB_OUTPUT` に書き、step outputs から同名の job outputs を公開する
- [x] `build` ジョブ(`needs: verify`、`windows-latest`)
  - [x] `actions/checkout` を `fetch-depth: 0` で実行し、`git merge-base --is-ancestor "$env:GITHUB_SHA" origin/main` でタグのコミットが `main` に含まれることを検査する(含まれなければ失敗)
  - [x] `astral-sh/setup-uv` で uv・Python 3.13 を設定し、`uv sync --locked` を実行する。`verify` の環境は引き継がれない前提とする
  - [x] 固定版 Inno Setup 6 を専用の導入先へ取得し、その ISCC のパスと実版を照合する(固定版は本書 7 章 #3)。プリインストール済みの別版を暗黙に使わず、不一致なら失敗させる。導入先・取得方法・実版を記録する
  - [x] `needs.verify.outputs.version`・`suffix` を環境変数経由で `packaging/build.ps1 -Version <版数> -Suffix <接尾辞> -IsccPath <固定版のパス>` に渡す。空接尾辞も欠落させない
  - [x] ビルド成功時だけ今回の確定出力先を step output に記録し、`actions/upload-artifact` にその配布対象ファイルだけを渡す。インストーラー・アプリ/依存の対応ソース・構成一覧・各 SHA256 の欠落時は失敗させる(`if-no-files-found: error`、通常の成功条件で実行)
  - [x] artifact 名を job output として公開する。artifact は工程間の受け渡し用とし、保持期限のある artifact だけを継続的なソース提供先にしない
- [x] `release` ジョブ(`needs: [verify, build]`、`permissions: contents: write`)
  - [x] `needs.build.outputs` の artifact 名で `actions/download-artifact` を実行し、必要な全成果物と各 SHA256 の一致を確認する
  - [x] `GH_TOKEN: ${{ github.token }}`・`GH_REPO: ${{ github.repository }}` を設定し、checkout の有無に依存せず対象リポジトリを確定する
  - [x] `gh release create "$env:GITHUB_REF_NAME" --verify-tag --draft [--prerelease] --generate-notes` で下書きを作成し、全成果物を添付する。`needs.verify.outputs.is_prerelease == 'true'` の場合だけ `--prerelease` を付け、タグの自動作成を許可しない
  - [x] Release 本文にアプリ全体の GPLv3 配布と、版別の対応ソース・依存ソース・構成一覧・ビルド手順の取得方法を明記する
  - [x] 同名の Release が既にある場合は失敗させる(上書きしない)
- [x] ワークフロー内で使うアクションの版は `ci.yml` に合わせ、シークレットを追加しない
- [x] `release.yml` の構文を VS Code の YAML 診断で確認する(`actionlint` は未導入)

#### I. 文書更新

- [x] 開発計画書 2.2 手順 5・9.2・11 章 #3 に、発行元(`IKinoCore`)、自作コードの MIT とアプリ全体の GPLv3、法的通知 UI の限定変更、アイコン、インストールモード、タグ規則、対応ソースを含む成果物(下書き Release)を反映する
- [x] 開発計画書 8 章の配布チェックリストに、検証データの隔離、同版数/異版数の更新、起動中の検出、法的通知と対応ソース提供の検証条件を反映する
- [x] 開発計画書 3.6 のディレクトリ構成に `LICENSE` と `packaging/` のライセンス・ソース案内等の実ファイルを反映する
- [x] `.github/copilot-instructions.md` に、ビルドコマンド、固定ツール版、タグ規則、対応ソースを同時提供する手順、データ保護、`main` を直接 push しない運用との関係を追記する
- [x] 本書のステータスを更新する

### 3.4 リリース検証(8b マージ後)

- [x] `v0.1.0-rc1` を `main` のコミットに作成して push する(push 前に利用者へ確認する)。確認済み: `77d45aacb74322609b1d61288989413654845a64` に注釈付きタグを作成し、origin へ push
- [ ] `verify`・`build`・`release` が成功し、下書きの pre-release にインストーラー・対応ソース・構成一覧・各 `.sha256` が添付される。版数・接尾辞・rc 判定と構成一覧のコミット SHA がタグに対応することを確認する
- [ ] ダウンロードした各成果物の SHA256 が添付ファイルと一致する。対応ソースの内容・依存の版対応・ビルド手順を確認する。rc を検証者へ渡す際も対応ソースの取得方法を添える
- [ ] 版数不一致のタグ(例: `v0.1.1-rc1`)がワークフローで失敗することを確認する(確認後にタグ・下書きを削除。削除は利用者の確認を得て行う)
- [ ] G の同版数再インストールと検証専用旧版からの異版数更新を、今回の rc インストーラーでも実施する
- [ ] rc1 の失敗を修正して `main` へ取り込み、`v0.1.0-rc2` を作成して上書き更新(rc1 → rc2)を確認する。アプリ表示版数はどちらも `0.1.0` のままとし、構成一覧と変更された同梱ファイルのハッシュで置換を確認する。初回 run は `Install pinned Inno Setup` で失敗し、固定版の旧ダウンロード URL が 404 だった。workflow の取得先を公式 GitHub Release asset に修正し、feature branch に反映済み。修正の main 取り込み後に rc2 で再検証する
- [ ] 配布チェックリスト(5 章)を実施し、結果を本書に記録する

### 3.5 正式リリース

- [ ] Phase 7 の実用紙試し刷り(実装計画書_Phase7 の手動確認 #1・#6・#7)が完了し、`LabelSpec` と開発計画書 9.1・11 章 #1 が確定している
- [ ] 配布チェックリストがすべて合格している
- [ ] `v0.1.0` を作成して push する(push 前に利用者へ確認する)。ワークフローが成功し、正式版の下書きが作成される
- [ ] 正式版のインストーラー・対応ソース・構成一覧・各 SHA256 がそろい、ライセンス通知とソース取得/ビルド手順が検証済みである。公開後も対応ソースを保持する運用を確認する
- [ ] 正式版の下書きを確認して手動で公開する。バイナリと対応ソースを同時に提供する
- [ ] 本書のステータスを「実装完了」に更新し、開発計画書 9 章の記載を整合させる

## 4. 検証結果の記録(実施時に記入)

| 項目 | 結果 | 備考 |
| --- | --- | --- |
| ビルド環境(PyInstaller・Python・Qt・Inno Setup の版) | 確認済み(ローカル検証版) | PyInstaller 6.22.3、Python 3.13.12、Qt/PySide6 6.11.2、Inno Setup 6.7.3、uv 0.11.32 |
| `check_bundle.py` の合格 | 確認済み | `build.ps1 -Version 0.1.0` の工程内で合格 |
| インストーラーのサイズ | 確認済み | 約 34.6 MB(`inventory-manager-mini-0.1.0-setup.exe`)。Qt 除外後は約 22.4 MB |
| 凍結版での品目登録・バージョン情報 | 要再確認 | 初回は `ModuleNotFoundError: inventory_manager_mini.ui.dialogs.item_dialog`。`ui/dialogs` 等が `__init__.py` のない名前空間パッケージで `collect_submodules` に拾われないため。spec でソースを走査して全モジュールを hiddenimports に追加して修正。修正版での品目登録・バージョン情報は手動で確認する。Qt 除外後の exe でも一時保存先を指定して起動し、MainWindow 表示と DB 作成を確認した(ダッシュボード・印刷プレビュー・PDF 出力は手動確認が必要) |
| Qt 除外後の依存 DLL 解決 | 確認済み | 残存する全 DLL/PYD/EXE のインポート表を pefile で検査し、同梱または System32 で解決できない DLL が 0 件であることを確認 |
| 法的通知・ライセンス本文・対応ソースと依存の版対応 | 一部確認済み | 棚卸し・通知・本文・対応ソース(6 件、URL・SHA256 照合済み、Qt 公式の `.sha256` と一致)を整備。未実施: バージョン情報 UI での表示とオフライン参照の実機確認、Microsoft ランタイムの System Libraries 扱いの法務確認 |
| 提供ソースからのビルド・インストール | ビルドまで確認済み | `-sources.zip` を一時ディレクトリに展開し、`uv sync --locked`・依存ソース検証・`build.ps1` でインストーラーまで生成(git なしでも検証ビルド可。展開先ではコミット SHA を `unknown` と記録)。インストールは隔離環境で未実施。ソース ZIP は約 77 MB、インストーラーは約 22 MB(Qt 除外後) |
| 検証環境と一時保存先の継承・実データの非変更 | 未実施 | |
| 外部コマンド失敗時の中断・古い成果物の非配布 | 確認済み | 提供ソース展開先の一時コピーで、版数不一致・spec を失敗させる PyInstaller・許可外の Qt DLL による同梱検査・[Files] に存在しないファイルを指定した ISCC・依存ソースの SHA256 不一致の各ケースが非ゼロ終了し、`dist/releases/` に新しいディレクトが作られなかった(ISCC に未知セクションを追記するだけでは失敗せず、成果物が確定されたため別ケースに変更)。正常終了時の出力パスのみが確定先となる |
| 起動中の更新/削除(各モード・別ユーザー・サイレント・検出失敗) | 未実施 | |
| 同版数再インストール・異版数更新・rc 異ビルド置換 | 未実施 | rc2 未作成時は異ビルド置換だけ対象外 |
| rc タグでのワークフロー結果 | rc1 失敗 | run `38323979508`(`v0.1.0-rc1`、commit `77d45aacb74322609b1d61288989413654845a64`): `verify` 成功、`build` は `Install pinned Inno Setup` で失敗、`release` はスキップ。CI の旧 URL は 404。公式 Release asset URL は HTTP 200 で取得でき、SHA256 が公式値 `9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732` と一致。workflow 修正後、main へ取り込み rc2 で再実行が必要。Actions のログ詳細は未認証のため取得できず |

## 5. 配布チェックリスト(手動)

実データのない隔離された Windows 環境(Windows Sandbox・新規の仮想マシン等)で、rc 版のインストーラーを使って実施する。既定保存先を使用し、開発 PC では行わない。Python 未導入であるだけでは実データの隔離を保証しない。起動前に環境と保存先を確認する。rc2 未作成時の異ビルド置換以外は、未実施を合格扱いにしない。

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
| 13 | 上書き更新 | 同版数再インストールと検証専用 `0.0.0` → `0.1.0` の異版数更新でデータ・設定を保持し、後者だけ表示版数の変更を確認する。rc2 を作成した場合は rc1 → rc2 の構成一覧と変更された同梱ファイルのハッシュで置換を確認する(rc 同士の表示版数は同じ)。両インストールモードで確認する | |
| 14 | 起動中の更新・アンインストール | 同じ/別ユーザーでアプリを起動し、各インストールモードで終了を促されること、終了・再確認前に更新/削除しないこと、キャンセル・検出失敗時も元の状態を保つことを確認する。サイレント実行では非ゼロ終了とログを確認する。強制終了はしない | |
| 15 | アンインストール | アプリが削除され、`%APPDATA%\inventory-manager-mini` のデータとログは残る。再インストールで既存データを読み込める | |
| 16 | 署名なしの警告 | SmartScreen の警告が表示された場合、README の案内で実行できる | |
| 17 | アイコン | `app.ico` を配置した場合、exe・インストーラー・ショートカットに反映される(未配置なら既定アイコン) | |
| 18 | 法的通知・ライセンス | インストーラーとバージョン情報に適切な配布条件・著作権・無保証・再配布と本文の参照方法が表示され、オフラインで同梱 GPLv3 原文と必要な第三者本文を参照できる | |
| 19 | 対応ソースの提供 | Release でバイナリと対応ソース・構成一覧を取得でき、タグ/コミット・アプリ/依存の版・必要資産・ビルド/インストール手順が対応する。各 SHA256 を照合し、別の開発環境で提供ソースからビルド・インストールできる | |

## 6. リスクと対策

| リスク | 影響 | 対策 |
| --- | --- | --- |
| 版数不一致のままタグを作成 | 誤った版数のインストーラーを配布 | `check_release_version.py` でタグと `pyproject.toml` を照合し、不一致なら失敗させる |
| `main` 以外のコミットのタグ | 未レビューのコードを配布 | `build` ジョブでタグのコミットが `origin/main` に含まれることを検査する |
| windows-latest の Inno Setup が未導入・版が変わる | ビルド失敗・再現性の低下 | 固定版を専用の導入先へ取得し、使用する ISCC のパスと実版を必ず照合する。不一致は失敗させる |
| 外部コマンド失敗を見落とす・古い成果物を添付 | 不完全/誤版の配布 | `$LASTEXITCODE` の直後検査、ビルドごとのステージング、全工程成功後だけ成果物確定とアップロード |
| 開発 PC の既定保存先で起動・環境変数の継承失敗 | 実データのマイグレーション・変更 | 開発 PC は一時保存先を指定して exe を直接起動する。インストーラーからの起動・復元後の再起動の継承と既定保存先の確認は、実データのない隔離環境に限定する |
| 起動中の検出失敗・更新/削除直前の再起動 | 使用中ファイルの更新・削除 | 対象パスを Restart Manager に登録して処理直前にも確認する。検出失敗時は中断し、検証中は終了後の再起動を避ける。アプリ変更なしで完全な起動排他を保証するとは記載しない |
| 日本語の言語ファイルがない | インストーラーが英語になる | 着手時に `Japanese.isl` の有無を確認し、結果を記録する |
| 動的 import の取りこぼし・Qt プラグインの欠落 | 凍結版でのみ機能不全 | `collect_submodules` と `check_bundle.py` で同梱を検査し、配布チェックリストで実機確認する |
| UPX・署名なし exe がウイルス対策ソフトに誤検知される | 配布・実行の阻害 | UPX を使わない。README に警告時の対処と SHA256 の確認方法を記載する |
| Qt Charts の GPLv3 条件・第三者条件の対応漏れ | ライセンス違反 | 実配布構成の棚卸し、法的通知・本文の同梱、版対応するソースと手順の同時提供・保持、提供ソースからのビルドを確認する |
| artifact の期限切れ・上流ソース消失 | 対応ソースを取得できない | artifact だけに依存せず Release に必要な対応ソースを添付し、バイナリ提供中は保持する |
| ジョブ間の環境/出力を暗黙に共有 | ビルド・rc 判定・Release 作成の失敗 | ジョブごとのセットアップ、step/job outputs と needs、PowerShell の環境変数、GH_REPO と既存タグ指定を明記する |
| Release の誤公開・上書き | 未検証の版が公開 | 下書きで作成し、公開は手動。同名 Release がある場合は失敗させる |
| rc 検証の中のタグ・Release の後始末 | 不要な成果物の残存 | 削除は利用者の確認を得て行う |
| Phase 7 の試し刷り未了のまま正式リリース | ラベル位置ずれ | 正式タグの前提条件を 3.5 に明記し、rc 版は補正値で調整できる旨を README に記載する |

## 7. 未決事項

| # | 項目 | 期限 | 回答 |
| --- | --- | --- | --- |
| 1 | Phase 7 の実用紙試し刷りと `LabelSpec` の確定(開発計画書 11 章 #1) | 正式タグ `v0.1.0` の作成前 | 未実施 |
| 2 | Inno Setup の `AppId`(GUID)の生成と固定 | 8a の C 完了まで | `{8B4D1CC3-78CB-452A-8D8F-2D81019811FC}`(`packaging/inventory-manager-mini.iss`。以後変更しない) |
| 3 | Inno Setup の実版固定と CI の専用導入先・取得方法 | 8a の D 検証前 | 6.7.3 に固定する。ローカル/CI とも実際に使用する ISCC を照合する |
| 4 | `packaging/app.ico` の用意 | rc 検証前(未配置でもビルドは可能) | 利用者が用意する |
| 5 | 実配布構成と必要な依存の対応ソース・ライセンス条件の確定 | 8a の A・D 完了まで | 実ビルドの棚卸しで確定し、構成一覧・通知・ソース成果物へ記録する。必要なソースを確保できない場合は配布しない |
| 6 | 法的通知 UI の本文参照用資源と通常実行/凍結版の配置 | 8a の A・B 完了まで | 最小限の UI 変更で確定する。業務処理・多重起動制御・ウィンドウアイコンは対象外のまま |
