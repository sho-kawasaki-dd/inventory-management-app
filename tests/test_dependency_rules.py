import ast
import re
from importlib.util import resolve_name
from pathlib import Path

import pytest

PACKAGE_NAME = "inventory_manager_mini"


def _module_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((PACKAGE_NAME, *parts))


def _import_targets(tree: ast.Module, module_name: str, is_package: bool) -> list[str]:
    targets: list[str] = []
    package = module_name if is_package else module_name.rpartition(".")[0]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                imported_module = resolve_name("." * node.level + (node.module or ""), package)
            else:
                imported_module = node.module or ""
            if node.module is None and node.level == 0:
                continue
            targets.extend(
                f"{imported_module}.{alias.name}" if alias.name != "*" else imported_module
                for alias in node.names
            )

    return targets


def _expression_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _time_api_violations(tree: ast.Module, path: Path, relative_path: Path) -> bool:
    if relative_path == Path("core/timeutil.py"):
        return False

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".")[0]
                aliases[local_name] = alias.name if alias.asname else local_name
        elif isinstance(node, ast.ImportFrom) and node.module == "datetime":
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"datetime.{alias.name}"

    forbidden_calls = {
        "datetime.now",
        "datetime.utcnow",
        "datetime.today",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "date.today",
        "datetime.date.today",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call_name = _expression_name(node.func, aliases)
            if call_name in forbidden_calls:
                return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower() == "localtime"
        ):
            return True

    if path.suffix == ".sql":
        return (
            re.search(r"(['\"])localtime\1", path.read_text(encoding="utf-8"), re.IGNORECASE)
            is not None
        )
    return False


def _find_violations(source_root: Path) -> list[str]:
    violations: list[str] = []
    package_root = source_root / PACKAGE_NAME

    for path in sorted(package_root.rglob("*.py")):
        relative_path = path.relative_to(package_root)
        module_name = _module_name(path, package_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        targets = _import_targets(tree, module_name, path.name == "__init__.py")

        for target in targets:
            if relative_path.parts[0] in {"core", "db"} and target.startswith("PySide6"):
                violations.append(f"R1:{relative_path}")
            if relative_path.parts[0] == "db" and target.startswith(f"{PACKAGE_NAME}.core"):
                allowed = (f"{PACKAGE_NAME}.core.models", f"{PACKAGE_NAME}.core.errors")
                if (
                    not target.startswith(tuple(f"{name}." for name in allowed))
                    and target not in allowed
                ):
                    violations.append(f"R2:{relative_path}")
            if relative_path in {Path("core/models.py"), Path("core/errors.py")}:
                forbidden = (
                    f"{PACKAGE_NAME}.db",
                    f"{PACKAGE_NAME}.core.services",
                    f"{PACKAGE_NAME}.core.reports",
                    f"{PACKAGE_NAME}.ui",
                )
                if target.startswith(forbidden):
                    violations.append(f"R3:{relative_path}")
            if relative_path.parts[0] == "core" and target.startswith(f"{PACKAGE_NAME}.ui"):
                violations.append(f"R4:{relative_path}")
            if relative_path.parts[0] == "ui" and (
                target == "sqlite3"
                or target.startswith("sqlite3.")
                or target.startswith(f"{PACKAGE_NAME}.db")
            ):
                violations.append(f"R5:{relative_path}")

        if _time_api_violations(tree, path, relative_path):
            violations.append(f"R6:{relative_path}")

    for path in sorted(package_root.rglob("*.sql")):
        relative_path = path.relative_to(package_root)
        if _time_api_violations(ast.Module(body=[], type_ignores=[]), path, relative_path):
            violations.append(f"R6:{relative_path}")

    return violations


def _write_source(source_root: Path, relative_path: str, source: str) -> None:
    path = source_root / PACKAGE_NAME / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_source_tree_obeys_dependency_rules() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"

    assert _find_violations(source_root) == []


@pytest.mark.parametrize(
    ("rule", "file_path", "invalid_source", "valid_source"),
    [
        ("R1", "core/module.py", "import PySide6.QtWidgets", "import pathlib"),
        (
            "R2",
            "db/repository.py",
            "from ..core import services",
            "from ..core.models import Item",
        ),
        (
            "R3",
            "core/models.py",
            "from ..db import repository",
            "from dataclasses import dataclass",
        ),
        ("R4", "core/service.py", "from ..ui import MainWindow", "from pathlib import Path"),
        ("R5", "ui/window.py", "import sqlite3", "from ..core import models"),
        ("R6", "core/service.py", "from datetime import datetime\ndatetime.now()", "value = 1"),
    ],
)
def test_dependency_rule_rejects_violation_and_accepts_compliant_code(
    tmp_path: Path,
    rule: str,
    file_path: str,
    invalid_source: str,
    valid_source: str,
) -> None:
    invalid_root = tmp_path / "invalid"
    _write_source(invalid_root, file_path, invalid_source)
    assert any(violation.startswith(f"{rule}:") for violation in _find_violations(invalid_root))

    valid_root = tmp_path / "valid"
    _write_source(valid_root, file_path, valid_source)
    assert _find_violations(valid_root) == []


@pytest.mark.parametrize(
    ("file_path", "source", "should_violate"),
    [
        ("core/clock.py", "import datetime as dt\ndt.datetime.utcnow()", True),
        (
            "core/clock.py",
            "from datetime import date as CalendarDate\nCalendarDate.today()",
            True,
        ),
        ("core/clock.py", "value = 'localtime'", True),
        ("core/clock.sql", "SELECT datetime('now', 'localtime');", True),
        ("core/clock.py", "value = 1", False),
    ],
)
def test_time_rule_variants(
    tmp_path: Path, file_path: str, source: str, should_violate: bool
) -> None:
    source_root = tmp_path / "source"
    _write_source(source_root, file_path, source)

    assert any(item.startswith("R6:") for item in _find_violations(source_root)) is should_violate


def test_time_rule_allows_core_timeutil(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _write_source(source_root, "core/timeutil.py", "from datetime import datetime\ndatetime.now()")

    assert _find_violations(source_root) == []
