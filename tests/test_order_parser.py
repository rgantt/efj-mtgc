"""
Tests for order parsing (no DB/network required).

Tests format detection, TCG text parsing, TCG HTML parsing, condition parsing.

To run: pytest tests/test_order_parser.py -v
"""

import pytest

from mtg_collector.services.order_parser import (
    detect_order_format,
    parse_order,
    _parse_condition_and_foil,
    _extract_treatment,
    _parse_dollar,
)


class TestFormatDetection:
    def test_tcg_html(self):
        assert detect_order_format('<div class="orderWrap">') == "tcg_html"

    def test_tcg_html_view_source(self):
        assert detect_order_format('<span class="start-tag">div</span>') == "tcg_html"

    def test_tcg_text(self):
        assert detect_order_format("Magic\tFINAL FANTASY\tCard Name\tNear Mint") == "tcg_text"

    def test_ck_text(self):
        assert detect_order_format("1x Lightning Bolt - Near Mint") == "ck_text"


class TestConditionParsing:
    def test_near_mint(self):
        assert _parse_condition_and_foil("Near Mint") == ("Near Mint", False)

    def test_lightly_played(self):
        assert _parse_condition_and_foil("Lightly Played") == ("Lightly Played", False)

    def test_near_mint_foil(self):
        assert _parse_condition_and_foil("Near Mint Foil") == ("Near Mint", True)

    def test_near_mint_holofoil(self):
        assert _parse_condition_and_foil("Near Mint Holofoil") == ("Near Mint", True)

    def test_lightly_played_holofoil(self):
        assert _parse_condition_and_foil("Lightly Played Holofoil") == ("Lightly Played", True)

    def test_lightly_played_foil(self):
        assert _parse_condition_and_foil("Lightly Played Foil") == ("Lightly Played", True)


class TestTreatmentExtraction:
    def test_no_treatment(self):
        assert _extract_treatment("Lightning Bolt") == ("Lightning Bolt", None)

    def test_borderless(self):
        assert _extract_treatment("Elesh Norn (Borderless)") == ("Elesh Norn", "Borderless")

    def test_showcase(self):
        assert _extract_treatment("Fblthp (Showcase)") == ("Fblthp", "Showcase")

    def test_non_treatment_paren(self):
        # Parenthetical that isn't a known treatment stays in the name
        name, treat = _extract_treatment("Jace (Phyrexian)")
        assert treat is None
        assert "Jace" in name


class TestDollarParsing:
    def test_basic(self):
        assert _parse_dollar("$0.60") == 0.60

    def test_large(self):
        assert _parse_dollar("$1,234.56") == 1234.56

    def test_no_dollar(self):
        assert _parse_dollar("15.30") == 15.30

    def test_empty(self):
        assert _parse_dollar("") is None


class TestTcgTextParsing:
    def test_basic_line(self):
        text = "Magic - FINAL FANTASY - Jecht, Reluctant Guardian - Near Mint"
        orders = parse_order(text, "tcg_text")
        assert len(orders) == 1
        assert len(orders[0].items) == 1
        item = orders[0].items[0]
        assert item.card_name == "Jecht, Reluctant Guardian"
        assert item.set_hint == "FINAL FANTASY"
        assert item.condition == "Near Mint"
        assert item.foil is False

    def test_foil_condition(self):
        text = "Magic - Foundations - Lightning Bolt - Near Mint Foil"
        orders = parse_order(text, "tcg_text")
        item = orders[0].items[0]
        assert item.card_name == "Lightning Bolt"
        assert item.foil is True
        assert item.condition == "Near Mint"

    def test_multiple_lines(self):
        text = """Magic - FINAL FANTASY - Card One - Near Mint
Magic - FINAL FANTASY - Card Two - Lightly Played
Magic - Foundations - Card Three - Near Mint Foil"""
        orders = parse_order(text, "tcg_text")
        assert len(orders) == 1
        assert len(orders[0].items) == 3

    def test_empty_lines_ignored(self):
        text = "\nMagic - Set - Card - Near Mint\n\n"
        orders = parse_order(text, "tcg_text")
        assert len(orders) == 1
        assert len(orders[0].items) == 1

    def test_tab_separated(self):
        text = "Magic\tFINAL FANTASY\tJecht\tNear Mint"
        orders = parse_order(text, "tcg_text")
        assert len(orders) == 1
        item = orders[0].items[0]
        assert item.card_name == "Jecht"

    def test_quantity_prefix(self):
        text = "Magic - Set - 2x Lightning Bolt - Near Mint"
        orders = parse_order(text, "tcg_text")
        item = orders[0].items[0]
        assert item.quantity == 2
        assert item.card_name == "Lightning Bolt"

    def test_dual_name_card(self):
        text = "Magic - FINAL FANTASY - Aettir and Priwen - Near Mint"
        orders = parse_order(text, "tcg_text")
        item = orders[0].items[0]
        assert item.card_name == "Aettir and Priwen"

    def test_borderless_treatment(self):
        text = "Magic - Set - Elesh Norn (Borderless) - Near Mint"
        orders = parse_order(text, "tcg_text")
        item = orders[0].items[0]
        assert item.card_name == "Elesh Norn"
        assert item.treatment == "Borderless"


