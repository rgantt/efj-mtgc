"""Orders command: mtg orders list|show|receive"""

import sys

from mtg_collector.db import (
    OrderRepository,
    get_connection,
    init_db,
)


def register(subparsers):
    """Register the orders subcommand."""
    parser = subparsers.add_parser(
        "orders",
        help="Manage orders",
        description="List, view, and manage card orders.",
    )
    sub = parser.add_subparsers(dest="orders_action", metavar="<action>")

    # orders list
    list_parser = sub.add_parser("list", help="List all orders")
    list_parser.add_argument(
        "--source",
        choices=["tcgplayer", "cardkingdom"],
        help="Filter by source",
    )

    # orders show <id>
    show_parser = sub.add_parser("show", help="Show order details")
    show_parser.add_argument("order_id", type=int, help="Order ID")

    # orders receive <id>
    receive_parser = sub.add_parser("receive", help="Mark order as received (flip ordered→owned)")
    receive_parser.add_argument("order_id", type=int, help="Order ID")

    parser.set_defaults(func=run)


def run(args):
    """Run the orders command."""
    if not args.orders_action:
        print("Usage: mtg orders <list|show|receive>")
        sys.exit(0)

    conn = get_connection(args.db_path)
    init_db(conn)
    order_repo = OrderRepository(conn)

    if args.orders_action == "list":
        _list_orders(order_repo, getattr(args, "source", None))
    elif args.orders_action == "show":
        _show_order(order_repo, args.order_id)
    elif args.orders_action == "receive":
        _receive_order(order_repo, conn, args.order_id)


def _list_orders(order_repo, source=None):
    """List all orders."""
    orders = order_repo.list_all(source=source)
    if not orders:
        print("No orders found.")
        return

    print(f"{'ID':>4}  {'Order #':<28}  {'Seller':<25}  {'Date':<20}  {'Cards':>5}  {'Total':>8}")
    print("-" * 100)
    for o in orders:
        order_num = (o["order_number"] or "")[:28]
        seller = (o["seller_name"] or "")[:25]
        date = (o["order_date"] or "")[:20]
        total = f"${o['total']:.2f}" if o["total"] else ""
        print(f"{o['id']:>4}  {order_num:<28}  {seller:<25}  {date:<20}  {o['card_count']:>5}  {total:>8}")


def _show_order(order_repo, order_id):
    """Show order details with cards."""
    order = order_repo.get(order_id)
    if not order:
        print(f"Order {order_id} not found.")
        return

    print(f"Order #{order.order_number or 'N/A'}")
    print(f"  Source: {order.source}")
    print(f"  Seller: {order.seller_name}")
    print(f"  Date: {order.order_date}")
    if order.subtotal is not None:
        print(f"  Subtotal: ${order.subtotal:.2f}")
    if order.shipping is not None:
        print(f"  Shipping: ${order.shipping:.2f}")
    if order.tax is not None:
        print(f"  Tax: ${order.tax:.2f}")
    if order.total is not None:
        print(f"  Total: ${order.total:.2f}")
    if order.shipping_status:
        print(f"  Shipping: {order.shipping_status}")
    if order.estimated_delivery:
        print(f"  Est. Delivery: {order.estimated_delivery}")

    cards = order_repo.get_order_cards(order_id)
    if cards:
        print(f"\n  Cards ({len(cards)}):")
        for c in cards:
            status_tag = f" [{c['status']}]" if c["status"] != "owned" else ""
            price_tag = f" ${c['purchase_price']:.2f}" if c.get("purchase_price") else ""
            print(f"    {c['name']} ({c['set_code'].upper()} #{c['collector_number']}) {c['finish']}{status_tag}{price_tag}")


def _receive_order(order_repo, conn, order_id):
    """Mark all ordered cards in an order as owned."""
    order = order_repo.get(order_id)
    if not order:
        print(f"Order {order_id} not found.")
        return

    count = order_repo.receive_order(order_id)
    conn.commit()

    if count == 0:
        print(f"No ordered cards to receive in order {order_id}.")
    else:
        print(f"Received {count} card(s) from {order.seller_name or 'order'} #{order.order_number or order_id}.")
