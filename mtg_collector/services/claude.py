"""Claude Vision API interface for card image analysis."""

import base64
import json
import time
from pathlib import Path
from typing import List, Dict, Optional

import anthropic


class ClaudeVision:
    """Interface to Claude API for card image analysis."""

    def __init__(self, model: str = "claude-sonnet-4-20250514", max_retries: int = 4):
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
        """Parse JSON from Claude response, handling markdown fences."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last line (code fences)
            text = "\n".join(lines[1:-1])
        return json.loads(text)

    def identify_cards(self, image_path: str) -> List[Dict[str, Optional[str]]]:
        """
        Send image to Claude and ask it to identify all card names and sets.

        Returns list of dicts with 'name' and 'set' keys.
        The 'set' may be None if Claude couldn't determine it.
        """
        print(f"Analyzing image: {image_path}")

        media_type = self._get_media_type(image_path)
        image_data = self.encode_image(image_path)

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    # Wait before retry with exponential backoff (starting at 3s)
                    wait_time = 3 * (2 ** (attempt - 1))
                    print(f"  Retrying in {wait_time}s (attempt {attempt + 1}/{self.max_retries + 1})...")
                    time.sleep(wait_time)

                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=2000,
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
                                    "text": """Please identify all Magic: The Gathering cards in this image.

For each card, read:
1. The card name (top of card)
2. The set code (3-4 letter code at bottom, near the collector number)

Return ONLY a JSON array with objects containing "name" and "set" for each card:
[
  {"name": "Card Name 1", "set": "ABC"},
  {"name": "Card Name 2", "set": "XYZ"}
]

If you cannot read the set code for a card, use null:
  {"name": "Card Name", "set": null}

If multiple cards appear to be from the same set but you can only read the set code on some of them, use that set code for all cards that look similar (same frame style, expansion symbol).

Be precise with card names - they must match exactly as printed.""",
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

                # Normalize the response format
                normalized = []
                for card in cards:
                    if isinstance(card, str):
                        # Legacy format - just a name
                        normalized.append({"name": card, "set": None})
                    elif isinstance(card, dict):
                        normalized.append({
                            "name": card.get("name", ""),
                            "set": card.get("set"),
                        })

                card_names = [c["name"] for c in normalized]
                print(f"  Found {len(normalized)} card(s): {', '.join(card_names)}")
                return normalized

            except json.JSONDecodeError as e:
                last_error = f"JSON parse error: {e}"
                # Log what Claude actually returned for debugging
                if text_content:
                    preview = text_content[:200] + "..." if len(text_content) > 200 else text_content
                    print(f"  {last_error} (response: {repr(preview)})")
                else:
                    print(f"  {last_error} (empty response)")
            except Exception as e:
                last_error = str(e)
                # Don't retry on authentication or permanent errors
                if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                    print(f"  Error calling Claude API: {e}")
                    return []
                print(f"  Error calling Claude API: {e}")

        print(f"  Failed after {self.max_retries + 1} attempts. Last error: {last_error}")
        return []

    def get_card_details(self, image_path: str, card_name: str) -> Dict:
        """
        Ask Claude to extract details about a specific card from the image.
        Returns dict with visible details: set_code, collector_number, foil, condition.
        """
        print(f"  Extracting details for '{card_name}'...")

        media_type = self._get_media_type(image_path)
        image_data = self.encode_image(image_path)

        try:
            response = self.client.messages.create(
                model=self.model,
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
                                    "data": image_data,
                                },
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
}}""",
                            },
                        ],
                    }
                ],
            )

            text_content = ""
            for block in response.content:
                if block.type == "text":
                    text_content += block.text

            details = self._parse_json_response(text_content)
            return details

        except Exception as e:
            print(f"    Could not extract details: {e}")
            return {
                "set_code": None,
                "collector_number": None,
                "foil": False,
                "condition": "Near Mint",
            }
