"""Order parsing for TCGPlayer and Card Kingdom orders."""

import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ParsedOrderItem:
    card_name: str
    set_hint: Optional[str] = None      # vendor set name ("FINAL FANTASY")
    condition: str = "Near Mint"
    foil: bool = False
    quantity: int = 1
    price: Optional[float] = None       # unit price from order
    treatment: Optional[str] = None     # "Borderless", "Showcase", etc.
    rarity_hint: Optional[str] = None   # raw rarity from order ("R", "M", "ACE SPEC Rare")


@dataclass
class ParsedOrder:
    order_number: Optional[str] = None
    source: str = "tcgplayer"
    seller_name: Optional[str] = None
    order_date: Optional[str] = None
    subtotal: Optional[float] = None
    shipping: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    shipping_status: Optional[str] = None
    estimated_delivery: Optional[str] = None
    items: List[ParsedOrderItem] = field(default_factory=list)


def detect_order_format(text: str) -> str:
    """Detect the format of order text.

    Returns: 'tcg_html', 'tcg_text', or 'ck_text'
    """
    if "<" in text and ("orderWrap" in text or "start-tag" in text):
        return "tcg_html"
    # TCG text format: tab-separated lines with "Magic -" pattern
    if "\t" in text and "Magic" in text:
        return "tcg_text"
    return "ck_text"


def parse_order(text: str, format: Optional[str] = None) -> List[ParsedOrder]:
    """Parse order text into structured order data.

    Args:
        text: Raw order text (HTML or plain text)
        format: Force format ('tcg_text', 'tcg_html', 'ck_text') or None for auto-detect

    Returns:
        List of ParsedOrder objects (one per seller/sub-order)
    """
    if format is None:
        format = detect_order_format(text)

    if format == "tcg_html":
        return _parse_tcg_html(text)
    elif format == "tcg_text":
        return _parse_tcg_text(text)
    elif format == "ck_text":
        return _parse_ck_text(text)
    else:
        raise ValueError(f"Unknown order format: {format}")


def _parse_dollar(text: str) -> Optional[float]:
    """Parse a dollar amount string like '$0.60' into a float."""
    text = text.strip()
    m = re.search(r'\$?([\d,]+\.?\d*)', text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _parse_condition_and_foil(raw: str) -> tuple[str, bool]:
    """Parse a condition string that may include foil info.

    Examples:
        "Near Mint" -> ("Near Mint", False)
        "Near Mint Foil" -> ("Near Mint", True)
        "Lightly Played Holofoil" -> ("Lightly Played", True)
        "Near Mint Holofoil" -> ("Near Mint", True)
    """
    raw = raw.strip()
    foil = False

    # Check for foil suffixes
    for suffix in ("Holofoil", "Foil"):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)].strip()
            foil = True
            break

    # Normalize condition
    from mtg_collector.utils import normalize_condition
    condition = normalize_condition(raw)

    return condition, foil


def _extract_treatment(card_name: str) -> tuple[str, Optional[str]]:
    """Extract treatment info from card name parenthetical.

    Examples:
        "Elesh Norn (Borderless)" -> ("Elesh Norn", "Borderless")
        "Lightning Bolt" -> ("Lightning Bolt", None)
        "Fblthp (Showcase)" -> ("Fblthp", "Showcase")
    """
    m = re.match(r'^(.+?)\s*\(([^)]+)\)\s*$', card_name)
    if m:
        name = m.group(1).strip()
        paren = m.group(2).strip()
        treatments = {"Borderless", "Showcase", "Extended Art", "Full Art",
                       "Retro Frame", "Surge Foil", "Confetti Foil",
                       "Galaxy Foil", "Textured Foil", "Serialized"}
        for t in treatments:
            if t.lower() in paren.lower():
                return name, paren
        # Not a treatment — could be "(Phyrexian)" etc., keep as part of name
        return card_name, None
    return card_name, None


# ── TCGPlayer HTML ──

