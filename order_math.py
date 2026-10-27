"""
Exact order totals. The AI never does arithmetic on money - measured over and over, models add
up prices wrongly or invent a figure - so when a customer asks "total for 3 green tea and 2
cinnamon?" the CODE works it out from the business's live product list, and the AI only reads
the verified result out to the customer.

    "3 green tea and 2 cinnamon, total?"
        -> lines  [3 x Green Tea 100g @ 780 = 2,340,  2 x Cinnamon Sticks 100g @ 650 = 1,300]
        -> items total 3,640 (verified), shortages noted (asked for 10 cardamom, 6 in stock)

What is NOT added up: delivery. Fees are written in the knowledge base as prose ("Rs 350 in
Colombo, Rs 450 elsewhere, free over Rs 5,000") and depend on the address, so the bot quotes the
fee that applies and says the delivery is added - it does not sum it.

Pieces:
  asks_total()      does this message ask for a total / how much for these?
  find_lines()      which products and how many: a fast model reads the chat against the real
                    catalogue names (it can only answer with those names), with a plain-text
                    fallback when no model is available
  compute_quote()   the arithmetic, with Decimal
  quote_prompt()    the verified block the reply model must use
"""

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

import llm_router

logger = logging.getLogger("uvicorn.error")

MAX_QUANTITY = 10_000


@dataclass
class CatalogItem:
    name: str
    price: float
    stock: int = 0


@dataclass
class QuoteLine:
    name: str
    wanted: int
    quantity: int            # what the total uses: capped at the stock
    unit_price: Decimal
    line_total: Decimal
    short_by: int = 0        # wanted - quantity


@dataclass
class Quote:
    lines: List[QuoteLine] = field(default_factory=list)
    subtotal: Decimal = Decimal(0)
    currency: str = ""

    @property
    def has_shortage(self) -> bool:
        return any(l.short_by for l in self.lines)


# --- Does the customer want a total? ------------------------------------------------------

_ASKS_TOTAL = re.compile(
    r"\b(total|altogether|all together|in all|in total|sum|grand total|how much (would|will) (it|that|they|this|these|all)|"
    r"how much for (all|these|those|them|everything|the order)|how much (it|that|they|this|these) (is|are|will be|would be)|"
    r"what('s| is| would be| will be) (the )?(total|bill|amount|cost for (all|these|them))|"
    r"price for (all|these|them)|cost for (all|these|them))\b"
    r"|mulu (mudala|ganan)|ekathuwa|ekatuwa|mulu ganan|kiyada (wenne|wei|ekata)|ekata kiyada|ekkata kiyada"
    r"|මුළු (මුදල|ගාන)|එකතුව|ඔක්කොම කීයද",
    re.I,
)
_HAS_QUANTITY = re.compile(r"\b\d{1,4}\s*(x|×|packs?|pcs|pieces?|boxes|bottles|units|kg|g)?\b|\b(one|two|three|four|five|six|seven|eight|nine|ten|dozen)\b", re.I)


def asks_total(message: str) -> bool:
    return bool(_ASKS_TOTAL.search(message or ""))


# --- Which products, how many ----------------------------------------------------------------

_GENERIC = {"the", "a", "an", "of", "and", "pack", "packs", "pcs", "piece", "pieces", "box", "boxes", "bottle", "bottles",
            "unit", "units", "g", "kg", "ml", "l", "gram", "grams", "rs", "lkr"}


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z]+", (text or "").lower()) if t not in _GENERIC]


