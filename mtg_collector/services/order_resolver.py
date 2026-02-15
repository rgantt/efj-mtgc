"""Order resolution: match parsed order items to Scryfall cards."""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from mtg_collector.db.models import CollectionEntry, Order
from mtg_collector.services.order_parser import ParsedOrder, ParsedOrderItem
from mtg_collector.services.scryfall import ScryfallAPI, cache_scryfall_data
from mtg_collector.utils import normalize_condition, normalize_finish, now_iso

# Vendor set name → Scryfall set code
# Covers common TCGPlayer set names that don't directly match Scryfall
SET_NAME_MAP = {
    "FINAL FANTASY": "fin",
    "FINAL FANTASY: THROUGH THE AGES": "fca",
}

# Set hints that indicate non-MTG products — skip resolution entirely
NON_MTG_PREFIXES = ("SV", "ME:", "INTO THE INKLANDS")


@dataclass
class ResolvedItem:
    """A parsed order item matched to a Scryfall card."""
    parsed: ParsedOrderItem
    scryfall_id: Optional[str] = None
    card_name: Optional[str] = None  # resolved name from Scryfall
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    image_uri: Optional[str] = None
    error: Optional[str] = None
    linked_collection_id: Optional[int] = None  # if matched to existing entry


@dataclass
class ResolvedOrder:
    """A parsed order with all items resolved."""
    parsed: ParsedOrder
    items: List[ResolvedItem] = field(default_factory=list)


def resolve_orders(
    orders: List[ParsedOrder],
    scryfall: ScryfallAPI,
    card_repo,
    set_repo,
    printing_repo,
    conn,
) -> List[ResolvedOrder]:
    """Resolve parsed orders by matching items to Scryfall cards."""
    resolved = []
    for order in orders:
        resolved_order = ResolvedOrder(parsed=order)
        for item in order.items:
            resolved_item = _resolve_item(
                item, scryfall, card_repo, set_repo, printing_repo, conn
            )
            resolved_order.items.append(resolved_item)
        resolved.append(resolved_order)
    return resolved


def _resolve_set_code(set_hint: Optional[str], set_repo) -> Optional[str]:
    """Map a vendor set name to a Scryfall set code using local DB only."""
    if not set_hint:
        return None

    # Check hardcoded map first
    if set_hint.upper() in SET_NAME_MAP:
        return SET_NAME_MAP[set_hint.upper()]

    # Try local DB — check if it's already a valid set code
    s = set_repo.get(set_hint.lower())
    if s:
        return s.set_code

    # Try local DB — search by set name
    s = set_repo.get_by_name(set_hint)
    if s:
        return s.set_code

    return None


def _is_non_mtg(set_hint: Optional[str]) -> bool:
    """Check if a set hint indicates a non-MTG product (Pokemon, Lorcana, etc.)."""
    if not set_hint:
        return False
    upper = set_hint.upper()
    return any(upper.startswith(prefix) for prefix in NON_MTG_PREFIXES)


def _printing_matches_treatment(p, treatment: Optional[str]) -> bool:
    """Check if a printing matches the expected treatment (borderless, extended art, etc.)."""
    fe = p.frame_effects if isinstance(p.frame_effects, list) else []
    if isinstance(p.frame_effects, str):
        import json
        try:
            fe = json.loads(p.frame_effects)
        except (json.JSONDecodeError, TypeError):
            fe = []

    if treatment:
        t = treatment.lower()
        if "borderless" in t:
            return p.border_color == "borderless"
        if "extended" in t:
            return "extendedart" in fe
        if "showcase" in t:
            return "showcase" in fe
        # Unknown treatment — don't filter
        return True

    # No treatment = regular printing (black border, no borderless/extendedart)
    return p.border_color != "borderless" and "extendedart" not in fe


def _find_card_local(
    card_name: str, card_repo, printing_repo,
    set_code: Optional[str], treatment: Optional[str] = None,
) -> Optional[ResolvedItem]:
    """Try to find a card in the local DB. Returns ResolvedItem or None."""
    card = card_repo.get_by_name(card_name) or card_repo.search_by_name(card_name)
    if not card:
        return None

    printings = printing_repo.get_by_oracle_id(card.oracle_id)
    if not printings:
        return None

    def _make_result(p):
        return ResolvedItem(
            parsed=None,  # caller sets this
            scryfall_id=p.scryfall_id,
            card_name=card.name,
            set_code=p.set_code,
            collector_number=p.collector_number,
            image_uri=p.image_uri,
        )

    # If we have a set code, prefer a printing from that set + matching treatment
    if set_code:
        # Best: set + treatment match
        for p in printings:
            if p.set_code == set_code and _printing_matches_treatment(p, treatment):
                return _make_result(p)
        # Fallback: set match only
        for p in printings:
            if p.set_code == set_code:
                return _make_result(p)

    # Use any available printing
    p = printings[0]
    return _make_result(p)


