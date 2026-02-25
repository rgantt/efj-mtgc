"""
UI testing harness — Claude Vision agent loop.

Drives a Playwright browser by sending screenshots + interactive element lists
to Claude, which decides the next action at each step.  Continues until Claude
declares the goal achieved (``done``) or unachievable (``fail``), or the step
limit is hit.

Screenshots are saved after every action:
``{scenario}_{step:02d}_{label}.png``
"""

import base64
import json
import logging
import os
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

MODEL = os.environ.get("UI_TEST_MODEL", "claude-sonnet-4-6")
MAX_STEPS = 20

SYSTEM = """\
You are a UI testing agent. You interact with a web application to accomplish \
a goal by choosing browser actions one at a time.

Each turn you receive:
1. A screenshot of the current page
2. A numbered list of interactive elements visible on the page

Rules:
- Choose ONE action per turn using the provided tools.
- Refer to elements by their index number from the elements list.
- After filling an input, the page may update asynchronously (debounce). \
Check the next screenshot before acting on results.
- Use 'done' ONLY after you can visually confirm the goal is fully achieved.
- Use 'fail' if the goal clearly cannot be accomplished.
- Be methodical — don't skip steps or assume state you can't see.\
"""

TOOLS = [
    {
        "name": "navigate",
        "description": "Go to a URL path on the site.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "URL path, e.g. /sealed or /collection",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "click",
        "description": "Click an interactive element.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "integer",
                    "description": "Element index from the list",
                },
            },
            "required": ["element"],
        },
    },
    {
        "name": "fill",
        "description": "Clear an input field and type new text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "integer",
                    "description": "Element index (must be an input/textarea)",
                },
                "value": {
                    "type": "string",
                    "description": "Text to type",
                },
            },
            "required": ["element", "value"],
        },
    },
    {
        "name": "select_option",
        "description": "Choose an option from a <select> dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "integer",
                    "description": "Element index (must be a <select>)",
                },
                "label": {
                    "type": "string",
                    "description": "Visible text of the option to select",
                },
            },
            "required": ["element", "label"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the page up or down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                },
            },
            "required": ["direction"],
        },
    },
    {
        "name": "done",
        "description": "The goal has been visually confirmed as achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What was accomplished",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "fail",
        "description": "The goal cannot be achieved.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the goal is unachievable",
                },
            },
            "required": ["reason"],
        },
    },
]

# Injected into the page to enumerate visible interactive elements and tag
# each one with data-uitest="N" so the harness can target them reliably.
EXTRACT_ELEMENTS_JS = """
(() => {
  document.querySelectorAll('[data-uitest]').forEach(
    el => el.removeAttribute('data-uitest')
  );

  const sels = [
    'button', 'a[href]', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="tab"]',
    '[role="checkbox"]', '[role="radio"]', 'summary',
    '[onclick]', '[tabindex]:not([tabindex="-1"])',
  ].join(', ');

  const results = [];
  let idx = 0;

  const vw = window.innerWidth;
  const vh = window.innerHeight;

  function tag(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    if (rect.right < 0 || rect.left > vw || rect.bottom < 0 || rect.top > vh) return;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;

    el.setAttribute('data-uitest', String(idx));

    let value = null;
    if (el.tagName === 'SELECT' && el.selectedOptions.length)
      value = el.selectedOptions[0].text;
    else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
      value = (el.value || '').slice(0, 50) || null;

    results.push({
      idx: idx,
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().slice(0, 80) || null,
      type: el.type || null,
      placeholder: el.placeholder || null,
      id: el.id || null,
      value: value,
      disabled: el.disabled || false,
    });
    idx++;
  }

  // Pass 1: standard interactive elements.
  for (const el of document.querySelectorAll(sels)) tag(el);

  // Pass 2: elements with cursor:pointer that weren't already tagged
  // (catches dynamically-rendered list items with JS click handlers).
  for (const el of document.querySelectorAll('li, div, span, tr, td')) {
    if (el.hasAttribute('data-uitest')) continue;
    if (el.querySelector('[data-uitest]')) continue;  // skip parents of tagged children
    const style = getComputedStyle(el);
    if (style.cursor !== 'pointer') continue;
    tag(el);
  }

  return results;
})()
"""


