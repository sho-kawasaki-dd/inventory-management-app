"""add_nonnegative_quantity_checks

Revision ID: 58307804bd32
Revises: 001
Create Date: 2025-12-30 20:04:08.976306

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '58307804bd32'
down_revision = '001'
branch_label = None
depends_on = None


def upgrade() -> None:
    # Add CHECK constraints for non-negative quantities
    op.create_check_constraint(
        'ck_stocks_quantity_nonnegative',
        'stocks',
        'quantity >= 0'
    )
    
    op.create_check_constraint(
        'ck_stocktake_lines_expected_quantity_nonnegative',
        'stocktake_lines',
        'expected_quantity >= 0'
    )
    
    op.create_check_constraint(
        'ck_stocktake_lines_counted_quantity_nonnegative',
        'stocktake_lines',
        'counted_quantity >= 0'
    )
    
    # Make counted_quantity NOT NULL (set to expected_quantity for existing rows)
    op.execute(
        'UPDATE stocktake_lines SET counted_quantity = expected_quantity WHERE counted_quantity IS NULL'
    )
    op.alter_column('stocktake_lines', 'counted_quantity', nullable=False)


def downgrade() -> None:
    # Revert counted_quantity to nullable
    op.alter_column('stocktake_lines', 'counted_quantity', nullable=True)
    
    # Drop CHECK constraints
    op.drop_constraint('ck_stocktake_lines_counted_quantity_nonnegative', 'stocktake_lines')
    op.drop_constraint('ck_stocktake_lines_expected_quantity_nonnegative', 'stocktake_lines')
    op.drop_constraint('ck_stocks_quantity_nonnegative', 'stocks')
