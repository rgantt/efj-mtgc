"""Demo fixture data for new installations.

Provides ~50 curated cards across various sets, statuses, conditions, and finishes
so the web UI has realistic data to browse immediately after setup.
"""

import sqlite3

from mtg_collector.db.models import (
    CollectionEntry,
    CollectionRepository,
    Order,
    OrderRepository,
    WishlistEntry,
    WishlistRepository,
)
from mtg_collector.utils import now_iso

# Each tuple: (set_code, collector_number, finish, condition, status)
# status is one of: "owned", "ordered"
# Cards tagged "ordered" will be linked to a demo order.
DEMO_CARDS = [
    # --- Foundations (FDN) — commons/uncommons/rares ---
    ("fdn", "132", "nonfoil", "Near Mint", "owned"),         # Lightning Bolt
    ("fdn", "132", "foil", "Near Mint", "owned"),             # Lightning Bolt (foil dupe)
    ("fdn", "100", "nonfoil", "Near Mint", "owned"),          # Llanowar Elves
    ("fdn", "60", "nonfoil", "Near Mint", "owned"),           # Counterspell
    ("fdn", "34", "nonfoil", "Near Mint", "owned"),           # Day of Judgment
    ("fdn", "90", "nonfoil", "Near Mint", "owned"),           # Doom Blade
    ("fdn", "191", "nonfoil", "Near Mint", "owned"),          # Sol Ring
    ("fdn", "103", "nonfoil", "Lightly Played", "owned"),     # Naturalize

    # --- Duskmourn (DSK) — horror set variety ---
    ("dsk", "1", "nonfoil", "Near Mint", "owned"),            # Abhorrent Oculus
    ("dsk", "119", "nonfoil", "Near Mint", "owned"),          # Blazemaw Hellion
    ("dsk", "197", "foil", "Near Mint", "owned"),             # Enduring Courage
    ("dsk", "216", "nonfoil", "Moderately Played", "owned"),  # Overlord of the Hauntwoods
    ("dsk", "107", "nonfoil", "Near Mint", "owned"),          # Unholy Annex // Broken Concentration (DFC)
    ("dsk", "224", "nonfoil", "Near Mint", "owned"),          # Valgavoth, Terror Eater

    # --- Bloomburrow (BLB) — cute animal set ---
    ("blb", "198", "nonfoil", "Near Mint", "owned"),          # Innkeeper's Talent
    ("blb", "124", "nonfoil", "Near Mint", "owned"),          # Fireglass Mentor
    ("blb", "156", "nonfoil", "Near Mint", "owned"),          # Camellia, the Seedmiser
    ("blb", "215", "foil", "Near Mint", "owned"),             # Ygra, Eater of All

    # --- Outlaws of Thunder Junction (OTJ) ---
    ("otj", "178", "nonfoil", "Near Mint", "owned"),          # Bristly Bill, Spine Sower
    ("otj", "90", "nonfoil", "Near Mint", "owned"),           # Caustic Bronco
    ("otj", "196", "nonfoil", "Near Mint", "owned"),          # Obeka, Splitter of Seconds
    ("otj", "233", "foil", "Near Mint", "owned"),             # Worldwalker Helm

    # --- Modern Horizons 3 (MH3) ---
    ("mh3", "209", "nonfoil", "Near Mint", "owned"),          # Nadu, Winged Wisdom
    ("mh3", "293", "nonfoil", "Near Mint", "owned"),          # Flare of Denial
    ("mh3", "197", "nonfoil", "Lightly Played", "owned"),     # Emrakul, the World Anew
    ("mh3", "196", "etched", "Near Mint", "owned"),           # Eldrazi Conscription

    # --- Special Guests (SPG) — borderless promos ---
    ("spg", "74", "nonfoil", "Near Mint", "owned"),           # Rhystic Study

    # --- Wilds of Eldraine (WOE) ---
    ("woe", "56", "nonfoil", "Near Mint", "owned"),           # Beseech the Mirror
    ("woe", "171", "nonfoil", "Near Mint", "owned"),          # Virtue of Strength // Garenbrig Growth (DFC)

    # --- The Lost Caverns of Ixalan (LCI) ---
    ("lci", "68", "nonfoil", "Near Mint", "owned"),           # Deepfathom Echo
    ("lci", "113", "nonfoil", "Lightly Played", "owned"),     # Trumpeting Carnosaur

    # --- Murders at Karlov Manor (MKM) ---
    ("mkm", "172", "nonfoil", "Near Mint", "owned"),          # Leyline of the Guildpact
    ("mkm", "210", "foil", "Near Mint", "owned"),             # Teysa, Opulent Oligarch

    # --- Duplicate cards (same printing, different copies) ---
    ("fdn", "132", "nonfoil", "Lightly Played", "owned"),     # Lightning Bolt #3
    ("dsk", "119", "nonfoil", "Near Mint", "owned"),          # Blazemaw Hellion #2

    # === ORDERED cards (linked to demo orders) ===
    # Order 1: TCGPlayer order (received — these become "owned")
    ("fdn", "63", "nonfoil", "Near Mint", "owned"),           # Cryptic Command
    ("fdn", "166", "nonfoil", "Near Mint", "owned"),          # Cultivate
    ("fdn", "94", "foil", "Near Mint", "owned"),              # Dark Ritual
    ("fdn", "139", "nonfoil", "Near Mint", "owned"),          # Goblin Guide
    ("fdn", "168", "nonfoil", "Near Mint", "owned"),          # Elvish Mystic

    # Order 2: Card Kingdom order (pending — status=ordered)
    ("mh3", "174", "nonfoil", "Near Mint", "ordered"),        # Ajani, Nacatl Pariah // Ajani, Nacatl Avenger
    ("mh3", "295", "nonfoil", "Near Mint", "ordered"),        # Flare of Fortitude
    ("mh3", "234", "nonfoil", "Near Mint", "ordered"),        # Ugin's Labyrinth
    ("blb", "188", "foil", "Near Mint", "ordered"),           # Beza, the Bounding Spring
    ("dsk", "173", "nonfoil", "Near Mint", "ordered"),        # Fear of Missing Out
]

