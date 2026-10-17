CREATE TABLE clients (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE purchasers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE staff (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE categories (
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES categories(id),
  name TEXT NOT NULL,
  code_prefix TEXT NOT NULL UNIQUE
    CHECK (length(code_prefix) BETWEEN 2 AND 5 AND code_prefix NOT GLOB '*[^A-Z0-9]*'),
  next_seq INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX uq_categories_parent_name
ON categories(COALESCE(parent_id, 0), name);

CREATE TABLE locations (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  client_id INTEGER NOT NULL REFERENCES clients(id),
  purchaser_id INTEGER NOT NULL REFERENCES purchasers(id),
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  location_id INTEGER REFERENCES locations(id),
  unit TEXT NOT NULL DEFAULT '個' CHECK (unit = '個'),
  quantity INTEGER NOT NULL DEFAULT 0
    CHECK (typeof(quantity) = 'integer' AND quantity BETWEEN 0 AND 1000000),
  reorder_threshold INTEGER NOT NULL DEFAULT 0
    CHECK (typeof(reorder_threshold) = 'integer' AND reorder_threshold BETWEEN 0 AND 1000000),
  reorder_quantity INTEGER CHECK (
    reorder_quantity IS NULL OR
    (typeof(reorder_quantity) = 'integer' AND reorder_quantity BETWEEN 1 AND 1000000)
  ),
  purchase_url TEXT,
  supplier TEXT,
  manufacturer_part_number TEXT,
  application TEXT,
  reference_price INTEGER CHECK (
    reference_price IS NULL OR
    (typeof(reference_price) = 'integer' AND reference_price BETWEEN 0 AND 10000000)
  ),
  note TEXT,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER trg_items_updated_at AFTER UPDATE ON items
WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE items SET updated_at = datetime('now') WHERE id = NEW.id;
END;

CREATE TABLE stock_movements (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  client_id INTEGER NOT NULL REFERENCES clients(id),
  purchaser_id INTEGER NOT NULL REFERENCES purchasers(id),
  staff_id INTEGER NOT NULL REFERENCES staff(id),
  reason TEXT NOT NULL CHECK (reason IN ('in','out','return','dispose','adjust')),
  delta INTEGER NOT NULL CHECK (
    typeof(delta) = 'integer' AND delta BETWEEN -1000000 AND 1000000
  ),
  unit_price INTEGER CHECK (
    unit_price IS NULL OR
    (typeof(unit_price) = 'integer' AND unit_price BETWEEN 0 AND 10000000)
  ),
  cost_amount INTEGER CHECK (
    cost_amount IS NULL OR
    (typeof(cost_amount) = 'integer' AND cost_amount BETWEEN -100000000 AND 100000000)
  ),
  used_for TEXT,
  reversal_of INTEGER UNIQUE REFERENCES stock_movements(id),
  note TEXT,
  moved_at TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (
    reversal_of IS NOT NULL
    OR (reason = 'in' AND delta > 0)
    OR (reason IN ('out','return','dispose') AND delta < 0)
    OR reason = 'adjust'
  ),
  CHECK (
    (reason IN ('out','dispose')) = (cost_amount IS NOT NULL)
  ),
  CHECK (
    cost_amount IS NULL
    OR (reversal_of IS NULL AND cost_amount >= 0)
    OR (reversal_of IS NOT NULL AND cost_amount <= 0)
  ),
  CHECK (
    reason NOT IN ('out','return','dispose') OR unit_price IS NULL
  )
);

CREATE INDEX idx_movements_item ON stock_movements(item_id, moved_at);
CREATE INDEX idx_movements_client_date ON stock_movements(client_id, moved_at);
CREATE INDEX idx_movements_purchaser_date ON stock_movements(purchaser_id, moved_at);
CREATE INDEX idx_movements_item_purchase ON stock_movements(item_id, moved_at DESC, id DESC)
WHERE reason = 'in' AND reversal_of IS NULL;
CREATE INDEX idx_movements_item_lot ON stock_movements(item_id, moved_at, id)
WHERE reversal_of IS NULL AND delta > 0 AND reason IN ('in','adjust');

CREATE TABLE stock_allocations (
  id INTEGER PRIMARY KEY,
  movement_id INTEGER NOT NULL REFERENCES stock_movements(id),
  lot_id INTEGER NOT NULL REFERENCES stock_movements(id),
  quantity INTEGER NOT NULL CHECK (
    typeof(quantity) = 'integer' AND quantity BETWEEN -1000000 AND 1000000 AND quantity <> 0
  ),
  UNIQUE (movement_id, lot_id)
);
CREATE INDEX idx_allocations_lot ON stock_allocations(lot_id);

CREATE TABLE total_aggregates (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  inbound_quantity INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(inbound_quantity) = 'integer' AND inbound_quantity BETWEEN 0 AND 1000000000000
  ),
  outbound_quantity INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(outbound_quantity) = 'integer' AND outbound_quantity BETWEEN 0 AND 1000000000000
  ),
  disposed_quantity INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(disposed_quantity) = 'integer' AND disposed_quantity BETWEEN 0 AND 1000000000000
  ),
  expenditure INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(expenditure) = 'integer' AND expenditure BETWEEN 0 AND 1000000000000
  ),
  disposal_amount INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(disposal_amount) = 'integer' AND disposal_amount BETWEEN 0 AND 1000000000000
  )
);

INSERT INTO total_aggregates (id) VALUES (1);

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO settings (key, value) VALUES ('fiscal_year_start_month', '4');