import os
import subprocess
import sys
import tomllib
from pathlib import Path

from PySide6.QtWidgets import QApplication

from inventory_manager_mini import __version__
from inventory_manager_mini.app import main
from inventory_manager_mini.config import AppPaths

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_package_version_matches_project_metadata() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert __version__ == project["project"]["version"]


def test_main_returns_zero(tmp_path: Path, monkeypatch, qtbot) -> None:
    paths = AppPaths.from_dirs(tmp_path / "data", tmp_path / "logs")
    monkeypatch.setattr(QApplication, "exec", lambda self: 0)

    assert main(paths) == 0


def test_module_exits_successfully(tmp_path: Path) -> None:
    data_dir = tmp_path / "module-smoke"
    env = os.environ.copy()
    env["INVENTORY_MANAGER_MINI_DATA_DIR"] = str(data_dir)
    if sys.platform != "win32":
        env["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from PySide6.QtCore import QTimer; "
            "from PySide6.QtWidgets import QApplication; "
            "import runpy; "
            "app = QApplication([]); QTimer.singleShot(100, app.quit); "
            "runpy.run_module('inventory_manager_mini', run_name='__main__')",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert (data_dir / "inventory.db").is_file()
