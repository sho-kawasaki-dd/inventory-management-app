from dataclasses import dataclass
from pathlib import Path

from inventory_manager_mini.config import AppPaths
from inventory_manager_mini.core.reports import ReportService
from inventory_manager_mini.core.services import (
    BackupService,
    InventoryService,
    MasterService,
    SettingsService,
)
from inventory_manager_mini.ui.signals import DataBus


@dataclass(slots=True)
class AppContext:
    inventory: InventoryService
    master: MasterService
    settings: SettingsService
    reports: ReportService
    backup: BackupService
    paths: AppPaths
    data_bus: DataBus
    db_path: Path
    schema_version: int
    app_version: str
