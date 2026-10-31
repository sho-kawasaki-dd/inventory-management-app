from __future__ import annotations

import argparse
import contextlib
import sys
import tomllib
from email.parser import Parser
from pathlib import Path

LICENSES_DIR = Path(__file__).resolve().parents[1] / "packaging" / "licenses"

# manifest.json の依存ソースで対応できる Qt の同梱物。これ以外は対応ソースの範囲外として拒否する
ALLOWED_QT_LIBRARIES = frozenset(
    {
        "qt6charts.dll",
        "qt6core.dll",
        "qt6gui.dll",
        "qt6opengl.dll",
        "qt6openglwidgets.dll",
        "qt6printsupport.dll",
        "qt6svg.dll",
        "qt6widgets.dll",
    }
)
ALLOWED_QT_PLUGINS = frozenset(
    {
        "iconengines/qsvgicon.dll",
        "imageformats/qgif.dll",
        "imageformats/qico.dll",
        "imageformats/qjpeg.dll",
        "imageformats/qsvg.dll",
        "platforms/qwindows.dll",
        "styles/qmodernwindowsstyle.dll",
    }
)


def _read_project_version(project_file: Path) -> str:
    try:
        project_data = tomllib.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"pyproject.toml を読み込めません: {error}") from error

    project = project_data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str):
        raise ValueError("pyproject.toml に project.version がありません")
    return version


def _required_files(version: str) -> list[Path]:
    internal = Path("_internal")
    required = [
        Path("inventory-manager-mini.exe"),
        internal / "PySide6" / "QtCharts.pyd",
        internal / "PySide6" / "Qt6Charts.dll",
        internal / "PySide6" / "QtPrintSupport.pyd",
        internal / "PySide6" / "Qt6PrintSupport.dll",
        internal / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
        internal / "inventory_manager_mini" / "db" / "schema.sql",
        internal / "inventory_manager_mini" / "db" / "schema_v3.sql",
        internal / "inventory_manager_mini" / "db" / "schema_v4.sql",
        internal / "inventory_manager_mini" / "db" / "schema_v5.sql",
        internal / f"inventory_manager_mini-{version}.dist-info" / "METADATA",
        Path("LICENSE"),
        Path("DISTRIBUTION_LICENSE.txt"),
        Path("THIRD_PARTY_NOTICES.txt"),
        Path("SOURCE_CODE.txt"),
    ]
    license_names = {path.name for path in LICENSES_DIR.glob("*.txt")}
    license_names.add("GPL-3.0.txt")
    required.extend(Path("licenses") / name for name in sorted(license_names))
    return required


def _unexpected_qt_files(bundle_dir: Path) -> list[str]:
    pyside_dir = bundle_dir / "_internal" / "PySide6"
    if not pyside_dir.is_dir():
        return []

    unexpected: list[str] = []
    for path in sorted(pyside_dir.iterdir()):
        name = path.name.lower()
        if path.is_file() and name.startswith("qt6") and name not in ALLOWED_QT_LIBRARIES:
            unexpected.append(f"対応ソース範囲外: PySide6/{path.name}")
        if (path.is_file() and name == "qtnetwork.pyd") or (
            path.is_dir() and name == "translations"
        ):
            unexpected.append(f"対応ソース範囲外: PySide6/{path.name}")

    plugins_dir = pyside_dir / "plugins"
    if plugins_dir.is_dir():
        for path in sorted(plugins_dir.rglob("*")):
            relative = path.relative_to(plugins_dir).as_posix()
            if path.is_file() and relative.lower() not in ALLOWED_QT_PLUGINS:
                unexpected.append(f"対応ソース範囲外: PySide6/plugins/{relative}")
    return unexpected


def check_bundle(bundle_dir: Path, *, project_file: Path | None = None) -> list[str]:
    version_file = project_file or Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        version = _read_project_version(version_file)
    except ValueError as error:
        return [str(error)]

    problems: list[str] = []
    for relative_path in _required_files(version):
        path = bundle_dir / relative_path
        if not path.is_file():
            problems.append(f"不足: {relative_path.as_posix()}")
            continue
        try:
            if path.stat().st_size == 0:
                problems.append(f"空ファイル: {relative_path.as_posix()}")
        except OSError:
            problems.append(f"読み取れません: {relative_path.as_posix()}")

    problems.extend(_unexpected_qt_files(bundle_dir))

    metadata_path = (
        bundle_dir / "_internal" / f"inventory_manager_mini-{version}.dist-info" / "METADATA"
    )
    if metadata_path.is_file():
        try:
            metadata_text = metadata_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            problems.append(f"METADATA を読み取れません: {error}")
        else:
            if not metadata_text:
                return problems
            metadata = Parser().parsestr(metadata_text)
            metadata_version = metadata.get("Version")
            if metadata_version != version:
                relative_metadata_path = metadata_path.relative_to(bundle_dir).as_posix()
                problems.append(
                    f"版数不一致: {relative_metadata_path} "
                    f"(期待値 {version}、実際 {metadata_version or '未設定'})"
                )
    return problems


def _configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None, *, project_file: Path | None = None) -> int:
    _configure_streams()
    parser = argparse.ArgumentParser(
        description="PyInstaller onedir の必須同梱ファイルを検査します"
    )
    parser.add_argument("bundle_dir", type=Path)
    args = parser.parse_args(argv)

    problems = check_bundle(args.bundle_dir, project_file=project_file)
    if problems:
        print("同梱検査に失敗しました:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1

    print("同梱検査に合格しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
