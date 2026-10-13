from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
POWERSHELL = shutil.which("pwsh")


def test_release_workflow_fetches_dependency_sources_before_build() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    fetch_step = workflow.index("- name: Fetch dependency sources")
    build_step = workflow.index("- name: Build installer and source artifacts")

    assert fetch_step < build_step
    fetch_block = workflow[fetch_step:build_step]
    assert "uv run python scripts/fetch_dependency_sources.py" in fetch_block
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in fetch_block


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell が必要です")
@pytest.mark.parametrize(
    ("registrations", "compiler_exists", "succeeds"),
    [
        ([{"location": "matching", "version": "6.7.3"}], True, True),
        ([{"location": "matching", "version": "6.7.30"}], True, False),
        ([{"location": "matching", "version": "6.6.1"}], True, False),
        ([{"location": "matching", "version": ""}], True, False),
        ([{"location": "other", "version": "6.7.3"}], True, False),
        (
            [
                {"location": "other", "version": "6.6.1"},
                {"location": "matching", "version": "6.7.3"},
            ],
            True,
            True,
        ),
        ([], True, False),
        (
            [
                {"location": "matching", "version": "6.7.3"},
                {"location": "matching", "version": "6.6.1"},
            ],
            True,
            False,
        ),
        ([{"location": "matching", "version": "6.7.3"}], False, False),
    ],
)
def test_pinned_inno_setup_validation(
    tmp_path, monkeypatch, registrations, compiler_exists, succeeds
) -> None:
    install_directory = tmp_path / "innosetup-6.7.3"
    install_directory.mkdir()
    if compiler_exists:
        (install_directory / "ISCC.exe").write_bytes(b"compiler")
    output_path = tmp_path / "github-output"
    monkeypatch.setenv("TEST_INSTALL_DIRECTORY", str(install_directory))
    monkeypatch.setenv("TEST_REGISTRATIONS", json.dumps(registrations))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))

    workflow = WORKFLOW.read_text(encoding="utf-8")
    validation = workflow.split("          $isccPath = ", 1)[1]
    validation = "$isccPath = " + validation.split("\n\n", 1)[0]
    script = textwrap.dedent(
        r"""
        $ErrorActionPreference = 'Stop'
        $installDirectory = $env:TEST_INSTALL_DIRECTORY
        function Get-ItemProperty {
          param([string[]]$Path, [string]$ErrorAction)
          if ($Path.Count -ne 4 -or $ErrorAction -ne 'SilentlyContinue') {
            throw 'インストール登録の検索条件が不正です'
          }
          foreach ($registration in (ConvertFrom-Json $env:TEST_REGISTRATIONS)) {
            $location = if ($registration.location -eq 'matching') {
              "$installDirectory\"
            } else {
              Join-Path $installDirectory 'other'
            }
            [pscustomobject]@{
              InstallLocation = $location
              DisplayVersion = $registration.version
            }
          }
        }
        function Get-Item {
          throw 'ISCC.exe の ProductVersion は 0.0.0.0 のため参照しません'
        }
        """
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", script + validation],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ.copy(),
        timeout=30,
    )

    assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
    if succeeds:
        assert output_path.read_text(encoding="utf-8").strip() == (
            f"iscc_path={install_directory / 'ISCC.exe'}"
        )
    else:
        assert not output_path.exists()
