from PySide6.QtCore import QObject, Signal


class DataBus(QObject):
    data_changed = Signal()
