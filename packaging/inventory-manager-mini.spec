# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

spec_dir = Path(SPECPATH)
project_root = spec_dir.parent
icon_file = spec_dir / "app.ico"

datas = collect_data_files("inventory_manager_mini.db")
datas += copy_metadata("inventory-manager-mini")

hiddenimports = ["PySide6.QtCharts", "PySide6.QtPrintSupport"]
hiddenimports += collect_submodules("inventory_manager_mini")
# ui/dialogs 等は __init__.py のない名前空間パッケージで collect_submodules に拾われないため、ソースを走査して補う
package_root = project_root / "src"
for module_file in (package_root / "inventory_manager_mini").rglob("*.py"):
    parts = module_file.relative_to(package_root).with_suffix("").parts
    hiddenimports.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
hiddenimports = sorted(set(hiddenimports))

analysis = Analysis(
    [str(project_root / "src" / "inventory_manager_mini" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# 対応ソースの提供範囲を絞るため、アプリが使わない Qt モジュール・プラグインを同梱から外す
EXCLUDED_FILE_NAMES = {
    "qt6pdf.dll",
    "qt6qml.dll",
    "qt6qmlmeta.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6quick.dll",
    "qt6virtualkeyboard.dll",
    "qt6network.dll",
    "qtnetwork.pyd",
    "opengl32sw.dll",
    "qpdf.dll",
    "qicns.dll",
    "qtga.dll",
    "qtiff.dll",
    "qwbmp.dll",
    "qwebp.dll",
    "qdirect2d.dll",
    "qminimal.dll",
    "qoffscreen.dll",
}
EXCLUDED_DIRECTORIES = (
    "pyside6/translations/",
    "pyside6/plugins/tls/",
    "pyside6/plugins/networkinformation/",
    "pyside6/plugins/generic/",
    "pyside6/plugins/platforminputcontexts/",
)


def is_bundled(entry):
    destination = str(entry[0]).replace("\\", "/").lower()
    return destination.rsplit("/", 1)[-1] not in EXCLUDED_FILE_NAMES and not destination.startswith(EXCLUDED_DIRECTORIES)


analysis.binaries = [entry for entry in analysis.binaries if is_bundled(entry)]
analysis.datas = [entry for entry in analysis.datas if is_bundled(entry)]
pyz_archive = PYZ(analysis.pure)

executable_options = {}
if icon_file.is_file():
    executable_options["icon"] = str(icon_file)

executable = EXE(
    pyz_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="inventory-manager-mini",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    **executable_options,
)
bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="inventory-manager-mini",
)