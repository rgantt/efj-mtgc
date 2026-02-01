#!/usr/bin/env python3
"""
MTG Card Collection Builder
Uses Claude API for card identification and Scryfall API for card data
"""

import os
import sys
import json
import base64
import csv
import time
import argparse
from pathlib import Path
from typing import List, Dict, Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: requests library not found. Install with: pip install requests")
    sys.exit(1)

import anthropic


class ClaudeVision:
    """Interface to Claude API for card image analysis"""
    
    def __init__(self):
        self.client = anthropic.Anthropic()
        
    def encode_image(self, image_path: str) -> str:
        """Encode image to base64"""
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    
    def identify_cards(self, image_path: str) -> List[str]:
        """
        Send image to Claude and ask it to identify all card names
        Returns list of card names
        """
        print(f"🔍 Analyzing image: {image_path}")
        
        # Determine media type
        ext = Path(image_path).suffix.lower()
        media_type_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp'
        }
        media_type = media_type_map.get(ext, 'image/jpeg')
        
        # Encode image
        image_data = self.encode_image(image_path)
        
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data
                                }
                            },
                            {
                                "type": "text",
                                "text": """Please identify all Magic: The Gathering cards in this image.

For each card, read the card name carefully. Return ONLY a JSON array of card names, nothing else.

Format:
["Card Name 1", "Card Name 2", "Card Name 3"]

Be precise with the card names - they must match exactly as printed on the cards."""
                            }
                        ]
                    }
                ]
            )

            # Extract text from response
            text_content = ""
            for block in response.content:
                if block.type == 'text':
                    text_content += block.text

            # Parse JSON response
            # Remove markdown code fences if present
            text_content = text_content.strip()
            if text_content.startswith('```'):
                lines = text_content.split('\n')
                text_content = '\n'.join(lines[1:-1])

            card_names = json.loads(text_content)

            print(f"✓ Found {len(card_names)} card(s): {', '.join(card_names)}")
            return card_names

        except Exception as e:
            print(f"❌ Error calling Claude API: {e}")
            return []
    
    def get_card_details(self, image_path: str, card_name: str) -> Dict:
        """
        Ask Claude to extract details about a specific card from the image
        Returns dict with visible details: set_code, collector_number, foil, condition
        """
        print(f"  🔎 Extracting details for '{card_name}'...")
        
        ext = Path(image_path).suffix.lower()
        media_type_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp'
        }
        media_type = media_type_map.get(ext, 'image/jpeg')
        
        image_data = self.encode_image(image_path)
        
        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data
                                }
                            },
                            {
                                "type": "text",
                                "text": f"""Look at the card named "{card_name}" in this image.

Extract the following information if visible:
- Set code (3-4 letter code, often near bottom)
- Collector number (number at bottom)
- Is it foil? (shiny/holographic appearance)
- Condition (any visible damage, wear, or is it Near Mint?)

Return ONLY a JSON object:
{{
  "set_code": "xxx" or null,
  "collector_number": "123" or null,
  "foil": true or false,
  "condition": "Near Mint" or other condition
}}"""
                            }
                        ]
                    }
                ]
            )

            text_content = ""
            for block in response.content:
                if block.type == 'text':
                    text_content += block.text

            # Clean up JSON
            text_content = text_content.strip()
            if text_content.startswith('```'):
                lines = text_content.split('\n')
                text_content = '\n'.join(lines[1:-1])

            details = json.loads(text_content)
            return details

        except Exception as e:
            print(f"    ⚠️  Could not extract details: {e}")
            return {
                "set_code": None,
                "collector_number": None,
                "foil": False,
                "condition": "Near Mint"
            }