class TestCkTextParsing:
    def test_basic(self):
        text = "1x Lightning Bolt - Near Mint"
        orders = parse_order(text, "ck_text")
        assert len(orders) == 1
        item = orders[0].items[0]
        assert item.card_name == "Lightning Bolt"
        assert item.quantity == 1
        assert item.condition == "Near Mint"

    def test_with_set(self):
        text = "2x Counterspell [Foundations] - Lightly Played"
        orders = parse_order(text, "ck_text")
        item = orders[0].items[0]
        assert item.card_name == "Counterspell"
        assert item.set_hint == "Foundations"
        assert item.quantity == 2
        assert item.condition == "Lightly Played"

    def test_foil(self):
        text = "1x Thoughtseize - Near Mint Foil"
        orders = parse_order(text, "ck_text")
        item = orders[0].items[0]
        assert item.foil is True
        assert item.condition == "Near Mint"


class TestTcgHtmlParsing:
    """Test TCGPlayer HTML parsing with minimal HTML snippets."""

    def _make_order_html(self, seller="Test Seller", order_number="ABC-123",
                         date="January 1, 2026", items=None, total="$10.00",
                         direct=False):
        """Build a minimal orderWrap div for testing."""
        if items is None:
            items = [{"name": "Lightning Bolt", "set": "Foundations",
                      "rarity": "C", "condition": "Near Mint", "price": "$0.10", "qty": "1"}]

        if direct:
            header_num = f'''<span style="display: inline-block; width: 20%;">
                <span class="orderTitle">TCGPLAYER DIRECT #</span><br/>
                {order_number}
            </span>'''
            seller_section = f'''<span class="orderSummary breakword">
                <span class="orderTitle">SHIPPED BY</span><br/>
                TCGplayer Direct
                <span class="orderTitle" data-aid="spn-sellerorderwidget-trackingnumber">Shipping Not Confirmed</span><br/>
                Standard (est.delivery by March 02, 2026) - $1.00
            </span>'''
        else:
            header_num = f'''<span style="display: inline-block; width: 20%;">
                <span class="orderTitle">Order Number</span><br/>
                {order_number}
            </span>'''
            seller_section = f'''<span class="orderSummary breakword">
                <span class="orderTitle">SHIPPED AND SOLD BY</span><br/>
                <span data-aid="spn-sellerorderwidget-vendorname">
                    <a class="nocontext" href="#">{seller}</a>
                </span>
                <span class="orderTitle" data-aid="spn-sellerorderwidget-trackingnumber">Shipping Not Confirmed</span><br/>
                Standard (est.delivery by March 02, 2026) - $1.00
            </span>'''

        item_rows = ""
        for i, item in enumerate(items):
            cls = "trOdd" if i % 2 == 0 else "trEven"
            item_rows += f'''<tr class="{cls}">
                <td class="orderHistoryItems">
                    <span style="display: block; padding-left: 30px;">
                        <a class="nocontext" href="#">{item["name"]}</a><br/>
                        {item["set"]}
                    </span>
                </td>
                <td class="orderHistoryDetail">Rarity: {item["rarity"]}<br/>Condition: {item["condition"]}</td>
                <td class="orderHistoryPrice">{item["price"]}</td>
                <td class="orderHistoryQuantity">{item["qty"]}</td>
            </tr>'''

        return f'''<div class="orderWrap">
            <div class="orderHeader">
                <span style="display: inline-block; width: 20%;">
                    <span class="orderTitle">Order Date</span><br/>
                    <span data-aid="spn-sellerorderwidget-orderdate">{date}</span>
                </span>
                <span style="display: inline-block; width: 20%;">
                    <span class="orderTitle">Channel</span><br/>
                    <span data-aid="spn-sellerorderwidget-orderchannel">TCG Marketplace</span>
                </span>
                {header_num}
            </div>
            <span class="orderSummary">
                <span class="orderTitle">ORDER SUMMARY</span>
                <table data-aid="tbl-sellerorderwidget-productsinorder">
                    <tr><td>Total:</td><td>{total}</td></tr>
                </table>
            </span>
            {seller_section}
            <table class="orderTable" data-aid="tbl-sellerorderwidget-ordertable">
                <thead><tr><th>ITEMS</th><th>DETAILS</th><th>PRICE</th><th>QUANTITY</th></tr></thead>
                <tbody>{item_rows}</tbody>
            </table>
        </div>'''

    def test_single_order(self):
        html = self._make_order_html()
        orders = parse_order(html, "tcg_html")
        assert len(orders) == 1
        assert orders[0].seller_name == "Test Seller"
        assert orders[0].order_number == "ABC-123"
        assert orders[0].order_date == "January 1, 2026"
        assert len(orders[0].items) == 1
        assert orders[0].items[0].card_name == "Lightning Bolt"

    def test_multi_order(self):
        html = self._make_order_html(seller="Seller A", order_number="A-1")
        html += self._make_order_html(seller="Seller B", order_number="B-2")
        orders = parse_order(html, "tcg_html")
        assert len(orders) == 2
        assert orders[0].seller_name == "Seller A"
        assert orders[1].seller_name == "Seller B"

    def test_foil_item(self):
        items = [{"name": "Card", "set": "Set", "rarity": "R",
                  "condition": "Near Mint Foil", "price": "$1.00", "qty": "1"}]
        html = self._make_order_html(items=items)
        orders = parse_order(html, "tcg_html")
        assert orders[0].items[0].foil is True
        assert orders[0].items[0].condition == "Near Mint"

    def test_holofoil_item(self):
        items = [{"name": "Card", "set": "Set", "rarity": "M",
                  "condition": "Near Mint Holofoil", "price": "$5.00", "qty": "1"}]
        html = self._make_order_html(items=items)
        orders = parse_order(html, "tcg_html")
        assert orders[0].items[0].foil is True

    def test_quantity_gt_1(self):
        items = [{"name": "Card", "set": "Set", "rarity": "C",
                  "condition": "Near Mint", "price": "$0.05", "qty": "3"}]
        html = self._make_order_html(items=items)
        orders = parse_order(html, "tcg_html")
        assert orders[0].items[0].quantity == 3

    def test_multiple_items(self):
        items = [
            {"name": "Card A", "set": "Set", "rarity": "C", "condition": "Near Mint", "price": "$0.05", "qty": "1"},
            {"name": "Card B", "set": "Set", "rarity": "R", "condition": "Lightly Played", "price": "$1.00", "qty": "2"},
        ]
        html = self._make_order_html(items=items)
        orders = parse_order(html, "tcg_html")
        assert len(orders[0].items) == 2
        assert orders[0].items[0].card_name == "Card A"
        assert orders[0].items[1].card_name == "Card B"
        assert orders[0].items[1].condition == "Lightly Played"

    def test_direct_order(self):
        html = self._make_order_html(direct=True, order_number="260214-3ADF")
        orders = parse_order(html, "tcg_html")
        assert len(orders) == 1
        assert orders[0].seller_name == "TCGplayer Direct"
        assert orders[0].order_number == "260214-3ADF"

    def test_estimated_delivery(self):
        html = self._make_order_html()
        orders = parse_order(html, "tcg_html")
        assert orders[0].estimated_delivery == "March 02, 2026"

    def test_financial_total(self):
        html = self._make_order_html(total="$25.99")
        orders = parse_order(html, "tcg_html")
        assert orders[0].total == 25.99

    def test_rarity_hint(self):
        items = [{"name": "Card", "set": "Set", "rarity": "ACE SPEC Rare",
                  "condition": "Near Mint", "price": "$1.00", "qty": "1"}]
        html = self._make_order_html(items=items)
        orders = parse_order(html, "tcg_html")
        assert orders[0].items[0].rarity_hint == "ACE SPEC Rare"


