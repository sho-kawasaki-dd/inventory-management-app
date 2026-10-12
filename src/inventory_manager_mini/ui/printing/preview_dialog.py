from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from PySide6.QtCore import QMarginsF, QTimer
from PySide6.QtGui import QAction, QPageLayout, QPageSize, QShowEvent
from PySide6.QtPrintSupport import (
    QAbstractPrintDialog,
    QPrintDialog,
    QPrinter,
    QPrintPreviewDialog,
)
from PySide6.QtWidgets import QApplication, QDialog, QToolBar, QWidget

from inventory_manager_mini.ui.dialog_placement import center_dialog
from inventory_manager_mini.ui.error_handling import run_guarded

logger = logging.getLogger(__name__)

_PREVIEW_TOOLBAR_ACTION_COUNT = 22
_PREVIEW_ORIENTATION_ACTIONS = (7, 8)
_PREVIEW_PRINT_ACTION = 20
_PREVIEW_PAGE_SETUP_ACTION = 21


class PrintPreviewDialog(QPrintPreviewDialog):
    def __init__(
        self,
        printer: QPrinter,
        orientation: QPageLayout.Orientation,
        total_pages: int,
        render_callback: Callable[[QPrinter, Sequence[int] | None], None],
        parent: QWidget | None = None,
    ) -> None:
        if isinstance(total_pages, bool) or total_pages < 1:
            raise ValueError("総ページ数は1以上で指定してください")

        super().__init__(printer, parent)
        self._printer = printer
        self._orientation = orientation
        self._total_pages = total_pages
        self._render_callback = render_callback
        self._render_failed = False
        self._printing = False
        self._toolbar = self._configure_preview_toolbar()
        self.paintRequested.connect(self._on_paint_requested)

        self.setWindowTitle("印刷プレビュー")
        self.resize(1100, 660)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(self.width(), max(320, available.width() - 20)),
                min(self.height(), max(240, available.height() - 40)),
            )

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        center_dialog(self)

    def _configure_preview_toolbar(self) -> QToolBar:
        toolbars = self.findChildren(QToolBar)
        if len(toolbars) != 1:
            raise RuntimeError("印刷プレビューのツールバー構造が想定と異なります")

        toolbar = toolbars[0]
        actions = toolbar.actions()
        if len(actions) != _PREVIEW_TOOLBAR_ACTION_COUNT:
            raise RuntimeError("印刷プレビューの操作構造が想定と異なります")
        if (
            actions[0].objectName() != "fitWidthAction"
            or actions[1].objectName() != "fitPageAction"
            or [actions[index].objectName() for index in (16, 17, 18)]
            != ["singleModeAction", "facingModeAction", "overviewModeAction"]
        ):
            raise RuntimeError("印刷プレビューの操作識別に失敗しました")
        if any(not actions[index].isSeparator() for index in (2, 6, 9, 15, 19)):
            raise RuntimeError("印刷プレビューの区切り位置が想定と異なります")
        if (
            not all(actions[index].isCheckable() for index in _PREVIEW_ORIENTATION_ACTIONS)
            or actions[_PREVIEW_PRINT_ACTION].isCheckable()
            or actions[_PREVIEW_PAGE_SETUP_ACTION].isCheckable()
            or actions[_PREVIEW_PRINT_ACTION].icon().isNull()
            or actions[_PREVIEW_PAGE_SETUP_ACTION].icon().isNull()
        ):
            raise RuntimeError("印刷プレビューの印刷操作を識別できません")

        for index in _PREVIEW_ORIENTATION_ACTIONS:
            actions[index].setEnabled(False)
            actions[index].setVisible(False)
        page_setup_action = actions[_PREVIEW_PAGE_SETUP_ACTION]
        page_setup_action.setEnabled(False)
        page_setup_action.setVisible(False)

        self.print_action = QAction(self)
        self.print_action.setObjectName("managedPrintAction")
        self.print_action.setText("印刷")
        self.print_action.setToolTip("印刷")
        self.print_action.setIcon(actions[_PREVIEW_PRINT_ACTION].icon())
        self.print_action.triggered.connect(self._on_print_action)

        native_print_action = actions[_PREVIEW_PRINT_ACTION]
        toolbar.insertAction(native_print_action, self.print_action)
        toolbar.removeAction(native_print_action)
        return toolbar

    def _configure_page(self, printer: QPrinter) -> None:
        page_layout = QPageLayout(
            QPageSize(QPageSize.PageSizeId.A4),
            self._orientation,
            QMarginsF(0, 0, 0, 0),
            QPageLayout.Unit.Millimeter,
        )
        if not printer.setPageLayout(page_layout):
            raise RuntimeError("印刷用紙を設定できません")
        printer.setFullPage(True)

        actual = printer.pageLayout()
        margins = actual.margins(QPageLayout.Unit.Millimeter)
        if (
            actual.pageSize().id() != QPageSize.PageSizeId.A4
            or actual.orientation() != self._orientation
            or any(
                abs(value) > 0.01
                for value in (margins.left(), margins.top(), margins.right(), margins.bottom())
            )
            or not printer.fullPage()
        ):
            raise RuntimeError("プリンターが指定の用紙設定に対応していません")

    def _render_safely(
        self,
        printer: QPrinter,
        page_numbers: Sequence[int] | None,
    ) -> bool:
        if self._render_failed:
            return False

        succeeded, _ = run_guarded(
            self,
            lambda: self._render(printer, page_numbers),
        )
        if succeeded:
            return True

        self._render_failed = True
        if self._printing:
            try:
                if not printer.abort():
                    logger.error("印刷失敗後にプリンターを中止できませんでした")
            except Exception:
                logger.exception("印刷失敗後のプリンター中止にも失敗しました")
        QTimer.singleShot(0, self.reject)
        return False

    def _render(self, printer: QPrinter, page_numbers: Sequence[int] | None) -> None:
        self._configure_page(printer)
        self._render_callback(printer, page_numbers)

    def _on_paint_requested(self, printer: QPrinter) -> None:
        if not self._printing:
            self._render_safely(printer, None)

    def _on_print_action(self) -> None:
        if self._render_failed or self._printing:
            return
        run_guarded(self, self._print)

    def _print(self) -> None:
        dialog = QPrintDialog(self._printer, self)
        dialog.setMinMax(1, self._total_pages)
        dialog.setFromTo(1, self._total_pages)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintPageRange, True)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintSelection, False)
        dialog.setOption(QAbstractPrintDialog.PrintDialogOption.PrintCurrentPage, False)
        dialog.setPrintRange(QAbstractPrintDialog.PrintRange.AllPages)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        page_numbers = self._selected_page_numbers(dialog)
        self._printing = True
        try:
            self._render_safely(self._printer, page_numbers)
        finally:
            self._printing = False

    def _selected_page_numbers(self, dialog: QPrintDialog) -> list[int] | None:
        print_range = dialog.printRange()
        if print_range == QAbstractPrintDialog.PrintRange.AllPages:
            return None
        if print_range != QAbstractPrintDialog.PrintRange.PageRange:
            raise ValueError("印刷範囲は全ページまたはページ範囲を指定してください")

        selected_ranges = self._printer.pageRanges().toRangeList()
        if selected_ranges:
            page_numbers: set[int] = set()
            for selected in selected_ranges:
                first_page = selected.from_
                last_page = selected.to
                if not 1 <= first_page <= last_page <= self._total_pages:
                    raise ValueError("印刷ページの範囲が不正です")
                page_numbers.update(range(first_page, last_page + 1))
            return sorted(page_numbers)

        first_page = dialog.fromPage()
        last_page = dialog.toPage()
        if not 1 <= first_page <= last_page <= self._total_pages:
            raise ValueError("印刷ページの範囲が不正です")
        return list(range(first_page, last_page + 1))