def match_product(phrase: str, catalog: List[CatalogItem]) -> Optional[CatalogItem]:
    """The catalogue item a customer's words refer to - only when exactly one fits best."""
    words = set(_tokens(phrase))
    if not words:
        return None
    candidates = []
    for item in catalog:
        # "Gift box (tea + cinnamon)": what's in brackets describes the contents, it isn't what people call it.
        item_words = set(_tokens(re.sub(r"\([^)]*\)", " ", item.name)))
        if item_words and (item_words <= words or words <= item_words):
            candidates.append((item, item_words))
    if not candidates:
        return None
    exact = [item for item, item_words in candidates if item_words == words]
    if len(exact) == 1:
        return exact[0]
    # "tea" fits Green Tea, Black Tea and the gift box: that's a question, not a guess.
    # But "ceylon black tea 200g" fully names one product even though others share words.
    inside = [item for item, item_words in candidates if item_words <= words]
    if len(inside) == 1:
        return inside[0]
    if len(candidates) == 1:
        return candidates[0][0]
    # Several products contain these words: take the closest one only if it is clearly closer than the
    # runner-up ("cinnamon" is 1 word from Cinnamon Sticks, 3 from the gift box; "tea" is 1 from Green
    # Tea and 2 from Black Tea - too close to call, so ask).
    ranked = sorted(((len(item_words - words), item) for item, item_words in candidates), key=lambda r: r[0])
    return ranked[0][1] if ranked[1][0] - ranked[0][0] >= 2 else None


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "a dozen": 12, "dozen": 12}
_PHRASE = re.compile(
    r"(?P<q>\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten|a dozen|dozen)\s*(?:x|×)?\s*"
    r"(?:packs?|pcs|pieces?|boxes|bottles|units)?\s*(?:of\s+)?(?P<p>[a-z][a-z\- ]{1,40}?)(?=[ \t]*(?:,|\.|;|\?|!|\n|\band\b|\bplus\b|\bwith\b|\bfor\b|\bto\b|\d|$))",
    re.I,
)


def fallback_lines(text: str, catalog: List[CatalogItem]) -> Dict[str, int]:
    """Quantities and products read from plain text, with no AI: '3 green tea and 2 cinnamon'."""
    found: Dict[str, int] = {}
    for m in _PHRASE.finditer(text or ""):
        q = m.group("q").lower()
        quantity = int(q) if q.isdigit() else _NUMBER_WORDS.get(q, 0)
        item = match_product(m.group("p"), catalog)
        if item and 0 < quantity <= MAX_QUANTITY:
            found[item.name] = quantity                   # a later mention replaces an earlier one
    return found


LINES_PROMPT = (
    "A customer is talking to a shop. Work out which products they want to buy and how many.\n"
    "Answer with JSON only: {\"items\": [{\"product\": \"<exact name from the catalogue>\", \"quantity\": <whole number>}]}\n"
    "Rules:\n"
    "- Use ONLY names from the catalogue, exactly as written. If you can't tell which product they mean, leave it out.\n"
    "- Only things they want to buy (want, need, take, order, add, 'give me', or a quantity next to a product). "
    "Not things they only asked about.\n"
    "- If they changed their mind, the latest quantity wins. If a product was removed, leave it out.\n"
    "- Quantities in words count ('two' = 2). A message may be English, Sinhala or Singlish.\n"
    "- If they want nothing specific yet, answer {\"items\": []}."
)


# "also 2 more", "add a gift box", "plus 3 tea": the customer is adding to what they already asked for.
_ADDS_TO_BASKET = re.compile(
    r"\b(also|add|adding|plus|more|another|extra|in addition|along with|as well|too|and the (earlier|previous|rest)|"
    r"everything|all of (it|them|that)|whole (order|basket|bundle)|so far|together with)\b|thawa|ethakota|තව", re.I)


async def find_lines(chat: List[Dict[str, str]], message: str, catalog: List[CatalogItem]) -> Dict[str, int]:
    """
    {catalogue name: quantity} for what the customer wants to know the total of.

    If this message itself names products with quantities ("2 black tea and 1 gift box, total for those?")
    that is what is added up - unless they are adding to an earlier order ("also...", "plus...", "everything").
    A bare "total?" adds up what they asked for earlier in the chat.
    """
    own = await _extract(message, catalog)
    if own and not _ADDS_TO_BASKET.search(message or ""):
        return own
    recent = [m for m in chat[-8:] if m.get("role") == "user"] + [{"role": "user", "content": message}]
    return await _extract("\n".join(m.get("content", "") for m in recent), catalog)


