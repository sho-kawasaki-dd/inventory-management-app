from __future__ import annotations

from uuid import UUID

from flask import Blueprint, request
from sqlalchemy import func, select

from inventory_app.db import get_session
from inventory_app.http import error, ok
from inventory_app.models import InventoryTransaction, Item
from inventory_app.schemas.transactions import AdjustmentRequest, TransactionRequest, TransactionResponse
from inventory_app.services.inventory import (
    AlreadyReversedError,
    InsufficientStockError,
    ItemNotFoundError,
    TransactionNotFoundError,
    apply_inventory_delta,
)

bp = Blueprint("transactions", __name__)


@bp.get("/transactions")
def list_transactions():
    """List transactions across all items with pagination."""
    session = get_session()

    limit = request.args.get("limit", default=50, type=int) or 50
    limit = min(max(limit, 1), 100)
    offset = request.args.get("offset", default=0, type=int) or 0
    offset = max(offset, 0)

    total = session.execute(select(func.count()).select_from(InventoryTransaction)).scalar_one()

    rows = (
        session.execute(
            select(InventoryTransaction, Item)
            .join(Item, Item.id == InventoryTransaction.item_id)
            .order_by(InventoryTransaction.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        .all()
    )

    items = [
        {
            "transaction_id": str(t.id),
            "item_id": str(t.item_id),
            "item_name": it.name,
            "item_sku": it.sku,
            "item_unit": it.unit,
            "delta_quantity": float(t.delta_quantity),
            "txn_type": t.txn_type,
            "reason": t.reason,
            "reverses_transaction_id": str(t.reverses_transaction_id)
            if t.reverses_transaction_id
            else None,
            "created_at": t.created_at.isoformat(),
        }
        for t, it in rows
    ]

    meta = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(items),
        "has_next": offset + limit < total,
        "has_prev": offset > 0,
    }

    return ok({"items": items, "meta": meta})


@bp.post("/items/<uuid:item_id>/receipts")
def create_receipt(item_id: UUID):
    """Create a receipt transaction (increase stock)."""
    try:
        payload = request.get_json(force=True)
        data = TransactionRequest(**payload)
    except ValueError as e:
        return error(str(e), 400)
    
    session = get_session()
    try:
        txn = apply_inventory_delta(
            session=session,
            item_id=item_id,
            delta=data.quantity,
            txn_type="RECEIPT",
            reason=data.reason,
        )
        session.commit()
        
        response = TransactionResponse(
            transaction_id=str(txn.id),
            item_id=str(txn.item_id),
            delta_quantity=float(txn.delta_quantity),
            txn_type=txn.txn_type,
            reason=txn.reason,
            created_at=txn.created_at.isoformat(),
        )
        return ok(response.model_dump(), 201)
    except ItemNotFoundError:
        session.rollback()
        return error("Item not found", 404)
    except Exception as e:
        session.rollback()
        return error(str(e), 400)


@bp.get("/items/<uuid:item_id>/transactions")
def list_item_transactions(item_id: UUID):
    """List recent transactions for an item (newest first)."""
    session = get_session()

    # Ensure item exists
    item = session.get(Item, item_id)
    if not item:
        return error("Item not found", 404)

    limit = request.args.get("limit", default=20, type=int) or 20
    limit = min(max(limit, 1), 100)

    txns = (
        session.execute(
            select(InventoryTransaction)
            .where(InventoryTransaction.item_id == item_id)
            .order_by(InventoryTransaction.created_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    data = [
        {
            "transaction_id": str(t.id),
            "item_id": str(t.item_id),
            "delta_quantity": float(t.delta_quantity),
            "txn_type": t.txn_type,
            "reason": t.reason,
            "reverses_transaction_id": str(t.reverses_transaction_id)
            if t.reverses_transaction_id
            else None,
            "created_at": t.created_at.isoformat(),
        }
        for t in txns
    ]

    return ok(data)


@bp.post("/items/<uuid:item_id>/issues")
def create_issue(item_id: UUID):
    """Create an issue transaction (decrease stock)."""
    try:
        payload = request.get_json(force=True)
        data = TransactionRequest(**payload)
    except ValueError as e:
        return error(str(e), 400)
    
    session = get_session()
    try:
        txn = apply_inventory_delta(
            session=session,
            item_id=item_id,
            delta=-data.quantity,  # Negative delta for issue
            txn_type="ISSUE",
            reason=data.reason,
        )
        session.commit()
        
        response = TransactionResponse(
            transaction_id=str(txn.id),
            item_id=str(txn.item_id),
            delta_quantity=float(txn.delta_quantity),
            txn_type=txn.txn_type,
            reason=txn.reason,
            created_at=txn.created_at.isoformat(),
        )
        return ok(response.model_dump(), 201)
    except ItemNotFoundError:
        session.rollback()
        return error("Item not found", 404)
    except InsufficientStockError as e:
        session.rollback()
        return error(str(e), 409)
    except Exception as e:
        session.rollback()
        return error(str(e), 400)


@bp.post("/items/<uuid:item_id>/adjustments")
def create_adjustment(item_id: UUID):
    """Create an adjustment transaction (positive or negative delta)."""
    try:
        payload = request.get_json(force=True)
        data = AdjustmentRequest(**payload)
    except ValueError as e:
        return error(str(e), 400)
    
    session = get_session()
    try:
        txn = apply_inventory_delta(
            session=session,
            item_id=item_id,
            delta=data.delta,
            txn_type="ADJUST",
            reason=data.reason,
        )
        session.commit()
        
        response = TransactionResponse(
            transaction_id=str(txn.id),
            item_id=str(txn.item_id),
            delta_quantity=float(txn.delta_quantity),
            txn_type=txn.txn_type,
            reason=txn.reason,
            created_at=txn.created_at.isoformat(),
        )
        return ok(response.model_dump(), 201)
    except ItemNotFoundError:
        session.rollback()
        return error("Item not found", 404)
    except InsufficientStockError as e:
        session.rollback()
        return error(str(e), 409)
    except Exception as e:
        session.rollback()
        return error(str(e), 400)


@bp.post("/transactions/<uuid:transaction_id>/reverse")
def reverse_transaction(transaction_id: UUID):
    """Reverse a transaction by creating an opposite transaction."""
    session = get_session()
    
    # Get the original transaction
    original_txn = session.get(InventoryTransaction, transaction_id)
    if not original_txn:
        return error("Transaction not found", 404)
    
    # Check if already reversed (find if another transaction reverses this one)
    existing_reversal = session.execute(
        select(InventoryTransaction).where(
            InventoryTransaction.reverses_transaction_id == transaction_id
        )
    ).scalar_one_or_none()
    
    if existing_reversal:
        return error("Transaction already reversed", 409)
    
    # Create reversal transaction with opposite delta
    try:
        reversal_txn = apply_inventory_delta(
            session=session,
            item_id=original_txn.item_id,
            delta=-float(original_txn.delta_quantity),
            txn_type="REVERSAL",
            reason=f"Reversal of transaction {transaction_id}",
            reverses_transaction_id=transaction_id,
        )
        session.commit()
        
        response = TransactionResponse(
            transaction_id=str(reversal_txn.id),
            item_id=str(reversal_txn.item_id),
            delta_quantity=float(reversal_txn.delta_quantity),
            txn_type=reversal_txn.txn_type,
            reason=reversal_txn.reason,
            created_at=reversal_txn.created_at.isoformat(),
        )
        return ok(response.model_dump(), 201)
    except InsufficientStockError as e:
        session.rollback()
        return error(str(e), 409)
    except Exception as e:
        session.rollback()
        return error(str(e), 400)