def _resolve_item(
    item: ParsedOrderItem,
    scryfall: ScryfallAPI,
    card_repo,
    set_repo,
    printing_repo,
    conn,
) -> ResolvedItem:
    """Resolve a single order item to a Scryfall card.

    Uses local DB lookups first (microseconds). Only falls back to
    Scryfall API if card is not found locally.
    """
    resolved = ResolvedItem(parsed=item)

    # Skip non-MTG products immediately
    if _is_non_mtg(item.set_hint):
        resolved.error = f"Non-MTG product: {item.card_name}"
        return resolved

    # Clean card name — remove collector number suffixes like "(0393)"
    card_name = item.card_name
    card_name = re.sub(r'\s*\(\d+\)\s*$', '', card_name)
    # Remove collector number suffixes like "- 065/182"
    card_name = re.sub(r'\s*-\s*\d+/\d+\s*$', '', card_name)
    # Remove collector number suffixes like "- 026"
    card_name = re.sub(r'\s*-\s*\d+\s*$', '', card_name)

    set_code = _resolve_set_code(item.set_hint, set_repo)

    treatment = item.treatment

    # Try local DB — full name
    result = _find_card_local(card_name, card_repo, printing_repo, set_code, treatment)
    if result:
        result.parsed = item
        return result

    # For names with " - " (e.g. "FF Name - MTG Name"), try just the MTG part
    if " - " in card_name:
        mtg_name = card_name.split(" - ", 1)[1].strip()
        result = _find_card_local(mtg_name, card_repo, printing_repo, set_code, treatment)
        if result:
            result.parsed = item
            return result

    # Fall back to Scryfall API (card not in local cache)
    results = scryfall.search_card(card_name, set_code=set_code)

    if not results:
        results = scryfall.search_card(card_name)

    if not results:
        resolved.error = f"Card not found: {card_name}"
        return resolved

    # Pick the best match
    card_data = results[0]

    if set_code:
        for r in results:
            if r.get("set") == set_code:
                card_data = r
                break

    if card_data.get("object") != "card":
        resolved.error = f"Not an MTG card: {card_name}"
        return resolved

    # Cache the card data locally
    if "oracle_id" in card_data:
        cache_scryfall_data(scryfall, card_repo, set_repo, printing_repo, card_data)
        conn.commit()

    image_uri = None
    if "image_uris" in card_data:
        image_uri = card_data["image_uris"].get("small") or card_data["image_uris"].get("normal")
    elif "card_faces" in card_data and card_data["card_faces"]:
        face = card_data["card_faces"][0]
        if "image_uris" in face:
            image_uri = face["image_uris"].get("small") or face["image_uris"].get("normal")

    resolved.scryfall_id = card_data["id"]
    resolved.card_name = card_data.get("name", card_name)
    resolved.set_code = card_data.get("set")
    resolved.collector_number = card_data.get("collector_number")
    resolved.image_uri = image_uri

    return resolved


def commit_orders(
    resolved_orders: List[ResolvedOrder],
    order_repo,
    collection_repo,
    conn,
    status: str = "ordered",
    source: str = "order_import",
) -> Dict:
    """Commit resolved orders to the database.

    Creates order records and collection entries (or links existing ones).

    Returns summary dict with counts.
    """
    summary = {
        "orders_created": 0,
        "orders_skipped": 0,
        "cards_added": 0,
        "cards_linked": 0,
        "cards_skipped": 0,
        "errors": [],
    }

    ts = now_iso()

    for resolved in resolved_orders:
        parsed = resolved.parsed

        # Idempotency: skip if order_number + seller already exists
        existing_orders = order_repo.get_by_number(parsed.order_number)
        already_exists = any(
            o.seller_name == parsed.seller_name for o in existing_orders
        )
        if already_exists:
            summary["orders_skipped"] += 1
            continue

        # Create order record
        order = Order(
            id=None,
            order_number=parsed.order_number,
            source=parsed.source,
            seller_name=parsed.seller_name,
            order_date=parsed.order_date,
            subtotal=parsed.subtotal,
            shipping=parsed.shipping,
            tax=parsed.tax,
            total=parsed.total,
            shipping_status=parsed.shipping_status,
            estimated_delivery=parsed.estimated_delivery,
            created_at=ts,
        )
        order_id = order_repo.add(order)
        summary["orders_created"] += 1

        for item in resolved.items:
            if not item.scryfall_id:
                summary["errors"].append(item.error or f"Unresolved: {item.parsed.card_name}")
                continue

            for _ in range(item.parsed.quantity):
                # Check for existing unlinked ordered card with same scryfall_id
                existing = _find_existing_unlinked(
                    conn, item.scryfall_id, status
                )

                if existing:
                    # Link existing entry to this order
                    conn.execute(
                        "UPDATE collection SET order_id = ? WHERE id = ?",
                        (order_id, existing),
                    )
                    summary["cards_linked"] += 1
                else:
                    # Create new collection entry
                    finish = normalize_finish("foil" if item.parsed.foil else "nonfoil")
                    condition = normalize_condition(item.parsed.condition)
                    entry = CollectionEntry(
                        id=None,
                        scryfall_id=item.scryfall_id,
                        finish=finish,
                        condition=condition,
                        purchase_price=item.parsed.price,
                        source=source,
                        status=status,
                        order_id=order_id,
                        acquired_at=ts,
                    )
                    collection_repo.add(entry)
                    summary["cards_added"] += 1

    conn.commit()
    return summary


def _find_existing_unlinked(
    conn, scryfall_id: str, status: str
) -> Optional[int]:
    """Find an existing collection entry with matching scryfall_id, status, and no order_id."""
    cursor = conn.execute(
        "SELECT id FROM collection WHERE scryfall_id = ? AND status = ? AND order_id IS NULL LIMIT 1",
        (scryfall_id, status),
    )
    row = cursor.fetchone()
    return row["id"] if row else None
