from __future__ import annotations

from pydantic import BaseModel, Field


class TransactionRequest(BaseModel):
    """Base schema for transaction requests."""
    quantity: float = Field(gt=0, description="Quantity must be positive")
    reason: str | None = Field(None, description="Optional reason for the transaction")


class AdjustmentRequest(BaseModel):
    """Schema for adjustment requests (can be positive or negative)."""
    delta: float = Field(description="Delta quantity (positive or negative, but not zero)")
    reason: str | None = Field(None, description="Optional reason for the adjustment")
    
    def model_post_init(self, __context) -> None:
        if self.delta == 0:
            raise ValueError("Delta cannot be zero")


class TransactionResponse(BaseModel):
    """Response schema for transaction operations."""
    transaction_id: str
    item_id: str
    delta_quantity: float
    txn_type: str
    reason: str | None
    created_at: str
