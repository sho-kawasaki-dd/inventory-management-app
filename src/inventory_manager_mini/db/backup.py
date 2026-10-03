from __future__ import annotations

import os
import sqlite3
from contextlib import suppress
from pathlib import Path


def copy_database(src_conn: sqlite3.Connection, dest_path: Path) -> None:
    if src_conn.in_transaction:
        raise RuntimeError("トランザクション中はデータベースをコピーできません")

    dest_path = Path(dest_path)
    file_descriptor = os.open(dest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    os.close(file_descriptor)

    dest_conn: sqlite3.Connection | None = None
    try:
        dest_conn = sqlite3.connect(dest_path, autocommit=True)
        src_conn.backup(dest_conn)
        dest_conn.close()
        dest_conn = None
    except BaseException:
        if dest_conn is not None:
            dest_conn.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            with suppress(FileNotFoundError):
                Path(f"{dest_path}{suffix}").unlink()
        raise
