from __future__ import annotations

import sqlite3

import pytest

from inventory_manager_mini.core.errors import ValidationError
from inventory_manager_mini.core.services import SettingsService
from inventory_manager_mini.db.connection import transaction


def test_fiscal_year_start_month_get_and_set(settings: SettingsService) -> None:
    assert settings.get_fiscal_year_start_month() == 4
    settings.set_fiscal_year_start_month(1)
    assert settings.get_fiscal_year_start_month() == 1
    assert settings.validate_all() == []


@pytest.mark.parametrize("month", [0, 13, -1, True, 1.5])
def test_set_fiscal_year_start_month_rejects_invalid_values(
    settings: SettingsService, month: int
) -> None:
    with pytest.raises(ValidationError):
        settings.set_fiscal_year_start_month(month)
    assert settings.get_fiscal_year_start_month() == 4


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("other", "value", "未知の設定キー"),
        ("fiscal_year_start_month", "", "正規の整数表記"),
        ("fiscal_year_start_month", "04", "正規の整数表記"),
        ("fiscal_year_start_month", "0", "1〜12"),
        ("fiscal_year_start_month", "13", "1〜12"),
    ],
)
def test_validate_all_reports_invalid_setting_values(
    settings: SettingsService,
    seeded_conn: sqlite3.Connection,
    key: str,
    value: str,
    expected: str,
) -> None:
    if key == "other":
        seeded_conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    else:
        seeded_conn.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key))

    assert any(expected in reason for reason in settings.validate_all())


def test_validate_all_reports_missing_required_key(
    settings: SettingsService, seeded_conn: sqlite3.Connection
) -> None:
    seeded_conn.execute("DELETE FROM settings WHERE key = 'fiscal_year_start_month'")
    assert "年度開始月の設定がありません" in settings.validate_all()
    with pytest.raises(ValidationError):
        settings.get_fiscal_year_start_month()


def test_read_methods_participate_in_existing_transaction_without_ending_it(
    settings: SettingsService,
    seeded_conn: sqlite3.Connection,
) -> None:
    with transaction(seeded_conn):
        assert settings.get_fiscal_year_start_month() == 4
        assert settings.validate_all() == []
        assert seeded_conn.in_transaction
    assert not seeded_conn.in_transaction


def test_get_and_validate_are_read_only(
    settings: SettingsService, seeded_conn: sqlite3.Connection
) -> None:
    before = seeded_conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    assert settings.get_fiscal_year_start_month() == 4
    assert settings.validate_all() == []
    after = seeded_conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    assert after == before
