import pytest

from inventory_manager_mini.core.errors import NegativeStockError, ValidationError
from inventory_manager_mini.core.fifo import plan_fifo_allocations
from inventory_manager_mini.core.models import (
    MAX_STOCK_QUANTITY,
    MAX_UNIT_PRICE,
    Allocation,
    FifoEstimate,
    Lot,
)


def _lot(
    lot_id: int,
    moved_at: str,
    remaining_quantity: int,
    unit_price: int | None,
) -> Lot:
    return Lot(
        id=lot_id,
        moved_at=moved_at,
        remaining_quantity=remaining_quantity,
        unit_price=unit_price,
        purchaser_id=1,
    )


def test_plan_single_lot() -> None:
    allocations, estimate = plan_fifo_allocations([_lot(1, "2026-01-01 00:00:00", 8, 125)], 3)

    assert allocations == (Allocation(lot_id=1, quantity=3),)
    assert estimate == FifoEstimate(cost_amount=375, unpriced_quantity=0)


def test_plan_across_lots_in_fifo_order() -> None:
    lots = [
        _lot(3, "2026-01-03 00:00:00", 5, 30),
        _lot(2, "2026-01-02 00:00:00", 4, 20),
        _lot(1, "2026-01-01 00:00:00", 2, 10),
    ]

    allocations, estimate = plan_fifo_allocations(lots, 5)

    assert allocations == (
        Allocation(lot_id=1, quantity=2),
        Allocation(lot_id=2, quantity=3),
    )
    assert estimate == FifoEstimate(cost_amount=80, unpriced_quantity=0)


def test_plan_skips_empty_lots_and_allows_exact_consumption() -> None:
    lots = [
        _lot(1, "2026-01-01 00:00:00", 0, 10),
        _lot(2, "2026-01-02 00:00:00", 4, 15),
    ]

    allocations, estimate = plan_fifo_allocations(lots, 4)

    assert allocations == (Allocation(lot_id=2, quantity=4),)
    assert estimate == FifoEstimate(cost_amount=60, unpriced_quantity=0)


def test_plan_separates_unpriced_and_zero_price_quantities() -> None:
    lots = [
        _lot(1, "2026-01-01 00:00:00", 2, None),
        _lot(2, "2026-01-02 00:00:00", 3, 0),
        _lot(3, "2026-01-03 00:00:00", 4, 7),
    ]

    allocations, estimate = plan_fifo_allocations(lots, 7)

    assert allocations == (
        Allocation(lot_id=1, quantity=2),
        Allocation(lot_id=2, quantity=3),
        Allocation(lot_id=3, quantity=2),
    )
    assert estimate == FifoEstimate(cost_amount=14, unpriced_quantity=2)


def test_plan_breaks_same_timestamp_ties_by_lot_id() -> None:
    lots = [
        _lot(2, "2026-01-01 00:00:00", 2, 20),
        _lot(1, "2026-01-01 00:00:00", 2, 10),
    ]

    allocations, estimate = plan_fifo_allocations(lots, 1)

    assert allocations == (Allocation(lot_id=1, quantity=1),)
    assert estimate == FifoEstimate(cost_amount=10, unpriced_quantity=0)


def test_plan_uses_exact_integer_arithmetic_at_business_limits() -> None:
    quantity = MAX_STOCK_QUANTITY
    unit_price = MAX_UNIT_PRICE

    allocations, estimate = plan_fifo_allocations(
        [_lot(1, "2026-01-01 00:00:00", quantity, unit_price)], quantity
    )

    assert allocations == (Allocation(lot_id=1, quantity=quantity),)
    assert estimate.cost_amount == quantity * unit_price
    assert estimate.cost_amount == 10_000_000_000_000


def test_plan_rejects_insufficient_lot_balance() -> None:
    with pytest.raises(NegativeStockError, match="在庫が不足"):
        plan_fifo_allocations([_lot(1, "2026-01-01 00:00:00", 2, 10)], 3)


@pytest.mark.parametrize("quantity", [0, -1])
def test_plan_rejects_non_positive_quantity(quantity: int) -> None:
    with pytest.raises(ValidationError, match="引当数量"):
        plan_fifo_allocations([_lot(1, "2026-01-01 00:00:00", 2, 10)], quantity)
