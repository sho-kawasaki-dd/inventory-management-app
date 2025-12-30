from __future__ import annotations

from uuid import UUID

from flask import Blueprint, request
from sqlalchemy import and_, func, select

from inventory_app.db import get_session
from inventory_app.http import error, ok
from inventory_app.models import Item, Stock, Stocktake, StocktakeLine
from inventory_app.schemas.stocktakes import StocktakeCreate

bp = Blueprint("stocktakes", __name__)


@bp.get("/stocktakes")
def list_stocktakes():
    s = get_session()

    lines_count_sq = (
        select(func.count(StocktakeLine.id))
        .where(StocktakeLine.stocktake_id == Stocktake.id)
        .correlate(Stocktake)
        .scalar_subquery()
    )

    diff_count_sq = (
        select(func.count(StocktakeLine.id))
        .where(
            and_(
                StocktakeLine.stocktake_id == Stocktake.id,
                StocktakeLine.counted_quantity.is_not(None),
                StocktakeLine.counted_quantity != StocktakeLine.expected_quantity,
            )
        )
        .correlate(Stocktake)
        .scalar_subquery()
    )

    rows = s.execute(
        select(
            Stocktake.id,
            Stocktake.title,
            Stocktake.started_at,
            Stocktake.completed_at,
            Stocktake.created_at,
            lines_count_sq.label("lines_count"),
            diff_count_sq.label("diff_count"),
        ).order_by(Stocktake.id.desc())
    ).all()

    data = []
    for r in rows:
        data.append(
            {
                "id": str(r.id),
                "title": r.title,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "lines_count": int(r.lines_count or 0),
                "diff_count": int(r.diff_count or 0),
            }
        )
    return ok(data)


@bp.post("/stocktakes")
def create_stocktake():
    payload = request.get_json(force=True)
    data = StocktakeCreate(**payload)

    s = get_session()
    st = Stocktake(title=data.title)
    s.add(st)
    s.flush()

    # Generate lines from current stock
    rows = s.execute(select(Stock, Item).join(Item, Item.id == Stock.item_id)).all()
    for stock, item in rows:
        line = StocktakeLine(
            stocktake_id=st.id,
            item_id=item.id,
            expected_quantity=stock.quantity,
            counted_quantity=None,
            shelf_location=stock.shelf_location,
            shelf_location_note=stock.shelf_location_note,
        )
        s.add(line)

    s.commit()
    return ok({"id": str(st.id)}, 201)


@bp.get("/stocktakes/<uuid:stocktake_id>")
def get_stocktake(stocktake_id: UUID):
    s = get_session()
    st = s.get(Stocktake, stocktake_id)
    if not st:
        return error("Stocktake not found", 404)

    lines = s.execute(
        select(StocktakeLine, Item)
        .join(Item, Item.id == StocktakeLine.item_id)
        .where(StocktakeLine.stocktake_id == stocktake_id)
        .order_by(Item.name.asc())
    ).all()

    line_data = []
    diff_count = 0
    for line, item in lines:
        expected = float(line.expected_quantity or 0)
        counted = None if line.counted_quantity is None else float(line.counted_quantity)
        is_diff = counted is not None and counted != expected
        if is_diff:
            diff_count += 1

        # include shelf_location_note per request
        line_data.append(
            {
                "id": line.id,
                "item_id": str(item.id),
                "sku": item.sku,
                "name": item.name,
                "unit": item.unit,
                "expected_quantity": expected,
                "counted_quantity": counted,
                "shelf_location": line.shelf_location,
                "shelf_location_note": line.shelf_location_note,
                "note": line.note,
                "is_diff": is_diff,
            }
        )

    return ok(
        {
            "id": str(st.id),
            "title": st.title,
            "started_at": st.started_at.isoformat() if st.started_at else None,
            "completed_at": st.completed_at.isoformat() if st.completed_at else None,
            "created_at": st.created_at.isoformat() if st.created_at else None,
            "lines": line_data,
            "lines_count": len(line_data),
            "diff_count": diff_count,
        }
    )


@bp.patch("/stocktakes/lines/<int:line_id>")
def update_stocktake_line(line_id: int):
    s = get_session()
    line = s.get(StocktakeLine, line_id)
    if not line:
        return error("Line not found", 404)

    payload = request.get_json(force=True)
    for field in ["counted_quantity", "note"]:
        if field in payload:
            setattr(line, field, payload[field])

    s.commit()
    return ok({"status": "ok"})


@bp.post("/stocktakes/<uuid:stocktake_id>/confirm")
def confirm_stocktake(stocktake_id: UUID):
    """Apply counted quantities to stocks."""
    s = get_session()
    st = s.get(Stocktake, stocktake_id)
    if not st:
        return error("Stocktake not found", 404)

    lines = s.execute(
        select(StocktakeLine).where(StocktakeLine.stocktake_id == stocktake_id)
    ).scalars().all()

    for line in lines:
        if line.counted_quantity is None:
            continue
        stock = s.execute(select(Stock).where(Stock.item_id == line.item_id)).scalar_one_or_none()
        if not stock:
            stock = Stock(item_id=line.item_id, quantity=line.counted_quantity)
            s.add(stock)
        else:
            stock.quantity = line.counted_quantity

    st.completed_at = func.now()
    s.commit()
    return ok({"status": "ok"})
