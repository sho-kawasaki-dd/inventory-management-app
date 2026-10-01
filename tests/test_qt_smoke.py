from pathlib import Path

from PySide6.QtCharts import QChart, QChartView, QLineSeries
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QWidget


def test_qt_widgets_can_be_created(qtbot) -> None:
    widget = QWidget()
    qtbot.addWidget(widget)

    assert widget is not None


def test_chart_can_be_rendered_to_image(qtbot, tmp_path: Path) -> None:
    series = QLineSeries()
    series.append(QPointF(0, 0))
    series.append(QPointF(1, 1))
    series.append(QPointF(2, 0.5))

    chart = QChart()
    chart.addSeries(series)
    chart.createDefaultAxes()
    view = QChartView(chart)
    view.resize(480, 320)
    qtbot.addWidget(view)
    view.show()
    qtbot.wait(50)

    image_path = tmp_path / "chart.png"
    image = view.grab().toImage()

    assert not image.isNull()
    assert image.width() > 0
    assert image.height() > 0
    assert image.save(str(image_path))
    assert image_path.stat().st_size > 0

    colors = {
        image.pixelColor(x, y).rgba()
        for x in range(0, image.width(), 8)
        for y in range(0, image.height(), 8)
    }
    assert len(colors) > 1
    assert any(
        image.pixelColor(x, y) != QColor("white")
        for x in range(image.width())
        for y in range(image.height())
    )


def test_printer_can_write_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "smoke.pdf"
    printer = QPrinter()
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(pdf_path))

    painter = QPainter()
    assert painter.begin(printer)
    painter.drawText(100, 100, "Qt PDF smoke test")
    assert painter.end()

    assert pdf_path.is_file()
    assert pdf_path.stat().st_size > 0
    assert pdf_path.read_bytes().startswith(b"%PDF-")
