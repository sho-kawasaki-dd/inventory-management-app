"""add_inventory_transactions_table

Revision ID: 62c77c157476
Revises: 58307804bd32
Create Date: 2025-12-30 20:45:49.582915

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '62c77c157476'
down_revision = '58307804bd32'
branch_label = None
depends_on = None


def upgrade() -> None:
    # Create inventory_transactions table
    op.create_table(
        'inventory_transactions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('delta_quantity', sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column('txn_type', sa.Text(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('reverses_transaction_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['reverses_transaction_id'], ['inventory_transactions.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('delta_quantity <> 0', name='ck_inventory_transactions_delta_nonzero')
    )
    
    # Create indexes
    op.create_index('ix_inventory_transactions_item_id_created_at', 'inventory_transactions', ['item_id', 'created_at'])
    op.create_index('ix_inventory_transactions_reverses_transaction_id', 'inventory_transactions', ['reverses_transaction_id'])


def downgrade() -> None:
    op.drop_index('ix_inventory_transactions_reverses_transaction_id', table_name='inventory_transactions')
    op.drop_index('ix_inventory_transactions_item_id_created_at', table_name='inventory_transactions')
    op.drop_table('inventory_transactions')