def _parse_tcg_html(html: str) -> List[ParsedOrder]:
    """Parse TCGPlayer order history HTML (handles view-source and normal saves)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Detect Firefox view-source wrapper
    lines = soup.find_all("span", id=re.compile(r"^line\d+"))
    if lines:
        raw_html = "\n".join(line.get_text() for line in lines)
        soup = BeautifulSoup(raw_html, "html.parser")

    wraps = soup.find_all("div", class_="orderWrap")
    orders = []

    for wrap in wraps:
        order = _parse_tcg_html_wrap(wrap)
        if order and order.items:
            orders.append(order)

    return orders


def _parse_tcg_html_wrap(wrap) -> Optional[ParsedOrder]:
    """Parse a single orderWrap div into a ParsedOrder."""
    order = ParsedOrder(source="tcgplayer")

    # ── Header: order date, order number ──
    header = wrap.find("div", class_="orderHeader")
    if header:
        date_span = header.find("span", attrs={"data-aid": "spn-sellerorderwidget-orderdate"})
        if date_span:
            order.order_date = date_span.get_text(strip=True)

        # Order number: look for "Order Number" title or "TCGPLAYER DIRECT #"
        header_spans = header.find_all("span", recursive=False)
        for span in header_spans:
            text = span.get_text(separator="|", strip=True)
            if "Order Number" in text:
                parts = text.split("|")
                if len(parts) >= 2:
                    order.order_number = parts[-1].strip()
            elif "TCGPLAYER DIRECT" in text:
                # Format: "TCGPLAYER DIRECT # | 260214-3ADF"
                parts = text.split("|")
                if len(parts) >= 2:
                    order.order_number = parts[-1].strip()

    # ── Seller name + shipping info ──
    vendor_span = wrap.find("span", attrs={"data-aid": "spn-sellerorderwidget-vendorname"})
    if vendor_span:
        order.seller_name = vendor_span.get_text(strip=True)

    # Check for TCGplayer Direct
    if not order.seller_name:
        shipped_spans = wrap.find_all("span", class_="orderSummary")
        for s in shipped_spans:
            text = s.get_text()
            if "SHIPPED BY" in text and "TCGplayer Direct" in text:
                order.seller_name = "TCGplayer Direct"
                break

    # Shipping status
    tracking = wrap.find("span", attrs={"data-aid": "spn-sellerorderwidget-trackingnumber"})
    if tracking:
        order.shipping_status = tracking.get_text(strip=True)

    # Estimated delivery
    shipped_spans = wrap.find_all("span", class_="orderSummary")
    for s in shipped_spans:
        text = s.get_text()
        m = re.search(r'est\.?\s*delivery by ([^)]+)\)', text)
        if m:
            order.estimated_delivery = m.group(1).strip()
            break

    # ── Financial summary ──
    summary = wrap.find("table", attrs={"data-aid": "tbl-sellerorderwidget-productsinorder"})
    if summary:
        rows = summary.find_all("tr")
        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 2:
                continue
            label = tds[0].get_text(strip=True).lower()
            value = _parse_dollar(tds[1].get_text(strip=True))
            if "subtotal" in label:
                order.subtotal = value
            elif "shipping" in label:
                order.shipping = value
            elif "tax" in label:
                order.tax = value
            elif "total" in label:
                order.total = value

    # ── Item rows ──
    table = wrap.find("table", attrs={"data-aid": "tbl-sellerorderwidget-ordertable"})
    if not table:
        return order

    item_rows = table.find_all("tr", class_=re.compile(r"tr(Odd|Even)"))
    for row in item_rows:
        item = _parse_tcg_html_item_row(row)
        if item:
            order.items.append(item)

    return order


def _parse_tcg_html_item_row(row) -> Optional[ParsedOrderItem]:
    """Parse a single item row from TCGPlayer order table."""
    # Card name and set
    name_td = row.find("td", class_="orderHistoryItems")
    if not name_td:
        return None

    link = name_td.find("a")
    if not link:
        return None
    card_name = link.get_text(strip=True)

    # Set name: text after the link in the span
    set_hint = None
    span = name_td.find("span", style=re.compile(r"padding-left"))
    if span:
        texts = list(span.stripped_strings)
        if len(texts) >= 2:
            set_hint = texts[-1]  # Last text is the set name

    # Details: rarity and condition
    detail_td = row.find("td", class_="orderHistoryDetail")
    condition = "Near Mint"
    foil = False
    rarity_hint = None
    if detail_td:
        detail_text = detail_td.get_text(strip=True)
        # Parse "Rarity: RCondition: Near Mint Foil"
        rarity_m = re.search(r'Rarity:\s*(.+?)Condition:', detail_text)
        if rarity_m:
            rarity_hint = rarity_m.group(1).strip()
        cond_m = re.search(r'Condition:\s*(.+)$', detail_text)
        if cond_m:
            condition, foil = _parse_condition_and_foil(cond_m.group(1))

    # Price
    price_td = row.find("td", class_="orderHistoryPrice")
    price = _parse_dollar(price_td.get_text(strip=True)) if price_td else None

    # Quantity
    qty_td = row.find("td", class_="orderHistoryQuantity")
    quantity = 1
    if qty_td:
        qty_text = qty_td.get_text(strip=True)
        if qty_text.isdigit():
            quantity = int(qty_text)

    # Extract treatment from card name
    card_name, treatment = _extract_treatment(card_name)

    return ParsedOrderItem(
        card_name=card_name,
        set_hint=set_hint,
        condition=condition,
        foil=foil,
        quantity=quantity,
        price=price,
        treatment=treatment,
        rarity_hint=rarity_hint,
    )


# ── TCGPlayer Text ──

def _parse_tcg_text(text: str) -> List[ParsedOrder]:
    """Parse TCGPlayer text format (tab-separated clipboard paste).

    Lines like:
    Magic - FINAL FANTASY - Jecht, Reluctant Guardian - Near Mint
    Magic - Foundations - Lightning Bolt - Lightly Played Foil
    """
    order = ParsedOrder(source="tcgplayer")

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        # Tab-separated or dash-separated
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = [p.strip() for p in line.split(" - ")]

        # Expected: Magic - SET - CARD NAME - CONDITION
        if len(parts) < 3:
            continue

        # First part should be "Magic" (skip it)
        if parts[0].lower() == "magic":
            parts = parts[1:]

        if len(parts) < 2:
            continue

        set_hint = parts[0]
        # Last part is condition
        condition_raw = parts[-1]
        # Everything in between is the card name
        card_name = " - ".join(parts[1:-1]) if len(parts) > 2 else parts[1]

        condition, foil = _parse_condition_and_foil(condition_raw)
        card_name, treatment = _extract_treatment(card_name)

        item = ParsedOrderItem(
            card_name=card_name,
            set_hint=set_hint,
            condition=condition,
            foil=foil,
            treatment=treatment,
        )

        # Handle quantity prefix (e.g., "2x Lightning Bolt" or "2 Lightning Bolt")
        qty_m = re.match(r'^(\d+)\s*x?\s+(.+)$', item.card_name)
        if qty_m:
            item.quantity = int(qty_m.group(1))
            item.card_name = qty_m.group(2)

        order.items.append(item)

    return [order] if order.items else []


# ── Card Kingdom Text ──

def _parse_ck_text(text: str) -> List[ParsedOrder]:
    """Parse Card Kingdom text format from order confirmation emails.

    Lines like:
    1x Lightning Bolt - Near Mint
    2x Counterspell [Foundations] - Lightly Played
    """
    order = ParsedOrder(source="cardkingdom")

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        # Parse "Nx Card Name [Set] - Condition"
        qty_m = re.match(r'^(\d+)\s*x?\s+(.+)$', line)
        quantity = 1
        rest = line
        if qty_m:
            quantity = int(qty_m.group(1))
            rest = qty_m.group(2)

        # Split on last " - " for condition
        parts = rest.rsplit(" - ", 1)
        if len(parts) < 2:
            continue

        card_part = parts[0].strip()
        condition_raw = parts[1].strip()

        # Extract set hint from brackets
        set_hint = None
        set_m = re.search(r'\[([^\]]+)\]', card_part)
        if set_m:
            set_hint = set_m.group(1)
            card_part = card_part[:set_m.start()].strip()

        condition, foil = _parse_condition_and_foil(condition_raw)
        card_part, treatment = _extract_treatment(card_part)

        order.items.append(ParsedOrderItem(
            card_name=card_part,
            set_hint=set_hint,
            condition=condition,
            foil=foil,
            quantity=quantity,
            treatment=treatment,
        ))

    return [order] if order.items else []