class TestRealTcgHtml:
    """Test parsing the real TCGPlayer order HTML files if available."""

    @pytest.fixture
    def real_html(self):
        import os
        files = [
            os.path.expanduser("~/Downloads/order-page-example-1.html"),
            os.path.expanduser("~/Downloads/order-page-example-2.html"),
        ]
        if not all(os.path.exists(f) for f in files):
            pytest.skip("Real TCGPlayer HTML files not available")
        text = ""
        for f in files:
            with open(f) as fh:
                text += fh.read()
        return text

    def test_parses_all_orders(self, real_html):
        orders = parse_order(real_html, "tcg_html")
        assert len(orders) == 20

    def test_total_items(self, real_html):
        orders = parse_order(real_html, "tcg_html")
        total = sum(len(o.items) for o in orders)
        assert total == 120

    def test_all_orders_have_seller(self, real_html):
        orders = parse_order(real_html, "tcg_html")
        for o in orders:
            assert o.seller_name, f"Order {o.order_number} missing seller name"

    def test_all_orders_have_number(self, real_html):
        orders = parse_order(real_html, "tcg_html")
        for o in orders:
            assert o.order_number, f"Order from {o.seller_name} missing order number"

    def test_items_have_names(self, real_html):
        orders = parse_order(real_html, "tcg_html")
        for o in orders:
            for item in o.items:
                assert item.card_name, f"Item in order {o.order_number} missing card name"
