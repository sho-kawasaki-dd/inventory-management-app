from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ItemCreate(BaseModel):
    sku: str | None = None
    name: str
    unit: str = "pcs"


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sku: str | None
    name: str
    unit: str
