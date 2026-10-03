from dataclasses import dataclass
from pathlib import Path

from inventory_manager_mini.core.services import InventoryService, MasterService, SettingsService
from inventory_manager_mini.ui.signals import DataBus


@dataclass(slots=True)
class AppContext:
    inventory: InventoryService
    master: MasterService
    settings: SettingsService
    data_bus: DataBus
    db_path: Path
    schema_version: int
    app_version: str
