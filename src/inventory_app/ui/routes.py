from __future__ import annotations

from uuid import UUID

from flask import Blueprint, redirect, render_template, url_for

bp = Blueprint("ui", __name__)


@bp.get("/")
def index():
    return redirect(url_for("ui.stocks"))


@bp.get("/stocks")
def stocks():
    return render_template("stocks.html")


@bp.get("/items")
def items():
    return render_template("items.html")


@bp.get("/item-transactions")
def item_transactions():
    # future extension
    return render_template("item_transactions.html")


@bp.get("/stocktakes")
def stocktakes():
    return render_template("stocktakes.html")


@bp.get("/stocktakes/<uuid:stocktake_id>")
def stocktake_detail(stocktake_id: UUID):
    return render_template("stocktake_detail.html", stocktake_id=stocktake_id)
