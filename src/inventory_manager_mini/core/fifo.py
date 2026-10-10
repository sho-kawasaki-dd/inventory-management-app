from collections.abc import Sequence

from inventory_manager_mini.core.errors import NegativeStockError, ValidationError
from inventory_manager_mini.core.models import Allocation, FifoEstimate, Lot


def plan_fifo_allocations(
    lots: Sequence[Lot], quantity: int
) -> tuple[tuple[Allocation, ...], FifoEstimate]:
    if quantity <= 0:
        raise ValidationError("引当数量は1以上で指定してください")

    available_quantity = sum(lot.remaining_quantity for lot in lots if lot.remaining_quantity > 0)
    if quantity > available_quantity:
        raise NegativeStockError("在庫が不足しています")

    remaining_quantity = quantity
    cost_amount = 0
    unpriced_quantity = 0
    allocations: list[Allocation] = []

    for lot in sorted(lots, key=lambda candidate: (candidate.moved_at, candidate.id)):
        if lot.remaining_quantity <= 0:
            continue
        allocated_quantity = min(remaining_quantity, lot.remaining_quantity)
        allocations.append(Allocation(lot_id=lot.id, quantity=allocated_quantity))
        if lot.unit_price is None:
            unpriced_quantity += allocated_quantity
        else:
            cost_amount += allocated_quantity * lot.unit_price
        remaining_quantity -= allocated_quantity
        if remaining_quantity == 0:
            break

    return tuple(allocations), FifoEstimate(
        cost_amount=cost_amount,
        unpriced_quantity=unpriced_quantity,
    )
