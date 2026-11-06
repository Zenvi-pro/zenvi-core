"""Buying credits from the Zenvi Assistant (#272).

The tool quotes a top-up pack and shows a card in the chat dock. The charge
happens only when the user presses Pay on that card; outside the app (MCP) the
user confirms on zenvi.pro/topup.
"""

from classes.credits_client import credits, topup_url
from classes.editor_tools._base import enum, fail, get_app, obj, ok, on_main
from classes.editor_tools._registry import editor_tool
from classes.logger import log


def _show_card(quote: dict) -> bool:
    """True when the chat dock rendered the card (not headless, not mid-charge, web chat)."""
    def show():
        dock = getattr(getattr(get_app(), "window", None), "dockAIChat", None)
        return bool(dock is not None and dock.show_topup_card(quote))
    try:
        return bool(on_main(show))
    except Exception as exc:  # no editor window (headless MCP): the link is enough
        log.debug("buy_credits_tool: no chat dock to show the card: %s", exc)
        return False


@editor_tool(
    "buy_credits_tool",
    label="Buy credits",
    schema=obj({
        "pack": enum([5, 10, 15], "Top-up pack in dollars: 5 = 500 credits, 10 = 1,000, 15 = 1,500.", 10),
    }),
    background_safe=True,
    covers=("ai.credits",),
)
def buy_credits_tool(pack: int = 10) -> str:
    """Add Zenvi credits when the user asks to top up or a step failed for lack of credits.

    Shows a top-up card in the Zenvi chat with the price and how it is paid (the card on
    the user's subscription, or Stripe Checkout). Nothing is charged until the user presses
    Pay on that card or confirms at the returned link. Never claim the credits were added.
    """
    quote = credits.buy(int(pack))
    if quote.get("error"):
        return fail(quote["error"])
    shown = _show_card(quote)
    amount = "%s$%s" % ("CA" if quote.get("currency") == "cad" else "", quote.get("amount", pack))
    how = ("charged to %s" % quote["card"]) if quote.get("mode") == "card" and quote.get("card") else "paid on Stripe Checkout"
    credits_text = format(int(quote.get("credits", pack * 100)), ",")
    if not shown:
        return ok(
            "%s = %s credits, %s. Give the user this link to buy them; nothing is charged until they "
            "confirm there: %s" % (amount, credits_text, how, topup_url(int(pack))),
            pack=int(pack), mode=quote.get("mode"), card_shown=False,
        )
    return ok(
        "Showed the top-up card in the Zenvi chat: %s = %s credits, %s when the user presses Pay. "
        "Nothing is charged until they do. Outside the app they can buy at %s."
        % (amount, credits_text, how, topup_url(int(pack))),
        pack=int(pack), mode=quote.get("mode"), card_shown=True,
    )