async def _extract(text: str, catalog: List[CatalogItem]) -> Dict[str, int]:
    """Products and quantities in `text`: a fast model against the real names, else plain-text matching."""
    names = {c.name.lower(): c.name for c in catalog}
    if llm_router.role_candidates("fast"):
        user = ("Catalogue:\n" + "\n".join(f"- {c.name}" for c in catalog) +
                "\n\nWhat the customer wrote, oldest first:\n" + text)
        try:
            data, _ = await llm_router.complete_json(
                "fast", [{"role": "system", "content": LINES_PROMPT}, {"role": "user", "content": user}],
                max_tokens=300, temperature=0.0, timeout=25)
            out: Dict[str, int] = {}
            for entry in (data.get("items") if isinstance(data, dict) else None) or []:
                name = names.get(str((entry or {}).get("product", "")).strip().lower())
                try:
                    quantity = int(entry.get("quantity"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if name and 0 < quantity <= MAX_QUANTITY:
                    out[name] = quantity
            if out:
                return out
        except Exception as exc:  # noqa: BLE001 - never let a failed lookup break the reply
            logger.info(f"[order-math] model lines failed: {exc}")
    return fallback_lines(text, catalog)


# --- The arithmetic -----------------------------------------------------------------------------------

def compute_quote(lines: Dict[str, int], catalog: List[CatalogItem], currency: str = "") -> Optional[Quote]:
    by_name = {c.name: c for c in catalog}
    quote = Quote(currency=currency)
    for name, wanted in lines.items():
        item = by_name.get(name)
        if item is None or wanted <= 0:
            continue
        quantity = min(wanted, max(item.stock, 0)) if item.stock is not None else wanted
        unit = Decimal(str(item.price))
        total = unit * quantity
        quote.lines.append(QuoteLine(name, wanted, quantity, unit, total, short_by=wanted - quantity))
        quote.subtotal += total
    return quote if quote.lines else None


def money(value: Decimal) -> str:
    value = Decimal(value)
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"


def quote_text(quote: Quote) -> str:
    """The quote as plain text. Every figure here is allowed to appear in the reply."""
    cur = f"{quote.currency} " if quote.currency else ""
    out = []
    for l in quote.lines:
        if l.quantity == 0:
            out.append(f"- {l.name}: wanted {l.wanted}, OUT OF STOCK")
            continue
        line = f"- {l.quantity} x {l.name} @ {cur}{money(l.unit_price)} = {cur}{money(l.line_total)}"
        if l.short_by:
            line += f"  (they wanted {l.wanted}, only {l.quantity} in stock)"
        out.append(line)
    out.append(f"Items total: {cur}{money(quote.subtotal)}")
    return "\n".join(out)


def quote_prompt(quote: Quote) -> str:
    return (
        "--- VERIFIED CALCULATION (worked out by the system from the live product list - exact) ---\n"
        f"{quote_text(quote)}\n"
        "Use these figures exactly as written and show the customer the lines and the items total. "
        "This total is for the items only. Delivery is NOT included: say which delivery fee from the knowledge "
        "base applies to their area (or ask the area if you don't know it) and that it is added to the items "
        "total. Do not add the delivery fee to the total yourself and do not do any other arithmetic.\n"
        + ("Some items are short of stock: say so plainly and let them choose.\n" if quote.has_shortage else "")
        + "------------------------------------------\n"
    )


async def build_quote(chat: List[Dict[str, str]], message: str, catalog: List[CatalogItem], currency: str = "") -> Optional[Quote]:
    """A quote for what the customer is asking the total of, or None when this isn't that kind of message."""
    if not catalog or not asks_total(message):
        return None
    lines = await find_lines(chat, message, catalog)
    return compute_quote(lines, catalog, currency) if lines else None
