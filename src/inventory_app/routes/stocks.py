from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy import select

from inventory_app.db import get_session
from inventory_app.http import error, ok
from inventory_app.models import Item, Stock

bp = Blueprint("stocks", __name__)


@bp.get("/stocks")
def list_stocks():
    s = get_session()
    rows = (
        s.execute(select(Stock, Item).join(Item, Item.id == Stock.item_id).order_by(Item.name.asc()))
        .all()
    )
    data = []
    for st, it in rows:
        data.append(
            {
                "id": st.id,
                "item_id": str(it.id),
                "sku": it.sku,
                "name": it.name,
                "unit": it.unit,
                "quantity": float(st.quantity or 0),
                "shelf_location": st.shelf_location,
                "shelf_location_note": st.shelf_location_note,
                "updated_at": st.updated_at.isoformat() if st.updated_at else None,
            }
        )
    return ok(data)


@bp.patch("/stocks/<int:stock_id>")
def update_stock(stock_id: int):
    s = get_session()
    st = s.get(Stock, stock_id)
    if not st:
        return error("Stock not found", 404)

    payload = request.get_json(force=True)
    for field in ["quantity", "shelf_location", "shelf_location_note"]:
        if field in payload:
            setattr(st, field, payload[field])

    s.commit()
    return ok({"status": "ok"})