class ScryfallAPI:
    """Interface to Scryfall API"""
    
    BASE_URL = "https://api.scryfall.com"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'MTGCollectionTool/1.0'})
        self.last_request = 0
    
    def _rate_limit(self):
        """Respect Scryfall's rate limit (100ms between requests)"""
        elapsed = time.time() - self.last_request
        if elapsed < 0.1:
            time.sleep(0.1 - elapsed)
        self.last_request = time.time()
    
    def search_card(self, name: str, set_code: str = None, 
                    collector_number: str = None) -> List[Dict]:
        """
        Search for card printings
        If set_code and collector_number provided, tries exact match first
        Otherwise returns all printings
        """
        self._rate_limit()
        
        # Build search query
        if set_code and collector_number:
            # Try exact match first
            query = f'!"{name}" set:{set_code} cn:{collector_number}'
        elif set_code:
            query = f'!"{name}" set:{set_code}'
        else:
            query = f'!"{name}"'
        
        url = f"{self.BASE_URL}/cards/search"
        params = {
            'q': query,
            'unique': 'prints',
            'order': 'released',
            'dir': 'desc'
        }
        
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            if data.get('object') == 'list':
                return data.get('data', [])
            return []
            
        except requests.exceptions.RequestException as e:
            print(f"    ❌ Scryfall API error: {e}")
            return []
    
    def format_card_info(self, card: Dict) -> str:
        """Format card printing info for display"""
        set_code = card.get('set', '').upper()
        set_name = card.get('set_name', '')
        cn = card.get('collector_number', '')
        rarity = card.get('rarity', '').capitalize()
        released = card.get('released_at', '')
        
        foil_opts = []
        if card.get('nonfoil', False):
            foil_opts.append('nonfoil')
        if card.get('foil', False):
            foil_opts.append('foil')
        foil_str = '/'.join(foil_opts) if foil_opts else 'unknown'
        
        return f"{set_code:5s} #{cn:4s} - {set_name:35s} ({rarity:10s}) [{released}] ({foil_str})"


class MoxfieldCSV:
    """Handle Moxfield CSV operations"""
    
    HEADERS = [
        'Count', 'Tradelist Count', 'Name', 'Edition', 'Condition',
        'Language', 'Foil', 'Tags', 'Last Modified', 'Collector Number',
        'Alter', 'Proxy', 'Purchase Price'
    ]
    
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self._ensure_file_exists()
    
    def _ensure_file_exists(self):
        """Create CSV with headers if it doesn't exist"""
        if not Path(self.csv_path).exists():
            with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.HEADERS)
                writer.writeheader()
            print(f"✓ Created new collection file: {self.csv_path}")
    
    def append_card(self, card_data: Dict):
        """Append a card to the CSV"""
        with open(self.csv_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=self.HEADERS)
            
            row = {
                'Count': card_data.get('count', '1'),
                'Tradelist Count': '',
                'Name': card_data['name'],
                'Edition': card_data['set_code'].lower(),
                'Condition': card_data.get('condition', 'Near Mint'),
                'Language': 'English',
                'Foil': 'foil' if card_data.get('foil', False) else '',
                'Tags': '',
                'Last Modified': '',
                'Collector Number': card_data.get('collector_number', ''),
                'Alter': '',
                'Proxy': '',
                'Purchase Price': ''
            }
            writer.writerow(row)
        
        print(f"  ✓ Added to collection: {card_data['name']} ({card_data['set_code'].upper()} #{card_data.get('collector_number', 'N/A')})")


