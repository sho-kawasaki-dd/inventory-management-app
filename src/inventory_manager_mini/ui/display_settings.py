from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

ALLOWED_FONT_SCALES: tuple[int, ...] = (90, 100, 125, 150)
DEFAULT_FONT_SCALE_PERCENT = 100
LABEL_OFFSET_MIN_MM = -10.0
LABEL_OFFSET_MAX_MM = 10.0
LABEL_OFFSET_STEP_MM = 0.1
DEFAULT_LABEL_OFFSET_MM = 0.0

_FONT_FAMILY_KEY = "font_family"
_FONT_SCALE_KEY = "font_scale_percent"
_LABEL_OFFSET_LEFT_KEY = "label_offset_left_mm"
_LABEL_OFFSET_TOP_KEY = "label_offset_top_mm"
_MISSING = object()

# 起動時に取得した OS 既定フォント。倍率はこのフォントに対して適用する。
_base_font: QFont | None = None


@dataclass(frozen=True, slots=True)
class DisplaySettings:
    font_family: str | None = None
    font_scale_percent: int = DEFAULT_FONT_SCALE_PERCENT
    label_offset_left_mm: float = DEFAULT_LABEL_OFFSET_MM
    label_offset_top_mm: float = DEFAULT_LABEL_OFFSET_MM


def default_display_settings() -> DisplaySettings:
    return DisplaySettings()


def load_display_settings(path: Path) -> DisplaySettings:
    try:
        text = path.read_text(encoding="utf-8")
        data: object = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        logger.warning("UI 表示設定を読み込めないため既定値を使用します: %s (%s)", path, error)
        return default_display_settings()

    if not isinstance(data, dict):
        logger.warning("UI 表示設定の形式が不正なため既定値を使用します: %s", path)
        return default_display_settings()

    return DisplaySettings(
        font_family=_parse_font_family(data.get(_FONT_FAMILY_KEY)),
        font_scale_percent=_parse_font_scale(data.get(_FONT_SCALE_KEY)),
        label_offset_left_mm=_parse_label_offset(
            data.get(_LABEL_OFFSET_LEFT_KEY, _MISSING), _LABEL_OFFSET_LEFT_KEY
        ),
        label_offset_top_mm=_parse_label_offset(
            data.get(_LABEL_OFFSET_TOP_KEY, _MISSING), _LABEL_OFFSET_TOP_KEY
        ),
    )


def _parse_font_family(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    logger.warning("UI 表示設定のフォント名が不正なため OS 既定を使用します")
    return None


def _parse_font_scale(value: object) -> int:
    if value is None:
        return DEFAULT_FONT_SCALE_PERCENT
    # bool は int のサブクラスだが数値として受け付けない
    if isinstance(value, int) and not isinstance(value, bool) and value in ALLOWED_FONT_SCALES:
        return value
    logger.warning(
        "UI 表示設定の文字サイズが不正なため %d%% を使用します", DEFAULT_FONT_SCALE_PERCENT
    )
    return DEFAULT_FONT_SCALE_PERCENT


def _parse_label_offset(value: object, key: str) -> float:
    if value is _MISSING:
        return DEFAULT_LABEL_OFFSET_MM
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            number = math.nan
        if math.isfinite(number) and LABEL_OFFSET_MIN_MM <= number <= LABEL_OFFSET_MAX_MM:
            return round(number, 1)
    logger.warning(
        "UI 表示設定の位置補正値が不正なため %.1f mm を使用します: %s",
        DEFAULT_LABEL_OFFSET_MM,
        key,
    )
    return DEFAULT_LABEL_OFFSET_MM


def save_display_settings(path: Path, settings: DisplaySettings) -> None:
    payload = {
        _FONT_FAMILY_KEY: settings.font_family,
        _FONT_SCALE_KEY: settings.font_scale_percent,
        _LABEL_OFFSET_LEFT_KEY: settings.label_offset_left_mm,
        _LABEL_OFFSET_TOP_KEY: settings.label_offset_top_mm,
    }
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def capture_base_font() -> QFont:
    global _base_font
    if _base_font is None:
        _base_font = QFont(QApplication.font())
    return QFont(_base_font)


def apply_display_settings(settings: DisplaySettings) -> None:
    font = capture_base_font()

    family = settings.font_family
    if family is not None:
        if family in QFontDatabase.families():
            font.setFamily(family)
        else:
            logger.warning("フォントが見つからないため OS 既定を使用します: %s", family)

    _scale_font(font, settings.font_scale_percent)
    QApplication.setFont(font)


def reset_display_settings() -> DisplaySettings:
    settings = default_display_settings()
    apply_display_settings(settings)
    return settings


def _scale_font(font: QFont, percent: int) -> None:
    point_size = font.pointSizeF()
    if point_size > 0:
        font.setPointSizeF(point_size * percent / 100)
        return
    # ポイント指定が使えない場合はピクセルサイズで拡縮する
    pixel_size = font.pixelSize()
    if pixel_size > 0:
        font.setPixelSize(max(1, round(pixel_size * percent / 100)))
