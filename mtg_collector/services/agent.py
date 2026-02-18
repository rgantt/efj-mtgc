"""Tool-using Claude agent service for MTG card identification from photos."""

import json
import sqlite3
import sys

import anthropic

from mtg_collector.db.connection import get_connection
from mtg_collector.services.claude import ClaudeVision
from mtg_collector.services.ocr import run_ocr_with_boxes

AGENT_MODEL_HAIKU = "claude-haiku-4-5-20251001"
AGENT_MODEL_SONNET = "claude-sonnet-4-6"
VISION_MODEL = "claude-opus-4-6"
DEFAULT_MAX_CALLS = 8
LARGE_FRAGMENT_THRESHOLD = 70

SYSTEM_PROMPT = """\
You are an expert Magic: The Gathering card identifier. You have OCR text fragments
from a photo of MTG cards.

CARD LAYOUT (top to bottom):
  - Title (top of card)
  - Type and Subtype (middle, below the art)
  - Rules text (below type line — may be blank on vanilla creatures)
  - Flavor text (italic, below rules — optional)
  - Bottom-left corner: collector number, set code, artist name
  - Bottom-right corner: power/toughness (creatures only)

There will be large vertical gaps between the title and the type line — that is the card art.
Cards with no rules text will have another gap between the type line and the collector info.
All of these text regions belong to the SAME card. Do NOT split them into separate cards.

If the photo contains multiple cards side by side, use horizontal position (x coordinates)
to determine which fragments belong to which card.

Collector numbers — the printed format has changed over Magic's history:
  - Pre-1998 (before Exodus): NO collector number printed on card at all.
  - 1998–2014 (Exodus through M15): printed as "CN/TOTAL" (e.g., "10/250"), 1-3 digit CN.
  - 2015–2023 (M15 frame through Phyrexia): CN on its own line, 1-3 digits, no leading zeros.
  - 2023+ (March of the Machine onward): exactly 4 digits with leading zeros (e.g., 0092, 0161).
    If you see fewer than 4 digits from a 4-digit-era card, the OCR is truncated — omit it.
  - Some cards across all eras have letter suffixes (a, b, s, z) or prefixes (A-) for variants.

Identify every card in the image with high confidence. Strategy:
1. Interpret OCR fragments to find card name, set code, collector number
2. Search to verify using query_local_db
3. If OCR quality seems poor, call rerun_ocr once to get fresh data
4. Only call analyze_image if you cannot identify a card after 2–3 search attempts

When confident about all cards, stop calling tools. Your findings will be collected
and formatted automatically.

If you can identify the card name but cannot determine which specific printing it is,
return one entry per candidate printing (same name, different set_code/collector_number),
all with confidence "low" or "medium". Do not guess — if multiple printings are equally
plausible, list them all.

"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "set_code": {"type": "string"},
                    "collector_number": {"type": "string"},
                    "fragment_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "notes": {"type": "string"},
                    "type": {"type": "string"},
                    "artist": {"type": "string"},
                },
                "required": ["name", "set_code", "collector_number", "fragment_indices", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "query_local_db",
        "description": (
            "Run a read-only SELECT query against the local Scryfall SQLite cache.\n\n"
            "Schema:\n"
            "  cards(oracle_id, name, type_line, mana_cost, cmc, oracle_text)\n"
            "  printings(scryfall_id, oracle_id, set_code, collector_number, rarity, artist, finishes, full_art, promo)\n"
            "  sets(set_code, set_name, set_type, released_at)\n\n"
            "IMPORTANT: The local cache is incomplete — only cards from sets the user has explicitly "
            "cached are present. Empty results mean the card is not cached locally, not that it "
            "doesn't exist. Prefer querying only cards and printings; only join sets if you need "
            "set_name, and use LEFT JOIN since not all sets are guaranteed to have a row.\n\n"
            "Notes:\n"
            "- finishes is a JSON array stored as TEXT (e.g. '[\"nonfoil\"]', '[\"foil\"]')\n"
            "- Use LIKE with % for substring matching; COLLATE NOCASE for case-insensitivity\n"
            "- Always LIMIT results (e.g. LIMIT 10)\n"
            "- Only SELECT is permitted"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A SELECT statement to run against the local DB",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "rerun_ocr",
        "description": (
            "Re-run EasyOCR on the image to get a fresh text extraction. "
            "Use if initial OCR seems poor or incomplete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "analyze_image",
        "description": (
            "Use Claude Vision to directly analyze the full card image. "
            "EXPENSIVE — only use as a last resort after OCR and Scryfall search "
            "have failed to identify a card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _trace(msg: str, status_callback) -> None:
    if status_callback:
        status_callback(msg)
    else:
        print(msg, file=sys.stderr)


def _format_fragments(fragments: list[dict]) -> str:
    lines = []
    for i, f in enumerate(fragments):
        b = f["bbox"]
        lines.append(
            f'[{i}] (x={int(b["x"])},y={int(b["y"])} w={int(b["w"])},h={int(b["h"])}'
            f' conf={f["confidence"]:.2f}): "{f["text"]}"'
        )
    return "\n".join(lines)


def _tool_query_local_db(sql: str, conn: sqlite3.Connection) -> str:
    sql_stripped = sql.strip()
    if not sql_stripped.upper().startswith("SELECT"):
        return "Error: only SELECT statements are permitted"
    rows = conn.execute(sql_stripped).fetchall()
    if not rows:
        return "No results found in local cache"
    cols = rows[0].keys()
    lines = [" | ".join(str(row[c]) for c in cols) for row in rows]
    return "\n".join(lines)


def _tool_rerun_ocr(image_path: str) -> str:
    fragments = run_ocr_with_boxes(image_path)
    return _format_fragments(fragments)


def _tool_analyze_image(image_path: str, client: anthropic.Anthropic) -> str:
    vision = ClaudeVision(model=VISION_MODEL)
    image_data = vision.encode_image(image_path)
    media_type = vision._get_media_type(image_path)

    response = client.messages.create(
        model=VISION_MODEL,
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
                        "text": (
                            "This is a photo of one or more Magic: The Gathering cards. "
                            "Describe every card you can see: name, set code, collector number, "
                            "rarity, and any other identifying information visible on the card."
                        ),
                    },
                ],
            }
        ],
    )
    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text
    return text


def _has_tool_use(response) -> bool:
    return any(block.type == "tool_use" for block in response.content)


def run_agent(
    image_path: str,
    ocr_fragments: list[dict],
    max_calls: int | None = None,
    status_callback=None,
) -> list[dict]:
    """Run the tool-using agent to identify MTG cards from an image.

    Args:
        image_path: Path to the card image file.
        ocr_fragments: Pre-computed OCR fragments from run_ocr_with_boxes().
        max_calls: Maximum tool calls. Defaults to max(DEFAULT_MAX_CALLS,
                   int(DEFAULT_MAX_CALLS * len(ocr_fragments) / 10)).
        status_callback: Optional callable for trace messages (replaces stderr).

    Returns:
        List of card dicts with name, set_code, collector_number, fragment_indices, confidence.
    """
    n = len(ocr_fragments)
    if max_calls is None:
        max_calls = max(DEFAULT_MAX_CALLS, int(DEFAULT_MAX_CALLS * n / 10))
    agent_model = AGENT_MODEL_SONNET if n > LARGE_FRAGMENT_THRESHOLD else AGENT_MODEL_HAIKU

    client = anthropic.Anthropic()
    conn = get_connection()

    _trace(f"[AGENT] Starting with {n} OCR fragments (max_calls={max_calls}, model={agent_model})", status_callback)

    initial_content = (
        f"I have run OCR on the image `{image_path}`. "
        f"Here are the {len(ocr_fragments)} text fragments found:\n\n"
        + _format_fragments(ocr_fragments)
        + "\n\nPlease identify all MTG cards in this image."
    )
    messages = [{"role": "user", "content": initial_content}]

    tool_call_count = 0
    vision_used = [False]
    vision_cached_result = [None]
    response = None

    while tool_call_count < max_calls:
        response = client.messages.create(
            model=agent_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        for block in response.content:
            if block.type == "text":
                _trace(f"[AGENT] {block.text.strip()}", status_callback)
            elif block.type == "tool_use":
                _trace(f"[TOOL CALL] {block.name}: {json.dumps(block.input)}", status_callback)

        if response.stop_reason == "end_turn" or not _has_tool_use(response):
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_call_count += 1
            name = block.name
            inputs = block.input

            if name == "query_local_db":
                result = _tool_query_local_db(inputs.get("sql", ""), conn)
            elif name == "rerun_ocr":
                result = _tool_rerun_ocr(image_path)
            elif name == "analyze_image":
                if vision_used[0]:
                    result = vision_cached_result[0] or (
                        "[analyze_image already called — use query_local_db instead.]"
                    )
                else:
                    result = _tool_analyze_image(image_path, client)
                    vision_used[0] = True
                    vision_cached_result[0] = result
            else:
                result = f"Unknown tool: {name}"

            _trace(
                f"[TOOL RESULT] {name}: "
                f"{result[:500]}{'...' if len(result) > 500 else ''}",
                status_callback,
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    _trace(f"[FINAL] Tool calls used: {tool_call_count}/{max_calls}", status_callback)

    # If the last response was end_turn it hasn't been appended to messages yet.
    # Add it so the conversation is complete, then ask for the final answer.
    if response is not None and response.stop_reason == "end_turn":
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Output your final identification now."})
    # Budget-exhausted case: messages already ends with the tool results (user turn),
    # so the structured output call below will naturally close the loop.

    _trace("[FINAL] Requesting structured output...", status_callback)
    final_response = client.messages.create(
        model=AGENT_MODEL_HAIKU,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=messages,
        output_config={
            "format": {
                "type": "json_schema",
                "schema": OUTPUT_SCHEMA,
            }
        },
    )
    result = json.loads(final_response.content[0].text)
    return result["cards"]
