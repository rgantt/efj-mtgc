#!/usr/bin/env python3
"""Parse Claude Code CI sandbox JSON logs into a readable summary.

Usage:
    python3 ci/parse-log.py ci/logs/implement-60-20260220-131244.log
    python3 ci/parse-log.py ci/logs/*.log          # multiple files
    python3 ci/parse-log.py ci/logs/foo.log -v     # verbose: include tool results
"""
import json
import sys

DIMMED = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def shorten(text, max_len=200):
    if not text:
        return ""
    text = str(text).replace("\n", "\\n")
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def extract_content_blocks(obj):
    """Extract content blocks from the stream-json format.

    Messages are wrapped: {"type":"assistant","message":{"role":"assistant","content":[...]}}
    """
    msg = obj.get("message", {})
    if msg:
        return msg.get("content", [])
    return obj.get("content", [])


def extract_tool_result(obj):
    """Extract tool result content from user messages (tool_result type)."""
    msg = obj.get("message", {})
    if msg:
        content = msg.get("content", [])
    else:
        content = obj.get("content", [])
    if isinstance(content, str):
        return content
    results = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "tool_result":
                results.append(block.get("content", ""))
    return "\n".join(str(r) for r in results) if results else ""


def is_error_text(text):
    """Check if text contains error indicators."""
    lower = text.lower()
    for kw in ["error", "traceback", "failed", "errno", "exception"]:
        if kw in lower:
            # Exclude false positives
            if "is_error" in lower or "iserror" in lower:
                continue
            return True
    return False


