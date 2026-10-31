from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_bundle as bundle_checker
from scripts.check_bundle import check_bundle, main

VERSION = "1.2.3"
FIXED_FILES = [
    "inventory-manager-mini.exe",
    "_internal/PySide6/QtCharts.pyd",
    "_internal/PySide6/Qt6Charts.dll",
    "_internal/PySide6/QtPrintSupport.pyd",
    "_internal/PySide6/Qt6PrintSupport.dll",
    "_internal/PySide6/plugins/platforms/qwindows.dll",
    "_internal/inventory_manager_mini/db/schema.sql",
    "_internal/inventory_manager_mini/db/schema_v3.sql",
    "_internal/inventory_manager_mini/db/schema_v4.sql",
    "_internal/inventory_manager_mini/db/schema_v5.sql",
    f"_internal/inventory_manager_mini-{VERSION}.dist-info/METADATA",
    "LICENSE",
    "DISTRIBUTION_LICENSE.txt",
    "THIRD_PARTY_NOTICES.txt",
    "SOURCE_CODE.txt",
    *[
        f"licenses/{name}"
        for name in sorted(
            {path.name for path in bundle_checker.LICENSES_DIR.glob("*.txt")} | {"GPL-3.0.txt"}
        )
    ],
]


def _project_file(tmp_path: Path) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nversion = "{VERSION}"\n', encoding="utf-8")
    return path


def _make_bundle(bundle_dir: Path) -> None:
    for relative_path in FIXED_FILES:
        path = bundle_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = (
            f"Metadata-Version: 2.1\nName: inventory-manager-mini\nVersion: {VERSION}\n"
            if relative_path.endswith("METADATA")
            else "required bundle content\n"
        )
        path.write_text(contents, encoding="utf-8")


def test_accepts_complete_bundle(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)

    assert check_bundle(bundle_dir, project_file=_project_file(tmp_path)) == []


@pytest.mark.parametrize("relative_path", FIXED_FILES)
def test_reports_each_missing_file(tmp_path: Path, relative_path: str) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)
    (bundle_dir / relative_path).unlink()

    problems = check_bundle(bundle_dir, project_file=_project_file(tmp_path))

    assert problems == [f"不足: {relative_path}"]


@pytest.mark.parametrize("relative_path", FIXED_FILES)
def test_reports_each_empty_file(tmp_path: Path, relative_path: str) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)
    (bundle_dir / relative_path).write_bytes(b"")

    problems = check_bundle(bundle_dir, project_file=_project_file(tmp_path))

    assert problems == [f"空ファイル: {relative_path}"]


def test_rejects_metadata_version_mismatch(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)
    metadata_relative_path = next(path for path in FIXED_FILES if path.endswith("/METADATA"))
    metadata_path = bundle_dir / metadata_relative_path
    metadata_path.write_text(
        "Metadata-Version: 2.1\nName: inventory-manager-mini\nVersion: 9.9.9\n",
        encoding="utf-8",
    )

    problems = check_bundle(bundle_dir, project_file=_project_file(tmp_path))

    assert len(problems) == 1
    assert "版数不一致" in problems[0]


@pytest.mark.parametrize(
    "relative_path",
    [
        "_internal/PySide6/Qt6Pdf.dll",
        "_internal/PySide6/Qt6Quick.dll",
        "_internal/PySide6/QtNetwork.pyd",
        "_internal/PySide6/translations/qt_ja.qm",
        "_internal/PySide6/plugins/imageformats/qpdf.dll",
        "_internal/PySide6/plugins/tls/qopensslbackend.dll",
    ],
)
def test_rejects_qt_files_outside_dependency_source_scope(
    tmp_path: Path, relative_path: str
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)
    extra_path = bundle_dir / relative_path
    extra_path.parent.mkdir(parents=True, exist_ok=True)
    extra_path.write_text("unexpected\n", encoding="utf-8")

    problems = check_bundle(bundle_dir, project_file=_project_file(tmp_path))

    assert len(problems) == 1
    assert "対応ソース範囲外" in problems[0]


def test_requires_every_license_document_in_packaging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    license_dir = tmp_path / "source-licenses"
    license_dir.mkdir()
    (license_dir / "EXTRA-LICENSE.txt").write_text("license terms\n", encoding="utf-8")
    monkeypatch.setattr(bundle_checker, "LICENSES_DIR", license_dir)
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)

    problems = check_bundle(bundle_dir, project_file=_project_file(tmp_path))

    assert problems == ["不足: licenses/EXTRA-LICENSE.txt"]


def test_lists_all_missing_files_and_cli_fails(tmp_path: Path, capsys) -> None:
    bundle_dir = tmp_path / "empty-bundle"
    bundle_dir.mkdir()
    project_file = _project_file(tmp_path)

    problems = check_bundle(bundle_dir, project_file=project_file)

    assert len(problems) == len(FIXED_FILES)
    assert all(relative_path in "\n".join(problems) for relative_path in FIXED_FILES)
    assert main([str(bundle_dir)], project_file=project_file) == 1
    assert len(capsys.readouterr().err.splitlines()) == len(FIXED_FILES) + 1


def test_cli_accepts_complete_bundle(tmp_path: Path, capsys) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)

    assert main([str(bundle_dir)], project_file=_project_file(tmp_path)) == 0
    assert "合格" in capsys.readouterr().out


def test_cli_handles_non_utf8_stream_encoding(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    _make_bundle(bundle_dir)
    project_file = _project_file(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, scripts.check_bundle as cb; "
                "from pathlib import Path; "
                "sys.exit(cb.main([sys.argv[1]], project_file=Path(sys.argv[2])))"
            ),
            str(bundle_dir),
            str(project_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        check=False,
    )
    assert result.returncode == 0
    assert "同梱検査に合格しました" in result.stdout


def test_cli_failure_reports_in_non_utf8_stream_encoding(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "empty-bundle"
    bundle_dir.mkdir()
    project_file = _project_file(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, scripts.check_bundle as cb; "
                "from pathlib import Path; "
                "sys.exit(cb.main([sys.argv[1]], project_file=Path(sys.argv[2])))"
            ),
            str(bundle_dir),
            str(project_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        check=False,
    )
    assert result.returncode == 1
    assert "同梱検査に失敗しました" in result.stderr
