from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from inventory_app.db import Base


class Item(Base):
    __tablename__ = "items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    sku: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False, default="pcs")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    stock: Mapped["Stock"] = relationship(back_populates="item", uselist=False)


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (UniqueConstraint("item_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False)

    quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False, server_default="0")
    shelf_location: Mapped[str | None] = mapped_column(String, nullable=True)
    shelf_location_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    item: Mapped[Item] = relationship(back_populates="stock")


class Stocktake(Base):
    __tablename__ = "stocktakes"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["StocktakeLine"]] = relationship(
        back_populates="stocktake", cascade="all, delete-orphan"
    )


class StocktakeLine(Base):
    __tablename__ = "stocktake_lines"
    __table_args__ = (UniqueConstraint("stocktake_id", "item_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stocktake_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("stocktakes.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False)

    expected_quantity: Mapped[float] = mapped_column(Numeric(14, 3), nullable=False, server_default="0")
    counted_quantity: Mapped[float | None] = mapped_column(Numeric(14, 3), nullable=True)

    shelf_location: Mapped[str | None] = mapped_column(String, nullable=True)
    shelf_location_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    stocktake: Mapped[Stocktake] = relationship(back_populates="lines")
    item: Mapped[Item] = relationship()