def parse_log(path, verbose=False):
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}Log: {path}{RESET}")
    print(f"{BOLD}{'='*60}{RESET}\n")

    entries = []
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Non-JSON preamble (git output, uv sync, etc.)
                if verbose:
                    print(f"{DIMMED}[{line_no}] {shorten(line, 100)}{RESET}")
                continue
            entries.append((line_no, obj))

    tool_calls = 0
    errors = []
    edits = []
    bash_cmds = []

    for line_no, obj in entries:
        obj_type = obj.get("type", "")
        msg = obj.get("message", {})
        role = msg.get("role", "") if msg else ""

        # System messages (init, task_started, etc.)
        if obj_type == "system":
            subtype = obj.get("subtype", "")
            if subtype == "task_started":
                desc = obj.get("description", "")
                task_type = obj.get("task_type", "")
                print(f"{GREEN}[{line_no}] system: task_started{RESET} ({task_type}) {desc}")
            elif verbose:
                print(f"{DIMMED}[{line_no}] system: {subtype}{RESET}")
            continue

        # Final result
        if obj_type == "result":
            text = obj.get("result", "")
            cost = obj.get("total_cost_usd", 0)
            duration = obj.get("duration_ms", 0)
            duration_s = duration / 1000 if duration else 0
            is_error = obj.get("is_error", False)
            subtype = obj.get("subtype", "")
            num_turns = obj.get("num_turns", "?")
            prefix = f"{RED}RESULT ({subtype})" if is_error or "error" in subtype else f"{GREEN}RESULT ({subtype})"
            print(f"\n{prefix}:{RESET} {shorten(text, 400)}")
            print(f"  Cost: ${cost:.4f} | Duration: {duration_s:.0f}s | Turns: {num_turns}")
            if is_error or "error" in subtype:
                errors.append((line_no, f"{subtype}: {shorten(text, 200)}"))
            continue

        # Assistant messages
        if obj_type == "assistant" or role == "assistant":
            content = extract_content_blocks(obj)
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "thinking":
                    # Skip thinking blocks unless verbose
                    if verbose:
                        text = block.get("thinking", "")
                        print(f"{DIMMED}[{line_no}] thinking: {shorten(text, 150)}{RESET}")
                    continue

                if btype == "text":
                    text = block.get("text", "").strip()
                    if text:
                        print(f"{CYAN}[{line_no}] assistant:{RESET} {shorten(text, 300)}")
                    continue

                if btype == "tool_use":
                    tool_calls += 1
                    name = block.get("name", "?")
                    inp = block.get("input", {})

                    if name == "Edit":
                        fp = inp.get("file_path", "?")
                        edits.append(fp)
                        print(f"{YELLOW}[{line_no}] Edit{RESET} {fp}")
                        if verbose:
                            print(f"    old: {shorten(inp.get('old_string', ''), 80)}")
                            print(f"    new: {shorten(inp.get('new_string', ''), 80)}")

                    elif name == "Write":
                        fp = inp.get("file_path", "?")
                        print(f"{YELLOW}[{line_no}] Write{RESET} {fp}")

                    elif name == "Read":
                        fp = inp.get("file_path", "?")
                        if verbose:
                            print(f"{DIMMED}[{line_no}] Read {fp}{RESET}")

                    elif name == "Bash":
                        cmd = shorten(inp.get("command", ""), 120)
                        bash_cmds.append(inp.get("command", ""))
                        print(f"{YELLOW}[{line_no}] Bash{RESET} {cmd}")

                    elif name == "Glob":
                        if verbose:
                            pat = inp.get("pattern", "?")
                            print(f"{DIMMED}[{line_no}] Glob {pat}{RESET}")

                    elif name == "Grep":
                        if verbose:
                            pat = inp.get("pattern", "?")
                            print(f"{DIMMED}[{line_no}] Grep {pat}{RESET}")

                    elif name == "Task":
                        desc = inp.get("description", "?")
                        prompt = shorten(inp.get("prompt", ""), 150)
                        print(f"{GREEN}[{line_no}] Task{RESET} ({desc}) {prompt}")

                    elif name == "TodoWrite":
                        todos = inp.get("todos", [])
                        items = [t.get("content", "?") for t in todos if t.get("status") != "completed"]
                        print(f"{YELLOW}[{line_no}] TodoWrite{RESET} {len(todos)} items")
                        if verbose:
                            for item in items[:5]:
                                print(f"    - {shorten(item, 80)}")

                    else:
                        print(f"{YELLOW}[{line_no}] {name}{RESET} {shorten(str(inp), 120)}")
            continue

        # User/tool result messages
        if obj_type == "user" or role == "user":
            result_text = extract_tool_result(obj)
            # Also check the tool_use_result shortcut
            tur = obj.get("tool_use_result", {})
            if isinstance(tur, str):
                tur = {"stdout": tur}
            stdout = tur.get("stdout", "") if tur else ""
            stderr = tur.get("stderr", "") if tur else ""
            combined = result_text or stdout
            if stderr:
                combined = combined + " STDERR: " + stderr if combined else stderr

            if combined and is_error_text(combined):
                errors.append((line_no, shorten(combined, 300)))
                print(f"{RED}[{line_no}] result (ERROR):{RESET} {shorten(combined, 300)}")
            elif verbose and combined:
                print(f"{DIMMED}[{line_no}] result: {shorten(combined, 150)}{RESET}")
            continue

    # Summary
    print(f"\n{BOLD}--- Summary ---{RESET}")
    print(f"  JSON lines: {len(entries)}")
    print(f"  Tool calls: {tool_calls}")
    print(f"  Edits: {len(edits)}")
    if edits:
        from collections import Counter
        for fp, count in Counter(edits).most_common():
            print(f"    {fp}: {count} edits")
    print(f"  Bash commands: {len(bash_cmds)}")
    print(f"  Errors/failures: {len(errors)}")
    if errors:
        for line_no, err in errors:
            print(f"    {RED}[{line_no}] {err}{RESET}")


def main():
    args = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    files = [a for a in args if not a.startswith("-")]

    if not files:
        print(f"Usage: {sys.argv[0]} <log_file> [-v]")
        sys.exit(1)

    for path in files:
        parse_log(path, verbose=verbose)


if __name__ == "__main__":
    main()
