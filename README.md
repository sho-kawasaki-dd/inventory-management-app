# Inventory Management App (Flask + SQLAlchemy + PostgreSQL)

A small inventory management web app scaffold using:

- Flask
- SQLAlchemy 2.x
- Alembic migrations
- PostgreSQL

## Quickstart

### 1) Create a virtual environment and install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 2) Configure environment

Copy the example env file:

```bash
cp .env.example .env
```

Edit `DATABASE_URL` as needed.

### 3) Create database

Example (local):

```bash
# デフォルトのPostgreSQLユーザーがpostgresの場合
createdb -U postgres inventory_app
```

### 4) Run migrations (Alembic)

This repo includes an initial Alembic setup.

```bash
alembic upgrade head
```

> Note: The migration automatically enables the `pgcrypto` extension for UUID support with `gen_random_uuid()`.

If you need to initialize Alembic from scratch (normally not required):

```bash
alembic init migrations
```

### 5) Run the app

```bash
flask --app inventory_app.app run
```

Then open: http://127.0.0.1:5000

## Optional: apply SQL bootstrap script

If you prefer to create tables using raw SQL instead of Alembic (or for inspection), you can run:

```bash
psql "$DATABASE_URL" -f sql/001_init.sql
```

> Note: If you use Alembic, you typically do **not** also apply the SQL file (avoid duplication).

## Routes (high level)

- UI
  - `/` : redirect to `/stocks`
  - `/items`, `/stocks`, `/stocktakes`
  - `/stocktakes/<id>` detail

- JSON API (internal scaffold)
  - `/api/items`
  - `/api/stocks`
  - `/api/stocktakes` (GET includes `lines_count` and `diff_count`)
  - `/api/stocktakes/<id>` (GET includes `shelf_location_note`)
  - `/api/suggestions`

## Development notes

- App package: `src/inventory_app`
- Templates: `src/inventory_app/templates`
- Static: `src/inventory_app/static`

## License

MIT (or replace as desired)
