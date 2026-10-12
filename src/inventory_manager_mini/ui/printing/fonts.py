from PySide6.QtGui import QFont, QFontDatabase

_PRINT_FONT_FAMILIES = ("Yu Gothic UI", "Meiryo UI", "Noto Sans CJK JP")


def print_font(point_size: int, bold: bool = False) -> QFont:
    available_families = set(QFontDatabase.families())
    family = next(
        (candidate for candidate in _PRINT_FONT_FAMILIES if candidate in available_families),
        None,
    )
    if family is None:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    else:
        font = QFont(family)

    font.setPointSize(point_size)
    font.setBold(bold)
    return font
