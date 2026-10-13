from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import tomllib
from pathlib import Path

TAG_PATTERN = re.compile(r"v([0-9]+\.[0-9]+\.[0-9]+)(-rc[1-9][0-9]*)?")
VERSION_PATTERN = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SUFFIX_PATTERN = re.compile(r"-rc[1-9][0-9]*")


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


def _release_info(
    tag: str | None,
    version: str | None,
    suffix: str | None,
    project_version: str,
) -> dict[str, str | bool]:
    if tag is not None:
        if suffix is not None:
            raise ValueError("--suffix は --version と併用してください")
        match = TAG_PATTERN.fullmatch(tag)
        if match is None:
            raise ValueError("タグは v<版数> または v<版数>-rc<N> の形式で指定してください")
        release_version = match.group(1)
        release_suffix = match.group(2) or ""
    else:
        if version is None or VERSION_PATTERN.fullmatch(version) is None:
            raise ValueError("--version は X.Y.Z 形式で指定してください")
        release_version = version
        release_suffix = suffix or ""
        if suffix not in (None, "") and SUFFIX_PATTERN.fullmatch(suffix) is None:
            raise ValueError("--suffix は空または -rc<N> (N は正の整数) で指定してください")

    if release_version != project_version:
        raise ValueError(
            f"版数が一致しません: 指定値 {release_version}、pyproject.toml {project_version}"
        )

    return {
        "version": release_version,
        "suffix": release_suffix,
        "is_prerelease": bool(release_suffix),
    }


def _configure_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None, *, project_file: Path | None = None) -> int:
    _configure_streams()
    parser = argparse.ArgumentParser(description="リリース版数と pyproject.toml を照合します")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tag")
    mode.add_argument("--version")
    parser.add_argument("--suffix")
    args = parser.parse_args(argv)

    version_file = project_file or Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        project_version = _read_project_version(version_file)
        result = _release_info(args.tag, args.version, args.suffix, project_version)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