def interactive_select_printing(scryfall: ScryfallAPI, name: str, 
                                printings: List[Dict], 
                                hint_set: str = None,
                                hint_cn: str = None) -> Optional[Dict]:
    """
    Interactively select the correct printing
    Uses hints from image analysis to make a best guess
    """
    if not printings:
        return None
    
    # If we have exact match (1 result), use it
    if len(printings) == 1:
        return printings[0]
    
    # Try to narrow down using hints
    if hint_set:
        filtered = [p for p in printings if p.get('set', '').lower() == hint_set.lower()]
        if len(filtered) == 1:
            print(f"  ✓ Matched by set: {hint_set.upper()}")
            return filtered[0]
        elif len(filtered) > 1:
            printings = filtered
    
    if hint_cn and hint_set:
        filtered = [p for p in printings 
                   if p.get('set', '').lower() == hint_set.lower() 
                   and p.get('collector_number') == hint_cn]
        if len(filtered) == 1:
            print(f"  ✓ Matched by set + collector number: {hint_set.upper()} #{hint_cn}")
            return filtered[0]
        elif len(filtered) > 1:
            printings = filtered
    
    # Still ambiguous - ask user
    print(f"\n  Multiple printings found for '{name}':")
    print(f"  {'─' * 100}")
    for idx, card in enumerate(printings, 1):
        print(f"  {idx:3d}. {scryfall.format_card_info(card)}")
    print(f"  {'─' * 100}")
    
    while True:
        try:
            choice = input(f"  Select printing (1-{len(printings)}) or 's' to skip: ").strip().lower()
            
            if choice == 's':
                print("  ⊘ Skipped")
                return None
            
            choice_num = int(choice)
            if 1 <= choice_num <= len(printings):
                selected = printings[choice_num - 1]
                print(f"  ✓ Selected: {scryfall.format_card_info(selected)}")
                return selected
            else:
                print(f"  Invalid choice. Enter 1-{len(printings)} or 's'")
        except ValueError:
            print("  Invalid input. Enter a number or 's'")
        except (KeyboardInterrupt, EOFError):
            print("\n  ⊘ Cancelled")
            return None


def process_card(claude: ClaudeVision, scryfall: ScryfallAPI, 
                moxfield: MoxfieldCSV, image_path: str, card_name: str):
    """Process a single card through the full workflow"""
    print(f"\n{'═' * 100}")
    print(f"Processing: {card_name}")
    print('═' * 100)
    
    # Step 1: Get card details from image
    details = claude.get_card_details(image_path, card_name)
    
    # Step 2: Query Scryfall
    printings = scryfall.search_card(
        card_name,
        set_code=details.get('set_code'),
        collector_number=details.get('collector_number')
    )
    
    if not printings:
        print(f"  ❌ No printings found on Scryfall for '{card_name}'")
        return
    
    # Step 3: Select correct printing
    selected = interactive_select_printing(
        scryfall, card_name, printings,
        hint_set=details.get('set_code'),
        hint_cn=details.get('collector_number')
    )
    
    if not selected:
        return
    
    # Step 4: Append to CSV
    card_data = {
        'name': selected['name'],
        'set_code': selected['set'],
        'collector_number': selected.get('collector_number', ''),
        'foil': details.get('foil', False),
        'condition': details.get('condition', 'Near Mint'),
        'count': '1'
    }
    
    moxfield.append_card(card_data)


def main():
    parser = argparse.ArgumentParser(
        description='MTG Card Collection Builder using Claude + Scryfall',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s photo.jpg
  %(prog)s photo.jpg --output my_collection.csv
  %(prog)s cards/*.jpg --batch
        """
    )
    parser.add_argument('image', help='Path to card image')
    parser.add_argument('-o', '--output', default='moxfield_collection.csv',
                       help='Output CSV file (default: moxfield_collection.csv)')
    parser.add_argument('-b', '--batch', action='store_true',
                       help='Batch mode: process without prompts (auto-select first match)')
    
    args = parser.parse_args()
    
    # Validate image exists
    if not Path(args.image).exists():
        print(f"❌ Image not found: {args.image}")
        sys.exit(1)
    
    print("╔" + "═" * 98 + "╗")
    print("║" + " MTG CARD COLLECTION BUILDER ".center(98) + "║")
    print("╚" + "═" * 98 + "╝")
    print()
    
    # Initialize components
    claude = ClaudeVision()
    scryfall = ScryfallAPI()
    moxfield = MoxfieldCSV(args.output)
    
    # Step 1: Identify all cards in image
    card_names = claude.identify_cards(args.image)
    
    if not card_names:
        print("❌ No cards identified in image")
        sys.exit(1)
    
    # Step 2: Process each card
    for card_name in card_names:
        process_card(claude, scryfall, moxfield, args.image, card_name)
    
    print(f"\n{'═' * 100}")
    print(f"✓ Complete! Collection saved to: {args.output}")
    print('═' * 100)


if __name__ == "__main__":
    main()
