from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask

from inventory_app.db import init_db
from inventory_app.routes.items import bp as items_bp
from inventory_app.routes.stocks import bp as stocks_bp
from inventory_app.routes.stocktakes import bp as stocktakes_bp
from inventory_app.routes.suggestions import bp as suggestions_bp
from inventory_app.routes.transactions import bp as transactions_bp
from inventory_app.ui.routes import bp as ui_bp


def create_app() -> Flask:
    load_dotenv()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")
    app.config["TEMPLATES_AUTO_RELOAD"] = os.getenv("TEMPLATES_AUTO_RELOAD", "0") == "1"

    init_db()

    app.register_blueprint(ui_bp)

    app.register_blueprint(items_bp, url_prefix="/api")
    app.register_blueprint(stocks_bp, url_prefix="/api")
    app.register_blueprint(stocktakes_bp, url_prefix="/api")
    app.register_blueprint(suggestions_bp, url_prefix="/api")
    app.register_blueprint(transactions_bp, url_prefix="/api")

    return app


app = create_app()
