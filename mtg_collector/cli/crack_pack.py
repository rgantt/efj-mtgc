"""Crack-a-pack command: mtg crack-pack --set EOE --product collector"""

import sys

from mtg_collector.db.connection import get_db_path
from mtg_collector.services.pack_generator import PackGenerator


def register(subparsers):
    """Register the crack-pack subcommand."""
    parser = subparsers.add_parser(
        "crack-pack",
        help="Generate a virtual booster pack",
        description="Generate a virtual booster pack from MTGJSON data.",
    )
    parser.add_argument(
        "--set",
        required=True,
        help="Set code (e.g., EOE, STX, MOM)",
    )
    parser.add_argument(
        "--product",
        default="play",
        help="Booster type: play, draft, collector, set (default: play)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available booster products for the set",
    )
    parser.set_defaults(func=run)


def run(args):
    """Run the crack-pack command."""
    db_path = get_db_path(getattr(args, "db_path", None))

    try:
        gen = PackGenerator(db_path)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    set_code = args.set.lower()

    if args.list:
        try:
            products = gen.list_products(set_code)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Available boosters for {set_code.upper()}:")
        for p in products:
            print(f"  {p}")
        return

    try:
        result = gen.generate_pack(set_code, args.product)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    pack = result["cards"]
    vi = result["variant_index"]
    vw = result["variant_weight"]
    tw = result["total_weight"]

    print(f"\n{'=' * 70}")
    print(f"  {set_code.upper()} {args.product.title()} Booster — {len(pack)} cards")
    print(f"  Variant {vi} (weight {vw}/{tw} = {vw/tw:.1%})")
    print(f"{'=' * 70}\n")

    for i, card in enumerate(pack, 1):
        foil_tag = " *FOIL*" if card["foil"] else ""
        treatments = []
        if card["border_color"] == "borderless":
            treatments.append("borderless")
        if "showcase" in card["frame_effects"]:
            treatments.append("showcase")
        if "extendedart" in card["frame_effects"]:
            treatments.append("extended art")
        if card["is_full_art"]:
            treatments.append("full art")

        treatment_str = f" ({', '.join(treatments)})" if treatments else ""

        print(
            f"  {i:2d}. {card['name']:<35s} "
            f"{card['set_code']} #{card['collector_number']:<5s} "
            f"[{card['rarity']}]{foil_tag}{treatment_str:<30s} "
            f"sheet: {card['sheet_name']}"
        )

    print()
