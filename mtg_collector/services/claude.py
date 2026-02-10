"""Claude Vision API interface for reading card corner info."""

import base64
import json
import time
from pathlib import Path
from typing import List, Dict

import anthropic


class ClaudeVision:
    """Interface to Claude API for card image analysis."""

    def __init__(self, model: str = "claude-opus-4-6", max_retries: int = 4):
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_retries = max_retries

    def extract_cards_from_ocr(
        self,
        ocr_texts: List[str],
        expected_count: int,
        hints: Dict = None,
    ) -> List[Dict]:
        """
        Extract structured card data from raw OCR text using a cheap text-only Claude call.

        Args:
            ocr_texts: List of text fragments from EasyOCR
            expected_count: Number of cards expected in the image
            hints: Optional dict with 'set' and/or 'color' to help disambiguation

        Returns:
            List of dicts with card fields (name, set_code, collector_number, etc.)
        """
        hints = hints or {}
        hint_lines = []
        if hints.get("set"):
            hint_lines.append(f"- All cards are from set: {hints['set'].upper()}")
        if hints.get("color"):
            hint_lines.append(f"- All cards have color identity: {hints['color'].upper()}")
        hint_block = "\n".join(hint_lines) if hint_lines else "None"

        ocr_blob = "\n".join(ocr_texts)

        prompt = f"""Below is raw OCR text extracted from a photo of {expected_count} Magic: The Gathering card(s).
The OCR is noisy — text may be misspelled, fragmented, or out of order.

Your job: identify each card and extract whatever fields you can confidently read.

IMPORTANT CONSTRAINTS:

Card types — the ONLY valid card types in Magic are:
  Artifact, Creature, Enchantment, Instant, Land, Planeswalker, Sorcery, Battle, Tribal
Anything else is NOT a type — it is a card name, subtype, or rules text.

Collector numbers — the printed format has changed over Magic's history:
  - Pre-1998 (before Exodus): NO collector number printed on card at all.
  - 1998–2014 (Exodus through M15): printed as "CN/TOTAL" (e.g., "10/250"), 1-3 digit CN.
  - 2015–2023 (M15 frame through Phyrexia): CN on its own line, 1-3 digits, no leading zeros.
  - 2023+ (March of the Machine onward): exactly 4 digits with leading zeros (e.g., 0092, 0161).
    If you see fewer than 4 digits from a 4-digit-era card, the OCR is truncated — omit it.
  - Some cards across all eras have letter suffixes (a, b, s, z) or prefixes (A-) for variants.

Known hints:
{hint_block}

Return a JSON array of exactly {expected_count} objects. Each object should include
only the fields you can confidently identify from the OCR text:

- "name": card name (string)
- "mana_cost": mana cost like "{{2}}{{R}}" (string)
- "mana_value": converted mana cost (integer)
- "type": one of the valid card types listed above (string)
- "subtype": creature subtype like "Insect Horror" (string)
- "rules_text": rules/abilities text (string)
- "collector_number": exactly 4 digits with leading zeros (string)
- "set_code": 3-4 letter set code (string)
- "artist": artist name (string)
- "power": power for creatures (integer)
- "toughness": toughness for creatures (integer)

Omit any field you are not confident about. Do NOT guess or hallucinate.
If a hint provides the set code, include "set_code" in every card object.

Return ONLY the JSON array, no other text.

OCR TEXT:
{ocr_blob}"""

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 3 * (2 ** (attempt - 1))
                    print(f"  Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})...")
                    time.sleep(wait_time)

                response = self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt}],
                )

                text_content = ""
                for block in response.content:
                    if block.type == "text":
                        text_content += block.text

                if not text_content.strip():
                    raise ValueError("Empty response from Claude")

                cards = self._parse_json_response(text_content)

                if not isinstance(cards, list):
                    raise ValueError(f"Expected JSON array, got {type(cards)}")

                return cards, response.usage

            except json.JSONDecodeError as e:
                last_error = f"JSON parse error: {e}"
                print(f"  {last_error}")
            except anthropic.BadRequestError as e:
                print(f"  Error: {e}")
                return []
            except Exception as e:
                last_error = str(e)
                if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                    print(f"  Error: {e}")
                    return []
                print(f"  Error: {e}")

        print(f"  Failed after {self.max_retries + 1} attempts. Last error: {last_error}")
        return []

    def extract_cards_from_ocr_with_positions(
        self,
        fragments: List[Dict],
        expected_count: int,
        hints: Dict = None,
        status_callback: callable = None,
    ) -> tuple:
        """
        Extract structured card data from OCR fragments with bounding boxes.

        Each fragment has: text, bbox ({x, y, w, h}), confidence.
        Returns (cards_list, usage) where each card has a fragment_indices field.
        """
        hints = hints or {}
        hint_lines = []
        if hints.get("set"):
            hint_lines.append(f"- All cards are from set: {hints['set'].upper()}")
        if hints.get("color"):
            hint_lines.append(f"- All cards have color identity: {hints['color'].upper()}")
        hint_block = "\n".join(hint_lines) if hint_lines else "None"

        # Build numbered fragment list with positions
        frag_lines = []
        for i, f in enumerate(fragments):
            b = f["bbox"]
            frag_lines.append(
                f'[{i}] (at x={int(b["x"])},y={int(b["y"])} w={int(b["w"])},h={int(b["h"])}): "{f["text"]}"'
            )
        frag_blob = "\n".join(frag_lines)

        prompt = f"""Below are numbered OCR text fragments extracted from a photo of {expected_count} Magic: The Gathering card(s).
Each fragment has a position (x, y, w, h) showing where it appeared in the image.
The OCR is noisy — text may be misspelled, fragmented, or out of order.

Your job: identify each card and extract whatever fields you can confidently read.
Additionally, report which fragment indices belong to each card.

IMPORTANT CONSTRAINTS:

Card types — the ONLY valid card types in Magic are:
  Artifact, Creature, Enchantment, Instant, Land, Planeswalker, Sorcery, Battle, Tribal
Anything else is NOT a type — it is a card name, subtype, or rules text.

Collector numbers — the printed format has changed over Magic's history:
  - Pre-1998 (before Exodus): NO collector number printed on card at all.
  - 1998–2014 (Exodus through M15): printed as "CN/TOTAL" (e.g., "10/250"), 1-3 digit CN.
  - 2015–2023 (M15 frame through Phyrexia): CN on its own line, 1-3 digits, no leading zeros.
  - 2023+ (March of the Machine onward): exactly 4 digits with leading zeros (e.g., 0092, 0161).
    If you see fewer than 4 digits from a 4-digit-era card, the OCR is truncated — omit it.
  - Some cards across all eras have letter suffixes (a, b, s, z) or prefixes (A-) for variants.

Known hints:
{hint_block}

Return exactly {expected_count} cards. For each card, include only the fields you can
confidently identify from the OCR text. Always include fragment_indices.
Omit any field you are not confident about.
Do NOT guess or hallucinate.
If a hint provides the set code, include "set_code" in every card object.

OCR FRAGMENTS:
{frag_blob}"""

        card_schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "mana_cost": {"type": "string"},
                "mana_value": {"type": "integer"},
                "type": {"type": "string"},
                "subtype": {"type": "string"},
                "rules_text": {"type": "string"},
                "collector_number": {"type": "string"},
                "set_code": {"type": "string"},
                "artist": {"type": "string"},
                "power": {"type": "integer"},
                "toughness": {"type": "integer"},
                "fragment_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["fragment_indices"],
            "additionalProperties": False,
        }

        def _status(msg):
            if status_callback:
                status_callback(msg)

        _status(f"Sending {len(fragments)} fragments to Claude...")

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 3 * (2 ** (attempt - 1))
                    _status(f"Retry {attempt + 1}/{self.max_retries + 1} in {wait_time}s...")
                    print(f"  Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})...")
                    time.sleep(wait_time)

                _status(f"Waiting for Claude... (attempt {attempt + 1}/{self.max_retries + 1})")
                response = self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=4000,
                    messages=[{"role": "user", "content": prompt}],
                    output_config={
                        "format": {
                            "type": "json_schema",
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "cards": {
                                        "type": "array",
                                        "items": card_schema,
                                    },
                                },
                                "required": ["cards"],
                                "additionalProperties": False,
                            },
                        },
                    },
                )

                text_content = response.content[0].text

                if not text_content.strip():
                    raise ValueError("Empty response from Claude")

                _status("Parsing Claude response...")
                result = json.loads(text_content)
                cards = result["cards"]

                return cards, response.usage
            except anthropic.BadRequestError as e:
                print(f"  Error: {e}")
                return [], None
            except Exception as e:
                last_error = str(e)
                if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                    print(f"  Error: {e}")
                    return [], None
                print(f"  Error: {e}")

        print(f"  Failed after {self.max_retries + 1} attempts. Last error: {last_error}")
        return [], None

    def encode_image(self, image_path: str) -> str:
        """Encode image to base64."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _get_media_type(self, image_path: str) -> str:
        """Determine media type from file extension."""
        ext = Path(image_path).suffix.lower()
        media_type_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        return media_type_map.get(ext, "image/jpeg")

    def _parse_json_response(self, text: str) -> any:
        """Parse JSON from Claude response, handling markdown fences and preamble text."""
        text = text.strip()

        # Handle markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1])

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Look for JSON array in the text
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        # Look for JSON object in the text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

        # If all else fails, raise the original error
        return json.loads(text)

    def read_card_corners(self, image_path: str) -> List[Dict]:
        """
        Read rarity, collector number, and set code from a photo of card corners.

        The bottom-left corner of MTG cards contains text like:
            C 0075
            EOE · EN    (nonfoil — dot separator)
            EOE ★ EN    (foil — star separator)

        Args:
            image_path: Path to the image showing card corners

        Returns:
            List of dicts with 'rarity', 'collector_number', 'set', 'foil'
        """
        print(f"Reading card corners from: {image_path}")

        media_type = self._get_media_type(image_path)
        image_data = self.encode_image(image_path)

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 3 * (2 ** (attempt - 1))
                    print(f"  Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})...")
                    time.sleep(wait_time)

                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4000,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": image_data,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": """This image shows the bottom-left corners of Magic: The Gathering cards.

