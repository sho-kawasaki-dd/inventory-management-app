"""convert primary keys to uuid

Revision ID: 001
Revises: 
Create Date: 2025-12-30

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_label = None
depends_on = None


def upgrade() -> None:
    # Enable pgcrypto extension for gen_random_uuid()
    op.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto;')
    
    # Create tables with UUID primary keys
    op.create_table('items',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('sku', sa.String(), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('unit', sa.String(), nullable=False, server_default='pcs'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sku')
    )
    
    op.create_table('stocktakes',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    op.create_table('stocks',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=14, scale=3), server_default='0', nullable=False),
        sa.Column('shelf_location', sa.String(), nullable=True),
        sa.Column('shelf_location_note', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('item_id')
    )
    
    op.create_table('stocktake_lines',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('stocktake_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('expected_quantity', sa.Numeric(precision=14, scale=3), server_default='0', nullable=False),
        sa.Column('counted_quantity', sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column('shelf_location', sa.String(), nullable=True),
        sa.Column('shelf_location_note', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['stocktake_id'], ['stocktakes.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('stocktake_id', 'item_id')
    )


def downgrade() -> None:
    op.drop_table('stocktake_lines')
    op.drop_table('stocks')
    op.drop_table('stocktakes')
    op.drop_table('items')
    op.execute('DROP EXTENSION IF EXISTS pgcrypto;')
