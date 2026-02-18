"""Tool-using Claude agent service for MTG card identification from photos."""

import json
import sqlite3
import sys
import time

import anthropic

from mtg_collector.db.connection import get_db_path
from mtg_collector.services.claude import ClaudeVision


AGENT_MODEL_HAIKU = "claude-haiku-4-5-20251001"
AGENT_MODEL_SONNET = "claude-sonnet-4-6"
VISION_MODEL = "claude-opus-4-6"
DEFAULT_MAX_CALLS = 12
LARGE_FRAGMENT_THRESHOLD = 70
CONTEXT_UPGRADE_THRESHOLD = 8_000  # input tokens; switch Haiku → Sonnet if context grows large

SYSTEM_PROMPT = """\
You are an expert Magic: The Gathering card identifier. 
You have OCR text fragments from a photo of MTG cards indicating position (bounding boxes),
text at that position, and confidence scores from OCR. 

YOUR JOB:
Do your best to identify every card in the image. Repeat this strategy for all cards in the image:
1. Interpret OCR fragments to find card data. The most important indicators are name, set code, and collector number
2. Search to verify using query_local_db — when disambiguating printings, JOIN sets to get
   set_name and released_at so you can reason about which sets are plausible
3. If you still cannot determine which printing after 4 search attempts, call analyze_image —
   it can identify border color (black/white/silver), card frame era, set icon shape, and other
   visual details that OCR misses, especially useful for older cards. It is quite expensive.
4. Continue searching until no further disambiguation is possible
5. Stop calling tools and list candidate cards. 

OCR BOUNDING BOXES

Start by identifying how many cards are in the image, then use your knowledge to identify each one.

If the photo contains multiple cards side by side, use horizontal position (x coordinates)
to determine which fragments belong to which card. Cards will ALWAYS be positioned vertically
in an image. The aspect ratio of a Magic card is 63:88 (wide:tall).

CARD LAYOUT (top to bottom):
  - Title (top left of card)
  - Colorless portion of a mana cost (optional; top right of card)
  - Type and Subtype (middle left of card, below the art; subtype optional)
  - Rules text (below type line — may be blank on vanilla creatures)
  - Flavor text (italic, below rules — optional)
  - Bottom-left corner: collector number, set code, artist name (on newer cards: see Collector Numbers below)
  - Bottom-right corner: power/toughness (creatures only)

There may be large vertical gaps between the title and the type line — that is the card art.
Cards with no rules text will have another gap between the type line and the collector info.
All of these text regions belong to the SAME card. Do NOT split them into separate cards.
Some cards hae wildly different faces. Usually if text appears directly below the title of a card,
it is rules text. Use your judgment and knowledge as an expert.

Collector numbers — the printed format has changed over Magic's history:
  - Pre-1998 (before Exodus): NO collector number printed on card at all.
  - 1998–2014 (Exodus through M15): printed as "CN/TOTAL" (e.g., "10/250"), 1-3 digit CN.
  - 2015–2023 (M15 frame through Phyrexia): CN on its own line, 1-3 digits, no leading zeros.
  - 2023+ (March of the Machine onward): exactly 4 digits with leading zeros (e.g., 0092, 0161).
    If you see fewer than 4 digits from a 4-digit-era card, the OCR is truncated — omit it.
  - Some cards across all eras have letter suffixes (a, b, s, z) or prefixes (A-) for variants.

USING OCR DATA TO SEARCH
The most reliable indicators of a card are its name, set code, and collector number.
For older cards without a set code or collector number, artist and flavor text are
the best tools to narrow potential printings. If a date is present
Card text can be used also, but older card rules text wording may not match Scryfall.

DISAMBIGUATION RULE — this is critical:
If you cannot distinguish between printings of a card, you MUST return one entry for EVERY
plausible printing with confidence "low" or "medium". This is not a failure, this is success!
Do NOT pick one and declare confidence "high" based solely on artist name or rules text match
unless you have other reasons to be certain (such as a high-confidence date stamp from OCR).

Example: OCR shows card name "Grizzly Bears" with artist "Jeff A. Menges" and no date.
DB query returns many printings, all with identical features: Unlimited (2ed), Revised (3ed) both do not have dates, and on several sets, dates are extremely small: OCR may have just missed it.
CORRECT: return ALL the separate printings, each with confidence "low".
WRONG: return a single entry with set_code="sum", confidence="high".

This rule applies even after calling analyze_image — if vision analysis cannot definitively
resolve the printing, still return all remaining plausible candidates.

KNOWN HARD CASES
The DISAMBIGUATION RULE applies in these cases: Return all reasonable candidates and let the user decie.
* 3rd Edition (Revised, set code 3ed), 4th Edition (4ed), and 5th edition (5ed) are
  very similar: White-bordered, no set symbol, similar wording. 4th edition and 5th edition
  have dates under the artist line, so high-confidence OCR can help disinguish, but 4ed and 5ed
  are nearly identical.
* Similarly, distinguishing between Alpha and Beta can be difficult even for humans: Both
  black-bordered with identical wordings across most cards.
* OCR gives you text printed on the card. Scryfall's data contains modern wordings of rules
  text on cards (aka Oracle text). These can be very different, so have caution when doing
  rules text matching. 
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
            "doesn't exist. When listing candidate printings for disambiguation, JOIN sets to include "
            "set_name and released_at — this helps reason about which sets are plausible. "
            "Always use LEFT JOIN since not all sets are guaranteed to have a row.\n\n"
            "Notes:\n"
            "- finishes is a JSON array stored as TEXT (e.g. '[\"nonfoil\"]', '[\"foil\"]')\n"
            "- Use LIKE with % for substring matching; COLLATE NOCASE for case-insensitivity\n"
            "- Do NOT use LIMIT when fetching printings of a specific card — you need all rows to find the right printing\n"
            "- Use LIMIT only for broad/exploratory queries (e.g. browsing sets)\n"
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
        "name": "analyze_image",
        "description": (
            "Use Claude Vision to directly analyze the full card image. "
            "Can only be called ONCE per session. "
            "Use when OCR and DB search have not been enough to identify the card or narrow down the printing. "
            "Especially useful for older cards: it can read border color (black border = Alpha/Beta/Unlimited/some promos; "
            "white border = Revised through 7th edition; silver = Unsets), card frame era, set icon shape and color, "
            "and all card text directly from the image."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _trace(msg: str, status_callback, trace_lines: list[str] | None = None) -> None:
    if trace_lines is not None:
        trace_lines.append(msg)
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


_DB_ROW_CAP = 200
_DB_CHAR_CAP = 12_000


def _tool_query_local_db(sql: str, conn: sqlite3.Connection) -> str:
    sql_stripped = sql.strip()
    if not sql_stripped.upper().startswith("SELECT"):
        return "Error: only SELECT statements are permitted"
    rows = conn.execute(sql_stripped).fetchall()
    if not rows:
        return "No results found in local cache"
    cols = rows[0].keys()
    lines = []
    total_chars = 0
    for i, row in enumerate(rows):
        if i >= _DB_ROW_CAP:
            lines.append(
                f"[Truncated: row cap of {_DB_ROW_CAP} reached. "
                f"{len(rows) - _DB_ROW_CAP} rows omitted. Refine your query.]"
            )
            break
        line = " | ".join(str(row[c]) for c in cols)
        total_chars += len(line) + 1
        if total_chars > _DB_CHAR_CAP:
            lines.append(
                f"[Truncated: character cap of {_DB_CHAR_CAP} reached. "
                f"{len(rows) - i} rows omitted. Refine your query.]"
            )
            break
        lines.append(line)
    return "\n".join(lines)


def _tool_analyze_image(image_path: str, client: anthropic.Anthropic) -> str:
    vision = ClaudeVision(model=VISION_MODEL)
    image_data = vision.encode_image(image_path)
    media_type = vision._get_media_type(image_path)

    response = client.messages.create(
        model=VISION_MODEL,
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
                        "text": (
                            "This is a photo of one or more Magic: The Gathering cards. "
                            "For each card, describe everything clearly visible that would help "
                            "identify it and its specific printing — card text, border color, frame "
                            "style, set symbol, any numbers or codes, artist line, and anything else "
                            "you notice. Only describe what you can clearly see; if something is "
                            "unclear or not visible, say so explicitly rather than guessing."
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


def _call_api(fn, status_callback, trace_lines=None, **kwargs):
    """Call fn(**kwargs) with exponential backoff on 529 Overloaded errors.

    After 3 Haiku failures switches to Sonnet for remaining retries.
    """
    for attempt in range(6):
        try:
            return fn(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code != 529 or attempt == 5:
                raise
            if attempt == 2 and kwargs.get("model") == AGENT_MODEL_HAIKU:
                kwargs["model"] = AGENT_MODEL_SONNET
                _trace("[AGENT] Switching to Sonnet after 3 Haiku overload errors", status_callback, trace_lines)
            wait = 3 * (2 ** attempt)
            _trace(f"[AGENT] Overloaded (529), retrying in {wait}s...", status_callback, trace_lines)
            time.sleep(wait)


def run_agent(
    image_path: str,
    ocr_fragments: list[dict],
    max_calls: int | None = None,
    status_callback=None,
) -> tuple[list[dict], list[str]]:
    """Run the tool-using agent to identify MTG cards from an image.

    Args:
        image_path: Path to the card image file.
        ocr_fragments: Pre-computed OCR fragments from run_ocr_with_boxes().
        max_calls: Maximum tool calls. Defaults to max(DEFAULT_MAX_CALLS,
                   int(DEFAULT_MAX_CALLS * len(ocr_fragments) / 10)).
        status_callback: Optional callable for trace messages (replaces stderr).

    Returns:
        (cards, trace) where cards is a list of card dicts and trace is the
        list of all trace lines emitted during the run.
    """
    n = len(ocr_fragments)
    if max_calls is None:
        max_calls = max(DEFAULT_MAX_CALLS, int(DEFAULT_MAX_CALLS * n / 10))
    agent_model = AGENT_MODEL_SONNET if n > LARGE_FRAGMENT_THRESHOLD else AGENT_MODEL_HAIKU

    client = anthropic.Anthropic()
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row

    trace_lines: list[str] = []

    _trace(f"[AGENT] Starting with {n} OCR fragments (max_calls={max_calls}, model={agent_model})", status_callback, trace_lines)

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
        response = _call_api(
            client.messages.create,
            status_callback,
            trace_lines=trace_lines,
            model=agent_model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        model_label = "haiku" if agent_model == AGENT_MODEL_HAIKU else "sonnet"
        for block in response.content:
            if block.type == "text":
                _trace(f"[AGENT/{model_label}] {block.text.strip()}", status_callback, trace_lines)
            elif block.type == "tool_use":
                _trace(f"[TOOL CALL/{model_label}] {block.name}: {json.dumps(block.input)}", status_callback, trace_lines)

        if (
            agent_model == AGENT_MODEL_HAIKU
            and response.usage.input_tokens >= CONTEXT_UPGRADE_THRESHOLD
        ):
            agent_model = AGENT_MODEL_SONNET
            _trace(
                f"[AGENT] Context at {response.usage.input_tokens} tokens, upgrading to Sonnet",
                status_callback,
                trace_lines,
            )

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
                trace_lines,
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

    _trace(f"[FINAL] Tool calls used: {tool_call_count}/{max_calls}", status_callback, trace_lines)

    FINAL_PROMPT = (
        "Output your final identification now. "
        "Exhaustively list every card candidate visible in this image. "
        "If you are uncertain which printing a card is, include one entry per plausible printing "
        "(same name, different set_code/collector_number), all with confidence 'low' or 'medium'. "
        "Do not collapse uncertain printings into a single guess."
    )
    # If the last response was end_turn it hasn't been appended to messages yet.
    # Add it so the conversation is complete, then ask for the final answer.
    if response is not None and response.stop_reason == "end_turn":
        messages.append({"role": "assistant", "content": response.content})
    # Both paths get the same final prompt.
    messages.append({"role": "user", "content": FINAL_PROMPT})

    _trace("[FINAL] Requesting structured output...", status_callback, trace_lines)
    final_response = _call_api(
        client.messages.create,
        status_callback,
        trace_lines=trace_lines,
        model=agent_model,
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
    return result["cards"], trace_lines