# Demo orders
DEMO_ORDERS = [
    {
        "order_number": "DEMO-TCG-001",
        "source": "tcgplayer",
        "seller_name": "CardHaus Gaming",
        "order_date": "2025-01-15",
        "subtotal": 24.97,
        "shipping": 2.99,
        "tax": 2.12,
        "total": 30.08,
        "shipping_status": "Delivered",
        "cards_slice": slice(35, 40),  # indices into DEMO_CARDS
    },
    {
        "order_number": "DEMO-CK-001",
        "source": "cardkingdom",
        "seller_name": "Card Kingdom",
        "order_date": "2025-02-01",
        "subtotal": 42.50,
        "shipping": 0.00,
        "tax": 4.25,
        "total": 46.75,
        "shipping_status": "Shipped",
        "cards_slice": slice(40, 45),  # indices into DEMO_CARDS
    },
]

# Demo wishlist entries: (set_code, collector_number) — looked up by printing to get oracle_id
DEMO_WISHLIST = [
    ("mh3", "209", "Need for Commander deck", 2, 50.00),      # Nadu
    ("dsk", "224", None, 1, None),                              # Valgavoth
    ("otj", "196", "For Obeka combo deck", 3, 25.00),         # Obeka
]


def load_demo_data(conn: sqlite3.Connection) -> bool:
    """Load demo fixture data into the database.

    Returns True if data was loaded, False if already present.
    """
    # Idempotency check
    cursor = conn.execute(
        "SELECT value FROM settings WHERE key = 'demo_loaded'"
    )
    row = cursor.fetchone()
    if row is not None:
        return False

    collection_repo = CollectionRepository(conn)
    order_repo = OrderRepository(conn)
    wishlist_repo = WishlistRepository(conn)

    ts = now_iso()

    # Build scryfall_id lookup for all demo cards
    resolved = []
    missing = []
    for i, (set_code, cn, finish, condition, status) in enumerate(DEMO_CARDS):
        cursor = conn.execute(
            "SELECT scryfall_id FROM printings WHERE set_code = ? AND collector_number = ?",
            (set_code, cn),
        )
        row = cursor.fetchone()
        if row:
            resolved.append((i, row["scryfall_id"], finish, condition, status))
        else:
            missing.append(f"  {set_code.upper()} #{cn}")

    if missing:
        print(f"  WARNING: {len(missing)} card(s) not in Scryfall cache (run 'mtg cache all' first):")
        for m in missing[:5]:
            print(m)
        if len(missing) > 5:
            print(f"  ... and {len(missing) - 5} more")
        if not resolved:
            print("  No cards could be resolved. Aborting demo data load.")
            return False

    # Create demo orders first (need order IDs for collection entries)
    order_ids = []
    for order_def in DEMO_ORDERS:
        order = Order(
            id=None,
            order_number=order_def["order_number"],
            source=order_def["source"],
            seller_name=order_def["seller_name"],
            order_date=order_def["order_date"],
            subtotal=order_def["subtotal"],
            shipping=order_def["shipping"],
            tax=order_def["tax"],
            total=order_def["total"],
            shipping_status=order_def["shipping_status"],
            created_at=ts,
        )
        oid = order_repo.add(order)
        order_ids.append(oid)

    # Build order index: card index → (order_db_id, order_def_index)
    order_card_map = {}
    for oi, order_def in enumerate(DEMO_ORDERS):
        s = order_def["cards_slice"]
        for ci in range(s.start, s.stop):
            order_card_map[ci] = order_ids[oi]

    # Create collection entries
    added = 0
    for card_idx, scryfall_id, finish, condition, status in resolved:
        order_id = order_card_map.get(card_idx)
        source = "order_import" if order_id else "demo"

        entry = CollectionEntry(
            id=None,
            scryfall_id=scryfall_id,
            finish=finish,
            condition=condition,
            status=status,
            source=source,
            acquired_at=ts,
            order_id=order_id,
        )
        collection_repo.add(entry)
        added += 1

    # Create wishlist entries
    wishlist_added = 0
    for set_code, cn, notes, priority, max_price in DEMO_WISHLIST:
        cursor = conn.execute(
            "SELECT oracle_id, scryfall_id FROM printings WHERE set_code = ? AND collector_number = ?",
            (set_code, cn),
        )
        row = cursor.fetchone()
        if not row:
            continue

        entry = WishlistEntry(
            id=None,
            oracle_id=row["oracle_id"],
            scryfall_id=row["scryfall_id"],
            max_price=max_price,
            priority=priority,
            notes=notes,
            added_at=ts,
            source="demo",
        )
        wishlist_repo.add(entry)
        wishlist_added += 1

    # Mark demo as loaded
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('demo_loaded', ?)",
        (ts,),
    )

    conn.commit()

    print(f"  Added {added} collection entries ({len(missing)} skipped — not cached)")
    print(f"  Created {len(order_ids)} demo orders")
    print(f"  Added {wishlist_added} wishlist entries")

    return True
