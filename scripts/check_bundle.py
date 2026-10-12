from __future__ import annotations

import argparse
import sys
import tomllib
from email.parser import Parser
from pathlib import Path

LICENSES_DIR = Path(__file__).resolve().parents[1] / "packaging" / "licenses"


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


def main(argv: list[str] | None = None, *, project_file: Path | None = None) -> int:
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
