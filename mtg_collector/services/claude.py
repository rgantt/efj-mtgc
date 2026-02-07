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