class UIHarness:
    """Drive a Playwright page toward a UX goal via Claude Vision agent loop."""

    def __init__(self, page, base_url: str, screenshot_dir: Path, scenario_name: str):
        self.page = page
        self.base_url = base_url
        self.screenshot_dir = screenshot_dir
        self.scenario_name = scenario_name
        self.client = anthropic.Anthropic()
        self._step = 0
        self._history: list[dict] = []
        self._messages: list[dict] = []
        self._pending_tool_result: dict | None = None

    # ── public API ────────────────────────────────────────────────────────

    def run(self, goal: str, max_steps: int = MAX_STEPS) -> dict:
        """Run the agent loop.  Returns ``{status, summary|reason, steps}``."""
        log.info("[%s] Goal: %s", self.scenario_name, goal.strip()[:120])
        self.page.goto(f"{self.base_url}/", wait_until="networkidle")

        for _ in range(max_steps):
            screenshot_b64, elements = self._observe()
            log.info(
                "[%s] Step %d — %d interactive elements visible",
                self.scenario_name, self._step, len(elements),
            )
            action = self._decide(goal, screenshot_b64, elements)

            name = action["name"]
            inputs = action["input"]

            if name == "done":
                log.info("[%s] DONE: %s", self.scenario_name, inputs["summary"])
                self._snap("done")
                return {
                    "status": "done",
                    "summary": inputs["summary"],
                    "steps": self._history,
                }

            if name == "fail":
                log.warning("[%s] FAIL: %s", self.scenario_name, inputs["reason"])
                self._snap("fail")
                return {
                    "status": "fail",
                    "reason": inputs["reason"],
                    "steps": self._history,
                }

            log.info(
                "[%s] Step %d → %s(%s)",
                self.scenario_name, self._step, name, json.dumps(inputs),
            )
            result = self._execute(name, inputs)
            log.info("[%s] Step %d result: %s", self.scenario_name, self._step, result)
            self._history.append({
                "step": self._step,
                "action": name,
                "input": inputs,
                "result": result,
            })
            self._settle()

        self._snap("max_steps")
        log.warning("[%s] Exceeded %d steps", self.scenario_name, max_steps)
        return {
            "status": "fail",
            "reason": f"Exceeded {max_steps} steps",
            "steps": self._history,
        }

    # ── internals ─────────────────────────────────────────────────────────

    def _observe(self):
        """Screenshot the page and extract interactive elements."""
        self._step += 1
        path = self._snap(f"step_{self._step:02d}")
        screenshot_b64 = base64.standard_b64encode(path.read_bytes()).decode()
        elements = self.page.evaluate(EXTRACT_ELEMENTS_JS)
        return screenshot_b64, elements

    def _decide(self, goal, screenshot_b64, elements):
        """Send current state to Claude and get back a tool-use action."""
        elements_text = self._format_elements(elements)

        user_content = []

        # Include pending tool_result from previous turn.
        if self._pending_tool_result:
            user_content.append(self._pending_tool_result)
            self._pending_tool_result = None

        user_content.append({
            "type": "text",
            "text": (
                f"Goal: {goal}\n\n"
                f"Current page elements:\n{elements_text}"
            ) if not self._messages else (
                f"Current page elements:\n{elements_text}"
            ),
        })
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            },
        })

        self._messages.append({"role": "user", "content": user_content})

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            tools=TOOLS,
            messages=self._messages,
        )

        # Append the full assistant response to maintain conversation history.
        self._messages.append({"role": "assistant", "content": response.content})

        # Log any reasoning text before the tool call.
        reasoning = "".join(b.text for b in response.content if b.type == "text")
        if reasoning:
            log.info("[%s] Reasoning: %s", self.scenario_name, reasoning.strip()[:200])

        for block in response.content:
            if block.type == "tool_use":
                # Stash the tool_result to prepend to the next user turn.
                self._pending_tool_result = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "OK",
                }
                return {"name": block.name, "input": block.input}

        # No tool call — treat as failure.
        return {"name": "fail", "input": {"reason": f"No action chosen: {reasoning[:200]}"}}

    def _execute(self, action, inputs):
        """Dispatch an action to Playwright.  Returns a short result string."""
        # Re-tag elements right before acting — DOM may have re-rendered
        # since _observe() (e.g. async search results replacing innerHTML).
        self.page.evaluate(EXTRACT_ELEMENTS_JS)

        timeout = 5_000  # 5s max per action
        try:
            if action == "navigate":
                self.page.goto(
                    f"{self.base_url}{inputs['path']}",
                    wait_until="networkidle", timeout=timeout,
                )
                return "navigated"

            if action == "click":
                selector = f'[data-uitest="{inputs["element"]}"]'
                self.page.click(selector, timeout=timeout)
                return "clicked"

            if action == "fill":
                selector = f'[data-uitest="{inputs["element"]}"]'
                self.page.fill(selector, inputs["value"], timeout=timeout)
                return "filled"

            if action == "select_option":
                selector = f'[data-uitest="{inputs["element"]}"]'
                self.page.select_option(selector, label=inputs["label"], timeout=timeout)
                return "selected"

            if action == "scroll":
                delta = -500 if inputs["direction"] == "up" else 500
                self.page.mouse.wheel(0, delta)
                return "scrolled"

            return f"unknown action: {action}"
        except Exception as e:
            return f"error: {e}"

    def _settle(self):
        """Wait briefly for async page updates to land."""
        self.page.wait_for_timeout(500)
        try:
            self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

    def _snap(self, label: str) -> Path:
        """Take a viewport screenshot and return the file path."""
        name = f"{self.scenario_name}_{self._step:02d}_{label}.png"
        path = self.screenshot_dir / name
        self.page.screenshot(path=str(path))
        return path

    @staticmethod
    def _format_elements(elements):
        lines = []
        for el in elements:
            parts = [f"[{el['idx']}]", el["tag"]]
            if el.get("id"):
                parts.append(f"#{el['id']}")
            if el.get("type"):
                parts.append(f"type={el['type']}")
            if el.get("text"):
                parts.append(f'"{el["text"]}"')
            if el.get("placeholder"):
                parts.append(f'placeholder="{el["placeholder"]}"')
            if el.get("value"):
                parts.append(f'value="{el["value"]}"')
            if el.get("disabled"):
                parts.append("(disabled)")
            lines.append(" ".join(parts))
        return "\n".join(lines)

