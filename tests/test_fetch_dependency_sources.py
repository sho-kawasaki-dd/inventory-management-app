from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO

import pytest

from scripts.fetch_dependency_sources import fetch_sources, main

PAYLOAD = b"source archive bytes"
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
URL = "https://example.invalid/pkg-1.0.tar.gz"


def _write_manifest(directory: Path, **overrides: str) -> None:
    source = {
        "component": "pkg",
        "version": "1.0",
        "path": "pkg-1.0.tar.gz",
        "sha256": PAYLOAD_SHA256,
        "url": URL,
        **overrides,
    }
    manifest = {"schema_version": 1, "audit_status": "complete", "sources": [source]}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class _Opener:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url: str) -> BinaryIO:
        self.urls.append(url)
        return io.BytesIO(self.payload)


def test_downloads_and_verifies_sources(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    opener = _Opener(PAYLOAD)

    paths = fetch_sources(tmp_path, opener=opener)

    assert paths == [tmp_path / "pkg-1.0.tar.gz"]
    assert paths[0].read_bytes() == PAYLOAD
    assert opener.urls == [URL]
    assert not list(tmp_path.glob("*.part"))


def test_skips_download_when_existing_file_matches(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "pkg-1.0.tar.gz").write_bytes(PAYLOAD)
    opener = _Opener(b"must not be fetched")

    fetch_sources(tmp_path, opener=opener)

    assert opener.urls == []


def test_replaces_existing_file_with_wrong_hash(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    (tmp_path / "pkg-1.0.tar.gz").write_bytes(b"corrupted")

    fetch_sources(tmp_path, opener=_Opener(PAYLOAD))

    assert (tmp_path / "pkg-1.0.tar.gz").read_bytes() == PAYLOAD


def test_rejects_hash_mismatch_and_leaves_no_file(tmp_path: Path) -> None:
    _write_manifest(tmp_path)

    with pytest.raises(ValueError, match="SHA256"):
        fetch_sources(tmp_path, opener=_Opener(b"tampered"))

    assert list(tmp_path.glob("pkg-*")) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "http://example.invalid/pkg.tar.gz"},
        {"path": "../pkg.tar.gz"},
        {"path": "sub/pkg.tar.gz"},
        {"sha256": "ABC"},
        {"component": ""},
    ],
)
def test_rejects_invalid_manifest_entries(tmp_path: Path, overrides: dict[str, str]) -> None:
    _write_manifest(tmp_path, **overrides)

    with pytest.raises(ValueError):
        fetch_sources(tmp_path, opener=_Opener(PAYLOAD))


def test_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="manifest.json"):
        fetch_sources(tmp_path, opener=_Opener(PAYLOAD))


def test_cli_reports_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--directory", str(tmp_path)]) == 1
    assert "失敗" in capsys.readouterr().err


def test_cli_handles_non_utf8_stream_encoding(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env.pop("PYTHONUTF8", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.fetch_dependency_sources",
            "--directory",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "失敗" in result.stderr


def test_repository_manifest_is_well_formed() -> None:
    from scripts.fetch_dependency_sources import DEFAULT_DIRECTORY, _load_sources

    entries = _load_sources(DEFAULT_DIRECTORY)

    assert {entry["component"] for entry in entries} >= {"Qt-qtbase", "PySide6", "pyinstaller"}
