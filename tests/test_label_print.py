from __future__ import annotations

from PySide6.QtGui import QFont

from inventory_manager_mini.ui.printing import fonts


def test_print_font_uses_first_available_candidate(monkeypatch, qapp) -> None:
    class FontDatabase:
        @staticmethod
        def families() -> list[str]:
            return ["Noto Sans CJK JP", "Yu Gothic UI"]

    monkeypatch.setattr(fonts, "QFontDatabase", FontDatabase)

    font = fonts.print_font(12, bold=True)

    assert font.family() == "Yu Gothic UI"
    assert font.pointSizeF() == 12
    assert font.bold()


def test_print_font_falls_back_to_system_font(monkeypatch, qapp) -> None:
    fallback_font = QFont("System Fallback")

    class FontDatabase:
        class SystemFont:
            GeneralFont = object()

        @staticmethod
        def families() -> list[str]:
            return []

        @staticmethod
        def systemFont(_font_type: object) -> QFont:
            return QFont(fallback_font)

    monkeypatch.setattr(fonts, "QFontDatabase", FontDatabase)

    font = fonts.print_font(9)

    assert font.family() == "System Fallback"
    assert font.pointSizeF() == 9
    assert not font.bold()
