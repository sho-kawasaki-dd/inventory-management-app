from __future__ import annotations

from uuid import UUID

from flask import Blueprint, request
from sqlalchemy import select

from inventory_app.db import get_session
from inventory_app.http import error, ok
from inventory_app.models import Item, Stock
from inventory_app.schemas.items import ItemCreate, ItemOut

bp = Blueprint("items", __name__)


@bp.get("/items")
def list_items():
    s = get_session()
    q = s.execute(select(Item).order_by(Item.id.desc())).scalars().all()
    return ok([ItemOut.from_orm(i).model_dump() for i in q])


@bp.post("/items")
def create_item():
    payload = request.get_json(force=True)
    data = ItemCreate(**payload)

    s = get_session()
    item = Item(sku=data.sku, name=data.name, unit=data.unit)
    s.add(item)
    s.flush()

    # ensure Stock row exists
    stock = Stock(item_id=item.id, quantity=0)
    s.add(stock)

    s.commit()
    return ok(ItemOut.from_orm(item).model_dump(), 201)


@bp.get("/items/<uuid:item_id>")
def get_item(item_id: UUID):
    s = get_session()
    item = s.get(Item, item_id)
    if not item:
        return error("Item not found", 404)
    return ok(ItemOut.from_orm(item).model_dump())
