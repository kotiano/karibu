"""What a dish consumes from stock.

THE ENTRY QUESTION IS "HOW MANY PLATES", THE STORED FACT IS "HOW MUCH PER
PLATE". An owner thinks in yield — "this 10kg of meat gives 40 plates" — and
that is the right thing to ask, because it is the number they actually know.
What has to be stored is the consumption per sale, 250g, because that is what a
single order deducts. The conversion happens once, at the boundary.

WHY THIS IS PER (DISH, INGREDIENT) AND NOT A FIELD ON THE STOCK ITEM. One sack
of meat feeds Nyama Choma and Beef Stew at different portions. A single
"portions per kg" on the item would force both dishes to the same size, and the
first time they differ the figure quietly stops meaning anything.
"""
from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel


class RecipeLine(BaseModel):
    __tablename__ = "recipe_lines"

    menu_item_id: Mapped[str] = mapped_column(
        ForeignKey("menu_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_item_id: Mapped[str] = mapped_column(
        ForeignKey("stock_items.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Thousandths of a stock unit consumed by ONE sale — the same integer
    # discipline the rest of stock uses. 250g of a kg-denominated item is 250.
    quantity_milli: Mapped[int] = mapped_column(Integer, nullable=False)

    menu_item: Mapped["MenuItem"] = relationship()      # noqa: F821
    stock_item: Mapped["StockItem"] = relationship()    # noqa: F821

    __table_args__ = (
        UniqueConstraint("menu_item_id", "stock_item_id", name="uq_recipe_dish_item"),
    )

    @property
    def quantity(self) -> float:
        return self.quantity_milli / 1000
