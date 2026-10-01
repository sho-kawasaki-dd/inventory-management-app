import subprocess
import sys
import tomllib
from pathlib import Path

from inventory_manager_mini import __version__
from inventory_manager_mini.app import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_package_version_matches_project_metadata() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert __version__ == project["project"]["version"]


def test_main_returns_zero() -> None:
    assert main() == 0


def test_module_exits_successfully() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "inventory_manager_mini"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
