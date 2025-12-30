from __future__ import annotations

from flask import Blueprint, request
from sqlalchemy import select

from inventory_app.db import get_session
from inventory_app.http import ok
from inventory_app.models import Item

bp = Blueprint("suggestions", __name__)


@bp.get("/suggestions")
def suggestions():
    q = request.args.get("q", "").strip()
    s = get_session()
    if not q:
        return ok([])

    rows = (
        s.execute(select(Item).where(Item.name.ilike(f"%{q}%")).order_by(Item.name.asc()).limit(10))
        .scalars()
        .all()
    )
    return ok([{"id": str(i.id), "name": i.name, "sku": i.sku} for i in rows])
