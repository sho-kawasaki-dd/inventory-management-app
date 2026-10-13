from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from scripts.check_release_version import main


def _project_file(tmp_path, version: str = "1.2.3"):
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--tag", "v1.2.3"], {"version": "1.2.3", "suffix": "", "is_prerelease": False}),
        (
            ["--tag", "v1.2.3-rc12"],
            {"version": "1.2.3", "suffix": "-rc12", "is_prerelease": True},
        ),
        (
            ["--version", "1.2.3", "--suffix="],
            {"version": "1.2.3", "suffix": "", "is_prerelease": False},
        ),
        (
            ["--version", "1.2.3", "--suffix=-rc2"],
            {"version": "1.2.3", "suffix": "-rc2", "is_prerelease": True},
        ),
    ],
)
def test_accepts_release_tags_and_local_versions(tmp_path, capsys, arguments, expected) -> None:
    assert main(arguments, project_file=_project_file(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        ["--tag", "v1.2.4"],
        ["--tag", "1.2.3"],
        ["--tag", "v1.2"],
        ["--tag", "v1.2.3-rc0"],
        ["--tag", "v1.2.3-rc01"],
        ["--tag", "v1.2.3-beta1"],
        ["--tag", "v1.2.3", "--suffix="],
        ["--version", "v1.2.3"],
        ["--version", "1.2"],
        ["--version", "1.2.3", "--suffix=-rc0"],
        ["--version", "1.2.3", "--suffix=rc1"],
    ],
)
def test_rejects_invalid_or_mismatched_values(tmp_path, capsys, arguments) -> None:
    assert main(arguments, project_file=_project_file(tmp_path)) == 1
    assert capsys.readouterr().err


def test_tag_and_version_modes_are_mutually_exclusive(tmp_path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--tag", "v1.2.3", "--version", "1.2.3"], project_file=_project_file(tmp_path))

    assert error.value.code != 0


def test_cli_handles_non_utf8_stream_encoding(tmp_path) -> None:
    project_file = _project_file(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, scripts.check_release_version as crv; "
                "from pathlib import Path; "
                "sys.exit(crv.main(['--tag', 'v1.2.4'], project_file=Path(sys.argv[1])))"
            ),
            str(project_file),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        check=False,
    )
    assert result.returncode == 1
    assert "版数が一致しません" in result.stderr
