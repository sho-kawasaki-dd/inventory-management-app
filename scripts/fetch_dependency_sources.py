from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit
from urllib.request import urlopen

DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "packaging" / "dependency-sources"
CHUNK_SIZE = 1024 * 1024

Opener = Callable[[str], BinaryIO]


def _open_url(url: str) -> BinaryIO:
    return cast(BinaryIO, urlopen(url, timeout=60))


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _load_sources(directory: Path) -> list[dict[str, str]]:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"manifest.json を読み込めません: {error}") from error

    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(sources, list)
        or not sources
    ):
        raise ValueError("manifest.json の schema_version=1 と sources が必要です")

    entries: list[dict[str, str]] = []
    for source in sources:
        entry = {
            key: source.get(key) if isinstance(source, dict) else None
            for key in ("component", "path", "sha256", "url")
        }
        if not all(isinstance(value, str) and value for value in entry.values()):
            raise ValueError("sources の各項目には component/path/sha256/url が必要です")
        typed = {key: str(value) for key, value in entry.items()}
        parts = urlsplit(typed["url"])
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"url は https のみ許可します: {typed['url']}")
        relative = Path(typed["path"])
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise ValueError(f"path は置き場直下のファイル名にしてください: {typed['path']}")
        if len(typed["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in typed["sha256"]):
            raise ValueError(f"sha256 は小文字 16 進 64 桁で記載してください: {typed['path']}")
        entries.append(typed)
    return entries


def _download(url: str, destination: Path, expected_sha256: str, opener: Opener) -> None:
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(url) as response, partial.open("wb") as stream:
            while chunk := response.read(CHUNK_SIZE):
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError(f"SHA256 が一致しません: {url}")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def fetch_sources(directory: Path = DEFAULT_DIRECTORY, *, opener: Opener = _open_url) -> list[Path]:
    """manifest.json の依存ソースを取得して SHA256 を検証し、置き場のパスを返す。"""
    entries = _load_sources(directory)
    paths: list[Path] = []
    for entry in entries:
        destination = directory / entry["path"]
        if destination.is_file() and _sha256_of(destination) == entry["sha256"]:
            paths.append(destination)
            continue
        _download(entry["url"], destination, entry["sha256"], opener)
        paths.append(destination)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="依存ソースを manifest.json に従って取得し、SHA256 を検証します"
    )
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    args = parser.parse_args(argv)

    try:
        paths = fetch_sources(args.directory)
    except (OSError, ValueError) as error:
        print(f"依存ソースの取得に失敗しました: {error}", file=sys.stderr)
        return 1

    for path in paths:
        print(f"検証済み: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
