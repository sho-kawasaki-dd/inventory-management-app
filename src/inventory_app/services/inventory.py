from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from inventory_app.models import InventoryTransaction, Item, Stock


class InsufficientStockError(Exception):
    """Raised when a transaction would cause negative stock."""
    pass


class ItemNotFoundError(Exception):
    """Raised when an item does not exist."""
    pass


class TransactionNotFoundError(Exception):
    """Raised when a transaction does not exist."""
    pass


class AlreadyReversedError(Exception):
    """Raised when attempting to reverse an already-reversed transaction."""
    pass


def apply_inventory_delta(
    session: Session,
    item_id: UUID,
    delta: float,
    txn_type: str,
    reason: str | None = None,
    reverses_transaction_id: UUID | None = None,
) -> InventoryTransaction:
    """
    Apply an inventory delta and create a transaction record atomically.
    
    Args:
        session: SQLAlchemy session
        item_id: UUID of the item
        delta: Change in quantity (positive or negative)
        txn_type: Type of transaction (RECEIPT, ISSUE, ADJUST, STOCKTAKE, REVERSAL)
        reason: Optional reason for the transaction
        reverses_transaction_id: Optional ID of transaction being reversed
        
    Returns:
        Created InventoryTransaction instance
        
    Raises:
        ItemNotFoundError: If the item does not exist
        InsufficientStockError: If the operation would cause negative stock
    """
    # Verify item exists
    item = session.get(Item, item_id)
    if not item:
        raise ItemNotFoundError(f"Item {item_id} not found")
    
    # Get or create stock record
    stock = session.execute(
        select(Stock).where(Stock.item_id == item_id)
    ).scalar_one_or_none()
    
    if not stock:
        # Create new stock record only if delta is positive or zero
        if delta < 0:
            raise InsufficientStockError(f"Cannot reduce stock for item {item_id}: no stock record exists")
        stock = Stock(item_id=item_id, quantity=0)
        session.add(stock)
        session.flush()
    
    # For decrements, use atomic conditional UPDATE to prevent race conditions
    if delta < 0:
        # Use raw SQL with RETURNING to atomically check and update
        result = session.execute(
            text(
                "UPDATE stocks SET quantity = quantity + :delta, updated_at = now() "
                "WHERE item_id = :item_id AND quantity + :delta >= 0 "
                "RETURNING quantity"
            ),
            {"delta": delta, "item_id": item_id}
        )
        row = result.fetchone()
        if row is None:
            # No row was updated, meaning insufficient stock
            raise InsufficientStockError(
                f"Insufficient stock for item {item_id}. Cannot apply delta {delta}"
            )
        # Refresh the stock object to get updated quantity
        session.expire(stock)
    else:
        # For positive deltas, simple update is safe
        stock.quantity = float(stock.quantity) + delta
    
    # Create transaction record
    txn = InventoryTransaction(
        item_id=item_id,
        delta_quantity=delta,
        txn_type=txn_type,
        reason=reason,
        reverses_transaction_id=reverses_transaction_id,
    )
    session.add(txn)
    session.flush()
    
    return txn