Each corner has tiny printed text with collector info. Look for the pattern:
  RARITY  COLLECTOR_NUMBER
  SET · EN    (nonfoil)
  SET ★ EN    (foil)

Where:
- RARITY: a single letter — C (common), U (uncommon), R (rare), M (mythic rare), P (promo)
- COLLECTOR_NUMBER: 3-4 digits, often with leading zeros (e.g. 0075, 0187, 0200)
- SET: 3-4 letter set code (e.g. EOE, ECL, MKM) — appears BEFORE "EN"
- "EN" is the language marker (English) — NOT part of the set code

FOIL DETECTION — look at the symbol between SET and EN:
- A dot/circle (·) means NONFOIL
- A star (★) means FOIL
This is the ONLY reliable way to detect foil from corner photos.

TIP: Find "EN" first as a landmark, then read the set code before it, check the separator symbol for foil, and read rarity + collector number on the line above.

For EACH distinct card corner visible, extract the rarity letter, collector number, set code, and foil status.

Return ONLY a JSON array:
[{"rarity": "C", "collector_number": "0075", "set": "EOE", "foil": false}, ...]""",
                                },
                            ],
                        }
                    ],
                )

                text_content = ""
                for block in response.content:
                    if block.type == "text":
                        text_content += block.text

                if not text_content.strip():
                    raise ValueError("Empty response from Claude")

                cards = self._parse_json_response(text_content)

                if not isinstance(cards, list):
                    raise ValueError(f"Expected JSON array, got {type(cards)}")

                # Normalize
                normalized = []
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    rarity = card.get("rarity", "").strip().upper()
                    cn = card.get("collector_number", "").strip()
                    set_code = card.get("set", "").strip()
                    foil = bool(card.get("foil", False))

                    if not cn or not set_code:
                        continue

                    normalized.append({
                        "rarity": rarity,
                        "collector_number": cn,
                        "set": set_code,
                        "foil": foil,
                    })

                print(f"  Found {len(normalized)} card corner(s)")
                return normalized

            except json.JSONDecodeError as e:
                last_error = f"JSON parse error: {e}"
                print(f"  {last_error}")
            except anthropic.BadRequestError as e:
                print(f"  Error: {e}")
                return []
            except Exception as e:
                last_error = str(e)
                if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                    print(f"  Error: {e}")
                    return []
                print(f"  Error: {e}")

        print(f"  Failed after {self.max_retries + 1} attempts. Last error: {last_error}")
        return []
